import argparse
import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import dns.exception
import dns.resolver
from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient, UpdateOne


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def split_env(name: str, default: str) -> list[str]:
    value = os.getenv(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


def load_config() -> dict[str, Any]:
    load_dotenv(override=True)
    return {
        "mongo_uri": os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017").strip(),
        "mongo_db": os.getenv("MONGO_DB", "expired_domains_db").strip(),
        "domain_collection": os.getenv("MONGO_COLLECTION", "expired_domains").strip(),
        "nawala_collection": os.getenv("NAWALA_COLLECTION", "domain_nawala_checks").strip(),
        "dns_servers": split_env("NAWALA_DNS_SERVERS", "180.131.144.144,180.131.145.145"),
        "block_keywords": [item.lower() for item in split_env("NAWALA_BLOCK_KEYWORDS", "internetpositif,nawala,trustpositif,blocked")],
        "timeout": float(os.getenv("NAWALA_TIMEOUT_SECONDS", "5")),
        "limit": env_int("NAWALA_CHECK_LIMIT", 200),
        "concurrency": max(1, env_int("NAWALA_CHECK_CONCURRENCY", 20)),
    }


def latest_batch(config: dict[str, Any]) -> int:
    with MongoClient(config["mongo_uri"]) as client:
        collection = client[config["mongo_db"]][config["domain_collection"]]
        row = collection.find_one({"batch": {"$exists": True}}, {"batch": 1}, sort=[("batch", DESCENDING)])
        if not row or not isinstance(row.get("batch"), int):
            raise RuntimeError("No batch was found in the domain collection.")
        return int(row["batch"])


def load_batch_domains(config: dict[str, Any], batch: int, limit: int, only_unchecked: bool) -> list[str]:
    with MongoClient(config["mongo_uri"]) as client:
        db = client[config["mongo_db"]]
        domain_col = db[config["domain_collection"]]
        nawala_col = db[config["nawala_collection"]]
        domain_col.create_index([("batch", ASCENDING)])
        nawala_col.create_index([("domain", ASCENDING), ("batch", ASCENDING)], unique=True)
        nawala_col.create_index([("checked_at", DESCENDING)])

        checked: set[str] = set()
        if only_unchecked:
            checked = {
                row["domain"]
                for row in nawala_col.find({"batch": batch}, {"domain": 1})
                if row.get("domain")
            }

        query: dict[str, Any] = {"batch": batch}
        if checked:
            query["domain"] = {"$nin": list(checked)}

        rows = domain_col.find(query, {"domain": 1}).sort([("domain", ASCENDING)]).limit(limit)
        return [str(row["domain"]).strip().lower() for row in rows if row.get("domain")]


def make_resolver(server: str, timeout: float) -> dns.resolver.Resolver:
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = [server]
    resolver.timeout = timeout
    resolver.lifetime = timeout
    return resolver


def answer_text(answer: dns.resolver.Answer) -> list[str]:
    values: list[str] = []
    for item in answer:
        values.append(str(item).rstrip("."))
    try:
        canonical_name = str(answer.canonical_name).rstrip(".")
        if canonical_name:
            values.append(canonical_name)
    except Exception:
        pass
    return values


def check_domain_on_server(domain: str, server: str, config: dict[str, Any]) -> dict[str, Any]:
    resolver = make_resolver(server, config["timeout"])
    values: list[str] = []
    errors: list[str] = []

    for record_type in ("A", "CNAME"):
        try:
            answer = resolver.resolve(domain, record_type, raise_on_no_answer=False)
            values.extend(answer_text(answer))
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            continue
        except dns.exception.DNSException as exc:
            errors.append(f"{record_type}: {exc.__class__.__name__}")

    haystack = " ".join(values).lower()
    matched_keywords = [keyword for keyword in config["block_keywords"] if keyword in haystack]
    return {
        "server": server,
        "blocked": bool(matched_keywords),
        "matched_keywords": matched_keywords,
        "answers": sorted(set(values)),
        "errors": errors,
    }


async def check_domain(domain: str, batch: int, config: dict[str, Any]) -> dict[str, Any]:
    server_results = await asyncio.gather(
        *[
            asyncio.to_thread(check_domain_on_server, domain, server, config)
            for server in config["dns_servers"]
        ]
    )
    blocked_servers = [result["server"] for result in server_results if result["blocked"]]
    checked_at = datetime.now(timezone.utc)
    status = "blocked" if blocked_servers else "clear"
    if not any(result["answers"] for result in server_results) and any(result["errors"] for result in server_results):
        status = "error"

    return {
        "domain": domain,
        "batch": batch,
        "checked_at": checked_at,
        "status": status,
        "blocked": bool(blocked_servers),
        "blocked_servers": blocked_servers,
        "server_results": server_results,
    }


async def run_checks(domains: list[str], batch: int, config: dict[str, Any]) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(config["concurrency"])
    results: list[dict[str, Any]] = []

    async def worker(domain: str) -> None:
        async with semaphore:
            print(f"Checking {domain} on Nawala DNS...", flush=True)
            result = await check_domain(domain, batch, config)
            results.append(result)

    await asyncio.gather(*(worker(domain) for domain in domains))
    return results


def save_results(config: dict[str, Any], results: list[dict[str, Any]]) -> None:
    if not results:
        return

    with MongoClient(config["mongo_uri"]) as client:
        db = client[config["mongo_db"]]
        nawala_col = db[config["nawala_collection"]]
        domain_col = db[config["domain_collection"]]
        nawala_col.create_index([("domain", ASCENDING), ("batch", ASCENDING)], unique=True)
        nawala_col.create_index([("checked_at", DESCENDING)])

        nawala_ops = [
            UpdateOne(
                {"domain": result["domain"], "batch": result["batch"]},
                {"$set": result, "$setOnInsert": {"created_at": result["checked_at"]}},
                upsert=True,
            )
            for result in results
        ]
        domain_ops = [
            UpdateOne(
                {"domain": result["domain"]},
                {
                    "$set": {
                        "nawala_checked_at": result["checked_at"],
                        "nawala_status": result["status"],
                        "nawala_blocked": result["blocked"],
                        "nawala_batch": result["batch"],
                    }
                },
            )
            for result in results
        ]

        if nawala_ops:
            nawala_col.bulk_write(nawala_ops, ordered=False)
        if domain_ops:
            domain_col.bulk_write(domain_ops, ordered=False)


def main() -> None:
    config = load_config()
    parser = argparse.ArgumentParser(description="Check saved domains against Nawala DNS and save results to MongoDB.")
    parser.add_argument("--batch", type=int, help="Batch number to check. Defaults to the newest batch.")
    parser.add_argument("--limit", type=int, default=config["limit"], help="Maximum domains to check")
    parser.add_argument("--include-checked", action="store_true", help="Recheck domains already checked for this batch")
    parser.add_argument("--domain", help="Check one domain and save it using the chosen/latest batch number")
    args = parser.parse_args()

    batch = args.batch if args.batch is not None else latest_batch(config)
    domains = [args.domain.strip().lower()] if args.domain else load_batch_domains(
        config,
        batch=batch,
        limit=args.limit,
        only_unchecked=not args.include_checked,
    )

    if not domains:
        print(f"No domains need Nawala checks for batch {batch}.")
        return

    print(
        f"Checking {len(domains)} domains from batch {batch}; "
        f"saving to {config['mongo_db']}.{config['nawala_collection']}"
    )
    results = asyncio.run(run_checks(domains, batch, config))
    save_results(config, results)
    blocked = sum(1 for result in results if result["blocked"])
    errors = sum(1 for result in results if result["status"] == "error")
    print(f"Saved {len(results)} Nawala checks. Blocked: {blocked}, clear: {len(results) - blocked - errors}, errors: {errors}")


if __name__ == "__main__":
    main()
