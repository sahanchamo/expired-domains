import argparse
import asyncio
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Error as PlaywrightError, Page, async_playwright
from pymongo import ASCENDING, DESCENDING, MongoClient, UpdateOne

from main import load_settings, start_adspower_browser


RESULT_COLUMNS = ["URL", "Title", "DA", "PA", "TBL", "QBL", "Q/T", "OS", "MT", "SS", "DH"]


def complete_domain_value(value: str) -> bool:
    domain = value.strip().lower().replace("https://", "").replace("http://", "").split("/")[0]
    return bool(domain) and ".." not in domain


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config() -> dict[str, Any]:
    load_dotenv(override=True)
    return {
        "mongo_uri": os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017").strip(),
        "mongo_db": os.getenv("MONGO_DB", "expired_domains_db").strip(),
        "domain_collection": os.getenv("MONGO_COLLECTION", "expired_domains").strip(),
        "result_collection": os.getenv("WSC_COLLECTION", "website_seo_checker_results").strip(),
        "url": os.getenv("WSC_URL", "https://websiteseochecker.com/pro-service/").strip(),
        "batch_size": env_int("WSC_BATCH_SIZE", 50),
        "timeout_ms": env_int("WSC_TIMEOUT_MS", 180000),
        "headless": env_bool("WSC_HEADLESS", False),
        "force": env_bool("WSC_FORCE", False),
        "nawala_auto_after_wsc": env_bool("NAWALA_AUTO_AFTER_WSC", True),
        "nawala_api_url": os.getenv("NAWALA_API_URL", "https://ucantseeme.200m.website/api/check").strip(),
        "nawala_api_key": os.getenv("NAWALA_API_KEY", "").strip(),
        "nawala_collection": os.getenv("NAWALA_COLLECTION", "domain_nawala_checks").strip(),
        "nawala_final_collection": os.getenv("NAWALA_FINAL_COLLECTION", "website_seo_checker_nawala_results").strip(),
        "nawala_chunk_size": env_int("NAWALA_CHUNK_SIZE", 200),
        "nawala_timeout_seconds": env_int("NAWALA_API_TIMEOUT_SECONDS", 60),
    }


def load_domains(config: dict[str, Any], limit: int, force: bool = False) -> list[str]:
    with MongoClient(config["mongo_uri"]) as client:
        db = client[config["mongo_db"]]
        domain_col = db[config["domain_collection"]]
        result_col = db[config["result_collection"]]
        result_col.create_index([("domain", ASCENDING)], unique=True)
        result_col.create_index([("checked_at", DESCENDING)])

        checked: set[str] = set()
        if not force:
            checked = {
                row["domain"]
                for row in result_col.find({}, {"_id": 0, "domain": 1})
                if row.get("domain")
            }

        query: dict[str, Any] = {"domain": {"$not": {"$regex": "\\.\\."}}}
        if checked:
            query["domain"]["$nin"] = list(checked)
        cursor = (
            domain_col.find(query, {"_id": 0, "domain": 1, "exported_at": 1})
            .sort([("exported_at", ASCENDING), ("domain", ASCENDING)])
            .limit(limit)
        )

        domains: list[str] = []
        seen: set[str] = set()
        for row in cursor:
            domain = str(row.get("domain", "")).strip().lower()
            if complete_domain_value(domain) and domain not in seen:
                seen.add(domain)
                domains.append(domain)
        return domains


async def open_context(config: dict[str, Any]) -> tuple[Any, BrowserContext, Page]:
    settings = load_settings()
    async with async_playwright() as playwright:
        raise RuntimeError("open_context must be called inside run_checker")


async def fill_domain_box(page: Page, domains: list[str]) -> None:
    text = "\n".join(domains)
    filled = await page.evaluate(
        """
        (text) => {
            const visible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            };

            const textareas = [...document.querySelectorAll('textarea')].filter(visible);
            const textarea = textareas.sort((a, b) => (b.offsetWidth * b.offsetHeight) - (a.offsetWidth * a.offsetHeight))[0];
            if (textarea) {
                textarea.focus();
                textarea.value = text;
                textarea.dispatchEvent(new Event('input', { bubbles: true }));
                textarea.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }

            const ace = document.querySelector('.ace_editor');
            if (ace && window.ace) {
                window.ace.edit(ace).setValue(text, -1);
                return true;
            }

            const codeMirror = document.querySelector('.CodeMirror');
            if (codeMirror && codeMirror.CodeMirror) {
                codeMirror.CodeMirror.setValue(text);
                return true;
            }

            const editable = [...document.querySelectorAll('[contenteditable="true"]')].find(visible);
            if (editable) {
                editable.focus();
                editable.textContent = text;
                editable.dispatchEvent(new Event('input', { bubbles: true }));
                return true;
            }

            return false;
        }
        """,
        text,
    )
    if filled:
        return

    candidate = page.locator("textarea, .ace_text-input, [contenteditable='true']").first
    await candidate.click(timeout=10000)
    await page.keyboard.press("Control+A")
    await page.keyboard.insert_text(text)


def same_wsc_page(current_url: str, target_url: str) -> bool:
    current = urlparse(current_url)
    target = urlparse(target_url)
    return current.netloc == target.netloc and current.path.rstrip("/") == target.path.rstrip("/")


def clean_domain(value: str) -> str:
    return value.strip().lower().replace("https://", "").replace("http://", "").split("/")[0]


def matching_submitted_domain(raw_url: str, submitted_domains: list[str], fallback_index: int) -> str:
    domain = clean_domain(raw_url)
    if domain and "..." not in domain and ".." not in domain:
        for submitted in submitted_domains:
            if domain == submitted:
                return submitted

    prefix = domain.split("...", 1)[0].split("..", 1)[0]
    if prefix:
        matches = [submitted for submitted in submitted_domains if submitted.startswith(prefix)]
        if len(matches) == 1:
            return matches[0]

    if fallback_index < len(submitted_domains):
        return submitted_domains[fallback_index]
    return ""


async def wsc_page(context: BrowserContext, target_url: str) -> tuple[Page, bool]:
    for page in context.pages:
        if same_wsc_page(page.url, target_url):
            return page, False
    return await context.new_page(), True


async def click_check(page: Page) -> None:
    if await page.locator("#bigbutton").count():
        await page.locator("#bigbutton").click(timeout=15000)
        return
    if await page.locator("#formsubmitbutton input[type='submit']").count():
        await page.locator("#formsubmitbutton input[type='submit']").click(timeout=15000)
        return
    await page.get_by_role("button", name="Check").click(timeout=15000)


async def open_bulk_checker(page: Page, url: str, timeout_ms: int) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    bulk_link = page.locator('a.navx-link[href="/pro-service/"]').first
    if await bulk_link.count():
        await bulk_link.click(timeout=15000)
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)


async def parse_result_rows(page: Page) -> list[dict[str, Any]]:
    return await page.evaluate(
        """
        (columns) => {
            const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const normalizeHeader = (value) => clean(value).replace(/\\s+/g, '').toUpperCase();
            const cellValue = (cell) => {
                if (!cell) return '';
                const text = clean(cell.innerText);
                if (text) return text;
                const image = cell.querySelector('img');
                if (image) return clean(image.getAttribute('title') || image.getAttribute('alt') || image.getAttribute('src'));
                const link = cell.querySelector('a[href]');
                if (link) {
                    const text = clean(link.innerText || link.textContent || '');
                    const href = clean(link.href || link.getAttribute('href') || '');
                    return text.includes('...') && href ? href : (text || href);
                }
                return '';
            };
            const tables = [...document.querySelectorAll('table')];
            for (const table of tables) {
                const headerCells = [...table.querySelectorAll('thead th, tr:first-child th')].map((cell) => clean(cell.innerText));
                const normalized = headerCells.map(normalizeHeader);
                if (!normalized.includes('URL') || !normalized.includes('DA') || !normalized.includes('PA')) {
                    continue;
                }

                const columnIndexes = {};
                columns.forEach((column) => {
                    const normalizedColumn = normalizeHeader(column);
                    columnIndexes[column] = normalized.findIndex((header) => header === normalizedColumn);
                });
                const rows = [...table.querySelectorAll('tbody tr')];
                const dataRows = rows.length ? rows : [...table.querySelectorAll('tr')].slice(1);
                return dataRows.map((tr) => {
                    const cells = [...tr.querySelectorAll('td')];
                    const item = {};
                    columns.forEach((column, fallbackIndex) => {
                        const index = columnIndexes[column] >= 0 ? columnIndexes[column] : fallbackIndex;
                        item[column] = cellValue(cells[index]);
                    });
                    const link = tr.querySelector('td a[href]');
                    if (link && (!item.URL || item.URL.includes('...'))) {
                        const text = clean(link.innerText || link.textContent || '');
                        const href = clean(link.href || link.getAttribute('href') || '');
                        item.URL = text.includes('...') && href ? href : (item.URL || text || href);
                    }
                    return item;
                }).filter((row) => row.URL);
            }
            return [];
        }
        """,
        RESULT_COLUMNS,
    )


async def wait_for_results(page: Page, domains: list[str], timeout_ms: int) -> list[dict[str, Any]]:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    domain_set = {domain.lower() for domain in domains}
    last_rows: list[dict[str, Any]] = []
    while asyncio.get_running_loop().time() < deadline:
        rows = await parse_result_rows(page)
        if rows:
            last_rows = rows
            result_domains = {clean_domain(str(row.get("URL", ""))) for row in rows}
            if result_domains & domain_set:
                return rows
        await page.wait_for_timeout(1500)
    if last_rows:
        return last_rows
    raise RuntimeError("Website SEO Checker results table did not appear before timeout")


def normalize_result(row: dict[str, Any], submitted_domain: str) -> dict[str, Any]:
    raw_url = str(row.get("URL", "")).strip()
    domain = clean_domain(raw_url)
    if ("..." in domain or ".." in domain) and submitted_domain:
        domain = submitted_domain
        raw_url = submitted_domain
    checked_at = datetime.now(timezone.utc)
    return {
        "domain": domain,
        "submitted_domain": submitted_domain,
        "checked_at": checked_at,
        "source": "websiteseochecker.com",
        "URL": raw_url,
        "Title": row.get("Title", ""),
        "DA": row.get("DA", ""),
        "PA": row.get("PA", ""),
        "TBL": row.get("TBL", ""),
        "QBL": row.get("QBL", ""),
        "Q/T": row.get("Q/T", ""),
        "OS": row.get("OS", ""),
        "MT": row.get("MT", ""),
        "SS": row.get("SS", ""),
        "DH": row.get("DH", ""),
    }


def result_documents(rows: list[dict[str, Any]], submitted_domains: list[str]) -> list[dict[str, Any]]:
    submitted = [domain.lower() for domain in submitted_domains]
    documents = [
        normalize_result(row, matching_submitted_domain(str(row.get("URL", "")), submitted, index))
        for index, row in enumerate(rows)
    ]
    return [document for document in documents if complete_domain_value(document["domain"])]


def save_results(config: dict[str, Any], rows: list[dict[str, Any]], submitted_domains: list[str]) -> int:
    documents = result_documents(rows, submitted_domains)
    if not documents:
        return 0

    with MongoClient(config["mongo_uri"]) as client:
        col = client[config["mongo_db"]][config["result_collection"]]
        col.create_index([("domain", ASCENDING)], unique=True)
        col.create_index([("checked_at", DESCENDING)])
        operations = [
            UpdateOne(
                {"domain": document["domain"]},
                {"$set": document, "$setOnInsert": {"created_at": document["checked_at"]}},
                upsert=True,
            )
            for document in documents
        ]
        result = col.bulk_write(operations, ordered=False)
        return int(result.upserted_count + result.modified_count)


def call_nawala_api(config: dict[str, Any], domains: list[str]) -> dict[str, Any]:
    payload = {"domains": domains, "chunkSize": config["nawala_chunk_size"]}
    request = Request(
        config["nawala_api_url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": config["nawala_api_key"],
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=config["nawala_timeout_seconds"]) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 403:
            return call_nawala_api_curl(config, payload)
        raise RuntimeError(f"Nawala API returned HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Nawala API request failed: {exc}") from exc
    return json.loads(body)


def call_nawala_api_curl(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        payload_path = fh.name
    try:
        completed = subprocess.run(
            [
                "curl.exe" if os.name == "nt" else "curl",
                "--silent",
                "--show-error",
                "--location",
                config["nawala_api_url"],
                "--header",
                "Content-Type: application/json",
                "--header",
                f"x-api-key: {config['nawala_api_key']}",
                "--data-binary",
                f"@{payload_path}",
            ],
            capture_output=True,
            text=True,
            timeout=config["nawala_timeout_seconds"],
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Nawala curl request failed: {completed.stderr.strip() or completed.stdout.strip()}")
        response_text = completed.stdout.strip()
        response = json.loads(response_text)
        if response.get("ok") is False:
            raise RuntimeError(f"Nawala API returned error: {response}")
        return response
    finally:
        try:
            os.unlink(payload_path)
        except OSError:
            pass


def save_nawala_response(
    config: dict[str, Any],
    response: dict[str, Any],
    wsc_documents: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    results = response.get("results") or []
    if not results:
        return {"nawala_saved": 0, "final_saved": 0}

    checked_at = datetime.now(timezone.utc)
    summary = response.get("summary") or {}
    wsc_by_domain = {document["domain"]: document for document in wsc_documents or [] if document.get("domain")}
    documents = []
    final_documents = []
    for row in results:
        domain = clean_domain(str(row.get("domain") or row.get("input") or ""))
        if not complete_domain_value(domain):
            continue
        nawala_document = {
            "domain": domain,
            "input": row.get("input", ""),
            "status": row.get("status", ""),
            "blocked": bool(row.get("blocked")),
            "listed": bool(row.get("listed")),
            "batchIndex": row.get("batchIndex"),
            "identityId": row.get("identityId", ""),
            "sessionId": row.get("sessionId", ""),
            "checked_at": checked_at,
            "source": "ucantseeme.200m.website",
            "summary": summary,
            "raw": row,
        }
        documents.append(nawala_document)
        wsc_document = dict(wsc_by_domain.get(domain, {}))
        wsc_created_at = wsc_document.pop("created_at", None)
        final_documents.append(
            {
                **wsc_document,
                "domain": domain,
                "wsc_created_at": wsc_created_at,
                "nawala_status": nawala_document["status"],
                "nawala_blocked": nawala_document["blocked"],
                "nawala_listed": nawala_document["listed"],
                "nawala_checked_at": checked_at,
                "nawala_input": nawala_document["input"],
                "nawala_identityId": nawala_document["identityId"],
                "nawala_sessionId": nawala_document["sessionId"],
                "nawala_summary": summary,
                "final_source": "website_seo_checker+nawala",
                "updated_at": checked_at,
            }
        )
    if not documents:
        return {"nawala_saved": 0, "final_saved": 0}

    with MongoClient(config["mongo_uri"]) as client:
        col = client[config["mongo_db"]][config["nawala_collection"]]
        wsc_col = client[config["mongo_db"]][config["result_collection"]]
        final_col = client[config["mongo_db"]][config["nawala_final_collection"]]
        col.create_index([("domain", ASCENDING)], unique=True)
        col.create_index([("checked_at", DESCENDING)])
        col.create_index([("blocked", ASCENDING)])
        final_col.create_index([("domain", ASCENDING)], unique=True)
        final_col.create_index([("nawala_checked_at", DESCENDING)])
        final_col.create_index([("nawala_blocked", ASCENDING)])
        nawala_result = col.bulk_write(
            [
                UpdateOne(
                    {"domain": document["domain"]},
                    {"$set": document, "$setOnInsert": {"created_at": document["checked_at"]}},
                    upsert=True,
                )
                for document in documents
            ],
            ordered=False,
        )
        wsc_col.bulk_write(
            [
                UpdateOne(
                    {"domain": document["domain"]},
                    {
                        "$set": {
                            "nawala_status": document["status"],
                            "nawala_blocked": document["blocked"],
                            "nawala_listed": document["listed"],
                            "nawala_checked_at": document["checked_at"],
                        }
                    },
                )
                for document in documents
            ],
            ordered=False,
        )
        final_result = final_col.bulk_write(
            [
                UpdateOne(
                    {"domain": document["domain"]},
                    {"$set": document, "$setOnInsert": {"created_at": document["nawala_checked_at"]}},
                    upsert=True,
                )
                for document in final_documents
            ],
            ordered=False,
        )
        return {
            "nawala_saved": int(nawala_result.upserted_count + nawala_result.modified_count),
            "final_saved": int(final_result.upserted_count + final_result.modified_count),
        }


def run_nawala_after_wsc(config: dict[str, Any], rows: list[dict[str, Any]], submitted_domains: list[str]) -> dict[str, Any]:
    if not config["nawala_auto_after_wsc"]:
        return {"enabled": False, "message": "Nawala auto check disabled"}
    if not config["nawala_api_key"]:
        return {"enabled": False, "message": "NAWALA_API_KEY is not configured"}

    documents = result_documents(rows, submitted_domains)
    domains = sorted({document["domain"] for document in documents if complete_domain_value(document["domain"])})
    if not domains:
        return {"enabled": True, "submitted": 0, "saved": 0, "message": "No complete domains for Nawala check"}

    try:
        response = call_nawala_api(config, domains)
        saved_counts = save_nawala_response(config, response, documents)
    except Exception as exc:
        return {
            "enabled": True,
            "submitted": len(domains),
            "saved": 0,
            "collection": config["nawala_collection"],
            "final_collection": config["nawala_final_collection"],
            "error": str(exc),
        }
    summary = response.get("summary") or {}
    return {
        "enabled": True,
        "submitted": len(domains),
        "saved": saved_counts["nawala_saved"],
        "collection": config["nawala_collection"],
        "final_saved": saved_counts["final_saved"],
        "final_collection": config["nawala_final_collection"],
        "blocked": summary.get("blockedCount"),
        "not_blocked": summary.get("notBlockedCount"),
        "errors": summary.get("errorCount"),
    }


def load_wsc_documents_for_nawala(config: dict[str, Any], limit: int, force: bool = False) -> list[dict[str, Any]]:
    with MongoClient(config["mongo_uri"]) as client:
        db = client[config["mongo_db"]]
        wsc_col = db[config["result_collection"]]
        final_col = db[config["nawala_final_collection"]]
        checked: set[str] = set()
        if not force:
            checked = {
                row["domain"]
                for row in final_col.find({}, {"_id": 0, "domain": 1})
                if row.get("domain")
            }
        query: dict[str, Any] = {"domain": {"$not": {"$regex": "\\.\\."}}}
        if checked:
            query["domain"]["$nin"] = list(checked)
        rows = list(
            wsc_col.find(query, {"_id": 0})
            .sort([("checked_at", DESCENDING), ("domain", ASCENDING)])
            .limit(max(1, limit))
        )
        return [row for row in rows if complete_domain_value(str(row.get("domain", "")))]


def run_nawala_for_saved_wsc(limit: int = 1500, force: bool = False) -> dict[str, Any]:
    config = load_config()
    if not config["nawala_api_key"]:
        return {"enabled": False, "message": "NAWALA_API_KEY is not configured"}

    documents = load_wsc_documents_for_nawala(config, limit, force=force)
    domains = sorted({str(document.get("domain", "")).strip().lower() for document in documents if complete_domain_value(str(document.get("domain", "")))})
    if not domains:
        return {
            "enabled": True,
            "submitted": 0,
            "saved": 0,
            "final_saved": 0,
            "collection": config["nawala_collection"],
            "final_collection": config["nawala_final_collection"],
            "message": "No WSC domains need Nawala checks",
        }

    response = call_nawala_api(config, domains)
    saved_counts = save_nawala_response(config, response, documents)
    summary = response.get("summary") or {}
    return {
        "enabled": True,
        "submitted": len(domains),
        "saved": saved_counts["nawala_saved"],
        "final_saved": saved_counts["final_saved"],
        "collection": config["nawala_collection"],
        "final_collection": config["nawala_final_collection"],
        "blocked": summary.get("blockedCount"),
        "not_blocked": summary.get("notBlockedCount"),
        "errors": summary.get("errorCount"),
        "summary": summary,
    }


async def run_checker(config: dict[str, Any], domains: list[str]) -> dict[str, Any]:
    settings = load_settings()
    browser = None
    context = None
    page = None
    close_page_when_done = False
    async with async_playwright() as playwright:
        if settings.adspower_enabled:
            ws_endpoint = start_adspower_browser(settings)
            browser = await playwright.chromium.connect_over_cdp(ws_endpoint)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page, close_page_when_done = await wsc_page(context, config["url"])
        else:
            context = await playwright.chromium.launch_persistent_context(str(settings.state_dir), headless=config["headless"])
            page = await context.new_page()
            close_page_when_done = True

        try:
            await open_bulk_checker(page, config["url"], config["timeout_ms"])
            await fill_domain_box(page, domains)
            await click_check(page)
            rows = await wait_for_results(page, domains, config["timeout_ms"])
            saved = save_results(config, rows, domains)
            nawala = run_nawala_after_wsc(config, rows, domains)
            return {
                "submitted": len(domains),
                "result_rows": len(rows),
                "saved": saved,
                "collection": config["result_collection"],
                "nawala": nawala,
            }
        finally:
            if close_page_when_done and page is not None:
                await page.close()
            if browser is None and context is not None:
                await context.close()


async def run_next_batch_async(limit: int | None = None, force: bool | None = None) -> dict[str, Any]:
    config = load_config()
    batch_size = min(limit or config["batch_size"], 50)
    force_value = config["force"] if force is None else force
    domains = load_domains(config, batch_size, force=force_value)
    if not domains:
        return {"submitted": 0, "result_rows": 0, "saved": 0, "collection": config["result_collection"], "message": "No unchecked domains found"}
    return await run_checker(config, domains)


def run_next_batch(limit: int | None = None, force: bool | None = None) -> dict[str, Any]:
    return asyncio.run(run_next_batch_async(limit=limit, force=force))


def main() -> None:
    config = load_config()
    parser = argparse.ArgumentParser(description="Submit unique Mongo domains to Website SEO Checker and save table results.")
    parser.add_argument("--limit", type=int, default=config["batch_size"], help="Domains to submit, max 50")
    parser.add_argument("--force", action="store_true", help="Allow rechecking domains already saved in WSC collection")
    parser.add_argument("--nawala-only", action="store_true", help="Check saved Website SEO Checker domains with the Nawala API")
    args = parser.parse_args()
    if args.nawala_only:
        result = run_nawala_for_saved_wsc(limit=args.limit, force=args.force)
    else:
        result = run_next_batch(limit=args.limit, force=args.force)
    print(result)


if __name__ == "__main__":
    main()
