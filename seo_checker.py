import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import Error as PlaywrightError, Page, async_playwright
from pymongo import ASCENDING, DESCENDING, MongoClient, UpdateOne


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def load_config() -> dict[str, Any]:
    load_dotenv(override=True)
    return {
        "mongo_uri": os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017").strip(),
        "mongo_db": os.getenv("MONGO_DB", "expired_domains_db").strip(),
        "domain_collection": os.getenv("MONGO_COLLECTION", "expired_domains").strip(),
        "seo_collection": os.getenv("SEO_COLLECTION", "domain_seo_checks").strip(),
        "ranking_collection": os.getenv("SEO_RANKING_COLLECTION", "domain_seo_rankings").strip(),
        "limit": env_int("SEO_CHECK_LIMIT", 50),
        "concurrency": max(1, env_int("SEO_CHECK_CONCURRENCY", 3)),
        "timeout_ms": env_int("SEO_CHECK_TIMEOUT_MS", 20000),
        "headless": env_bool("SEO_CHECK_HEADLESS", True),
        "recheck_days": env_int("SEO_RECHECK_DAYS", 14),
    }


def score_seo(result: dict[str, Any]) -> tuple[int, list[str]]:
    score = 100
    issues: list[str] = []

    checks = [
        (result.get("reachable"), 30, "site not reachable"),
        (result.get("https"), 10, "not using HTTPS"),
        (result.get("title_ok"), 10, "missing or weak title"),
        (result.get("meta_description_ok"), 10, "missing or weak meta description"),
        (result.get("h1_ok"), 10, "missing H1"),
        (result.get("canonical_ok"), 5, "missing canonical"),
        (result.get("robots_txt"), 5, "missing robots.txt"),
        (result.get("sitemap_xml"), 5, "missing sitemap.xml"),
        (not result.get("noindex"), 10, "page has noindex"),
        (result.get("viewport_ok"), 5, "missing viewport meta"),
    ]
    for passed, penalty, issue in checks:
        if not passed:
            score -= penalty
            issues.append(issue)

    return max(0, score), issues


async def url_exists(page: Page, url: str, timeout_ms: int) -> bool:
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return bool(response and response.status < 400)
    except PlaywrightError:
        return False


async def check_domain(page: Page, domain: str, timeout_ms: int) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc)
    candidates = [f"https://{domain}", f"http://{domain}"]
    final_url = ""
    status: int | None = None
    html = ""
    error = ""

    for url in candidates:
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            status = response.status if response else None
            final_url = page.url
            html = await page.content()
            if status is None or status < 500:
                break
        except PlaywrightError as exc:
            error = str(exc).splitlines()[0][:300]

    reachable = bool(final_url and html and (status is None or status < 400))
    result: dict[str, Any] = {
        "domain": domain,
        "checked_at": checked_at,
        "reachable": reachable,
        "final_url": final_url,
        "status": status,
        "error": error if not reachable else "",
        "https": final_url.startswith("https://"),
    }

    if reachable:
        soup = BeautifulSoup(html, "html.parser")
        title = clean_text(soup.title.string if soup.title else "")
        meta_description = ""
        description_tag = soup.find("meta", attrs={"name": lambda value: value and value.lower() == "description"})
        if description_tag:
            meta_description = clean_text(description_tag.get("content"))
        h1_tags = [clean_text(tag.get_text(" ", strip=True)) for tag in soup.find_all("h1")]
        canonical = soup.find("link", attrs={"rel": lambda value: value and "canonical" in value})
        robots_meta = soup.find("meta", attrs={"name": lambda value: value and value.lower() == "robots"})
        viewport = soup.find("meta", attrs={"name": lambda value: value and value.lower() == "viewport"})
        links = soup.find_all("a")
        images = soup.find_all("img")
        images_missing_alt = sum(1 for image in images if not clean_text(image.get("alt")))

        result.update(
            {
                "title": title,
                "title_length": len(title),
                "title_ok": 10 <= len(title) <= 70,
                "meta_description": meta_description,
                "meta_description_length": len(meta_description),
                "meta_description_ok": 50 <= len(meta_description) <= 170,
                "h1_count": len(h1_tags),
                "h1": h1_tags[:5],
                "h1_ok": len(h1_tags) >= 1,
                "canonical": urljoin(final_url, canonical.get("href", "")) if canonical else "",
                "canonical_ok": bool(canonical and canonical.get("href")),
                "robots_meta": clean_text(robots_meta.get("content")) if robots_meta else "",
                "noindex": bool(robots_meta and "noindex" in clean_text(robots_meta.get("content")).lower()),
                "viewport_ok": bool(viewport and viewport.get("content")),
                "word_count": len(clean_text(soup.get_text(" ", strip=True)).split()),
                "link_count": len(links),
                "image_count": len(images),
                "images_missing_alt": images_missing_alt,
            }
        )

        base = f"{final_url.split('/', 3)[0]}//{final_url.split('/', 3)[2]}"
        result["robots_txt"] = await url_exists(page, urljoin(base, "/robots.txt"), timeout_ms)
        result["sitemap_xml"] = await url_exists(page, urljoin(base, "/sitemap.xml"), timeout_ms)
    else:
        result.update(
            {
                "title_ok": False,
                "meta_description_ok": False,
                "h1_ok": False,
                "canonical_ok": False,
                "robots_txt": False,
                "sitemap_xml": False,
                "noindex": False,
                "viewport_ok": False,
            }
        )

    score, issues = score_seo(result)
    result["seo_score"] = score
    result["issues"] = issues
    return result


def load_domains(config: dict[str, Any], limit: int | None, recheck_days: int, force_all: bool = False) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=recheck_days)
    with MongoClient(config["mongo_uri"]) as client:
        db = client[config["mongo_db"]]
        domain_col = db[config["domain_collection"]]
        seo_col = db[config["seo_collection"]]
        seo_col.create_index([("domain", ASCENDING)], unique=True)
        seo_col.create_index([("checked_at", DESCENDING)])

        recent: set[str] = set()
        if not force_all:
            recent = {
                row["domain"]
                for row in seo_col.find({"checked_at": {"$gte": cutoff}}, {"domain": 1})
                if row.get("domain")
            }
        query = {"domain": {"$nin": list(recent)}} if recent else {}
        cursor = domain_col.find(query, {"domain": 1}).sort([("exported_at", ASCENDING), ("domain", ASCENDING)])
        if limit is not None and limit > 0:
            cursor = cursor.limit(limit)
        rows = cursor
        return [row["domain"] for row in rows if row.get("domain")]


def save_results(config: dict[str, Any], results: list[dict[str, Any]]) -> None:
    if not results:
        return
    with MongoClient(config["mongo_uri"]) as client:
        collection = client[config["mongo_db"]][config["seo_collection"]]
        collection.create_index([("domain", ASCENDING)], unique=True)
        collection.create_index([("checked_at", DESCENDING)])
        operations = [
            UpdateOne(
                {"domain": result["domain"]},
                {"$set": result, "$setOnInsert": {"created_at": result["checked_at"]}},
                upsert=True,
            )
            for result in results
        ]
        collection.bulk_write(operations, ordered=False)


def ranking_tier(score: int, reachable: bool) -> str:
    if not reachable:
        return "unreachable"
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "good"
    if score >= 50:
        return "needs_work"
    return "poor"


def rebuild_rankings(config: dict[str, Any]) -> int:
    with MongoClient(config["mongo_uri"]) as client:
        db = client[config["mongo_db"]]
        seo_col = db[config["seo_collection"]]
        ranking_col = db[config["ranking_collection"]]
        ranking_col.create_index([("domain", ASCENDING)], unique=True)
        ranking_col.create_index([("rank", ASCENDING)])
        ranking_col.create_index([("seo_score", DESCENDING)])
        ranking_col.create_index([("reachable", ASCENDING)])

        cursor = seo_col.find({}, {"_id": 0}).sort(
            [
                ("seo_score", DESCENDING),
                ("reachable", DESCENDING),
                ("checked_at", DESCENDING),
                ("domain", ASCENDING),
            ]
        )
        operations = []
        ranked_at = datetime.now(timezone.utc)
        for rank, row in enumerate(cursor, start=1):
            score = int(row.get("seo_score") or 0)
            reachable = bool(row.get("reachable"))
            document = {
                "domain": row.get("domain"),
                "rank": rank,
                "seo_score": score,
                "ranking_tier": ranking_tier(score, reachable),
                "reachable": reachable,
                "checked_at": row.get("checked_at"),
                "ranked_at": ranked_at,
                "final_url": row.get("final_url", ""),
                "status": row.get("status"),
                "issues": row.get("issues", []),
                "title": row.get("title", ""),
                "meta_description": row.get("meta_description", ""),
                "https": row.get("https", False),
                "h1_count": row.get("h1_count", 0),
                "word_count": row.get("word_count", 0),
                "robots_txt": row.get("robots_txt", False),
                "sitemap_xml": row.get("sitemap_xml", False),
            }
            if document["domain"]:
                operations.append(
                    UpdateOne(
                        {"domain": document["domain"]},
                        {"$set": document, "$setOnInsert": {"created_at": ranked_at}},
                        upsert=True,
                    )
                )

        if operations:
            ranking_col.bulk_write(operations, ordered=False)
        return len(operations)


async def run_checks(config: dict[str, Any], domains: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    queue: asyncio.Queue[str] = asyncio.Queue()
    for domain in domains:
        queue.put_nowait(domain)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=config["headless"])

        async def worker(worker_id: int) -> None:
            page = await browser.new_page()
            try:
                while not queue.empty():
                    domain = await queue.get()
                    try:
                        print(f"[worker {worker_id}] checking {domain}", flush=True)
                        result = await check_domain(page, domain, config["timeout_ms"])
                        results.append(result)
                    except Exception as exc:
                        checked_at = datetime.now(timezone.utc)
                        result = {
                            "domain": domain,
                            "checked_at": checked_at,
                            "reachable": False,
                            "error": str(exc)[:300],
                            "seo_score": 0,
                            "issues": ["checker error"],
                        }
                        results.append(result)
                    finally:
                        queue.task_done()
            finally:
                await page.close()

        workers = [asyncio.create_task(worker(index + 1)) for index in range(config["concurrency"])]
        await queue.join()
        await asyncio.gather(*workers)
        await browser.close()

    return results


async def run_checks_incremental(config: dict[str, Any], domains: list[str], save_every: int = 25) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    lock = asyncio.Lock()
    queue: asyncio.Queue[str] = asyncio.Queue()
    for domain in domains:
        queue.put_nowait(domain)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=config["headless"])

        async def flush(force: bool = False) -> None:
            async with lock:
                if not pending or (not force and len(pending) < save_every):
                    return
                batch = pending[:]
                pending.clear()
            save_results(config, batch)
            print(f"Saved {len(batch)} SEO checks incrementally.", flush=True)

        async def worker(worker_id: int) -> None:
            page = await browser.new_page()
            try:
                while not queue.empty():
                    domain = await queue.get()
                    try:
                        print(f"[worker {worker_id}] checking {domain}", flush=True)
                        result = await check_domain(page, domain, config["timeout_ms"])
                    except Exception as exc:
                        checked_at = datetime.now(timezone.utc)
                        result = {
                            "domain": domain,
                            "checked_at": checked_at,
                            "reachable": False,
                            "error": str(exc)[:300],
                            "seo_score": 0,
                            "issues": ["checker error"],
                        }
                    async with lock:
                        results.append(result)
                        pending.append(result)
                    await flush()
                    queue.task_done()
            finally:
                await page.close()

        workers = [asyncio.create_task(worker(index + 1)) for index in range(config["concurrency"])]
        await queue.join()
        await asyncio.gather(*workers)
        await flush(force=True)
        await browser.close()

    return results


def main() -> None:
    config = load_config()
    parser = argparse.ArgumentParser(description="Check saved domains for basic website SEO and save results to MongoDB.")
    parser.add_argument("--limit", type=int, default=config["limit"], help="Maximum domains to check")
    parser.add_argument("--all", action="store_true", help="Check every domain in the DB, ignoring SEO_CHECK_LIMIT")
    parser.add_argument("--recheck-days", type=int, default=config["recheck_days"], help="Skip domains checked within this many days")
    parser.add_argument("--force", action="store_true", help="Recheck domains even if they were checked recently")
    parser.add_argument("--rank-only", action="store_true", help="Only rebuild SEO ranking collection from existing checks")
    parser.add_argument("--domain", help="Check one domain instead of loading from MongoDB")
    args = parser.parse_args()

    if args.rank_only:
        ranked = rebuild_rankings(config)
        print(f"Rebuilt {ranked} SEO rankings in {config['mongo_db']}.{config['ranking_collection']}")
        return

    limit = None if args.all else args.limit
    domains = [args.domain.strip().lower()] if args.domain else load_domains(
        config,
        limit,
        args.recheck_days,
        force_all=args.force,
    )
    if not domains:
        print("No domains need SEO checks.")
        return

    print(f"Checking {len(domains)} domains; saving to {config['mongo_db']}.{config['seo_collection']}")
    results = asyncio.run(run_checks_incremental(config, domains))
    save_results(config, results)
    ranked = rebuild_rankings(config)
    ok = sum(1 for result in results if result.get("reachable"))
    print(f"Saved {len(results)} SEO checks. Reachable: {ok}, unreachable: {len(results) - ok}")
    print(f"Rebuilt {ranked} SEO rankings in {config['mongo_db']}.{config['ranking_collection']}")


if __name__ == "__main__":
    main()
