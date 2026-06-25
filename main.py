import argparse
import asyncio
import csv
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Error as PlaywrightError, Page, async_playwright
from pymongo import ASCENDING, MongoClient, UpdateOne


BASE_URL = "https://www.expireddomains.net"
LOGIN_URL = "https://www.expireddomains.net/login/"
MEMBER_AREA_WARMUP_URL = LOGIN_URL
DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", re.IGNORECASE)
MIN_INTERVAL_SECONDS = 1800
MIN_EXPORT_INTERVAL_SECONDS = 600
MIN_REQUEST_GAP_SECONDS = 50.0
MAX_PAGES_PER_RUN = 2
DEFAULT_DOMAINS_PER_PAGE = 25

RAW_TLD_FILTERS = """
.dk, .cl, .pt, .sk, .lt, .ro, *.jp, .*au, .no, .jp, *.jp, .hr, .nu, .au,
.ee, .ar, .*ar, .nz, .ie, .it, .lu, .rs, .hu, .es, .eu, .*nz, .*at, .at,
.se, .kz, .md, *.es, .sm, .tc, .hk, .ca, .ch, .co.uk, .is, .xyz, .be,
.cz, .il, .tv, .org.il, .si, .ma, .lv, .by, .ru, .ir, .fi, .info, .care,
.cloud, .blog, .me, .su, .store, .shop, .online, .tw, .com.tw, .vip, .top,
.site, .sbs, .live, .monster, .cn, .com.br, .com.ua, .pl, com.cn, .life,
.fun, .ai, .ae, .com.my, .fr, .co.il, .com.tr, .org.ua, .tl, .co.za,
.net.cn, .ooo, .org.cn, .co.tz, .gov.cn, .cat, .co.kr, .co.id, .id, .cc,
.biz.id, .com.sg, .com.hr, org.tw, .re, .pe, .com.pl, .ge, .bg, .org.tr,
.tn, .biz.in, .com.ve
"""


@dataclass
class Settings:
    login: str
    password: str
    scrape_url: str
    interval_seconds: int
    request_gap_seconds: float
    pages_per_run: int
    start_page: int
    advance_pages: bool
    page_cursor_file: Path
    target_total_domains: int
    domains_per_page: int
    trust_flow_min: int
    site_tld_blocklist: str
    apply_site_filters: bool
    tld_filter_mode: str
    only_available: bool
    auto_login: bool
    headless: bool
    output_json: Path
    output_jsonl: Path
    export_domains_only: bool
    export_domains_file: Path
    seen_file: Path
    state_dir: Path
    mongo_uri: str
    mongo_db: str
    mongo_collection: str
    adspower_enabled: bool
    adspower_api_url: str
    adspower_api_key: str
    adspower_user_id: str
    adspower_profile_name: str
    adspower_app_path: str
    member_area_warmup_url: str
    manual_login_wait_seconds: int
    human_delay_min_seconds: float
    human_delay_max_seconds: float


class AccountBlockedError(RuntimeError):
    pass


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings() -> Settings:
    load_dotenv(override=True)
    settings = Settings(
        login=os.getenv("EXPIREDDOMAINS_LOGIN", "").strip(),
        password=os.getenv("EXPIREDDOMAINS_PASSWORD", "").strip(),
        scrape_url=os.getenv("SCRAPE_URL", "https://www.expireddomains.net/expired-domains/").strip(),
        interval_seconds=int(os.getenv("SCRAPE_INTERVAL_SECONDS", str(MIN_INTERVAL_SECONDS))),
        request_gap_seconds=float(os.getenv("REQUEST_GAP_SECONDS", "3")),
        pages_per_run=int(os.getenv("SCRAPE_PAGES_PER_RUN", "5")),
        start_page=int(os.getenv("SCRAPE_START_PAGE", "1")),
        advance_pages=env_bool("SCRAPE_ADVANCE_PAGES", False),
        page_cursor_file=Path(os.getenv("SCRAPE_PAGE_CURSOR_FILE", "page_cursor.json")),
        target_total_domains=int(os.getenv("SCRAPE_TARGET_TOTAL_DOMAINS", "0")),
        domains_per_page=int(os.getenv("SCRAPE_DOMAINS_PER_PAGE", str(DEFAULT_DOMAINS_PER_PAGE))),
        trust_flow_min=int(os.getenv("SCRAPE_TRUST_FLOW_MIN", "0")),
        site_tld_blocklist=os.getenv("SITE_TLD_BLOCKLIST", "").strip(),
        apply_site_filters=env_bool("APPLY_SITE_FILTERS", False),
        tld_filter_mode=os.getenv("SCRAPE_TLD_FILTER_MODE", "include").strip().lower(),
        only_available=env_bool("SCRAPE_ONLY_AVAILABLE", True),
        auto_login=env_bool("EXPIREDDOMAINS_AUTO_LOGIN", True),
        headless=env_bool("SCRAPE_HEADLESS", True),
        output_json=Path(os.getenv("OUTPUT_JSON", "result.json")),
        output_jsonl=Path(os.getenv("OUTPUT_JSONL", "domains.jsonl")),
        export_domains_only=env_bool("EXPORT_DOMAINS_ONLY", env_bool("EXPORT_CSV_ONLY", False)),
        export_domains_file=Path(os.getenv("EXPORT_DOMAINS_FILE", os.getenv("EXPORT_CSV", "expired_domains_export.txt"))),
        seen_file=Path(os.getenv("SEEN_FILE", "seen_domains.json")),
        state_dir=Path(os.getenv("STATE_DIR", ".browser-state")),
        mongo_uri=os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017").strip(),
        mongo_db=os.getenv("MONGO_DB", "expired_domains_db").strip(),
        mongo_collection=os.getenv("MONGO_COLLECTION", "expired_domains").strip(),
        adspower_enabled=env_bool("ADSPOWER_ENABLED", False),
        adspower_api_url=os.getenv("ADSPOWER_API_URL", "http://127.0.0.1:51441").strip().rstrip("/"),
        adspower_api_key=os.getenv("ADSPOWER_API_KEY", "").strip(),
        adspower_user_id=os.getenv("ADSPOWER_USER_ID", "").strip(),
        adspower_profile_name=os.getenv("ADSPOWER_PROFILE_NAME", "").strip(),
        adspower_app_path=os.getenv("ADSPOWER_APP_PATH", "").strip(),
        member_area_warmup_url=os.getenv("MEMBER_AREA_WARMUP_URL", MEMBER_AREA_WARMUP_URL).strip(),
        manual_login_wait_seconds=int(os.getenv("EXPIREDDOMAINS_MANUAL_LOGIN_WAIT_SECONDS", "300")),
        human_delay_min_seconds=float(os.getenv("HUMAN_DELAY_MIN_SECONDS", "1.5")),
        human_delay_max_seconds=float(os.getenv("HUMAN_DELAY_MAX_SECONDS", "5.0")),
    )
    return conservative_settings(settings)


def conservative_settings(settings: Settings) -> Settings:
    min_interval = MIN_EXPORT_INTERVAL_SECONDS if settings.export_domains_only else MIN_INTERVAL_SECONDS
    if settings.interval_seconds < min_interval:
        print(
            f"SCRAPE_INTERVAL_SECONDS={settings.interval_seconds} is below the conservative minimum; "
            f"using {min_interval}.",
            file=sys.stderr,
        )
        settings.interval_seconds = min_interval
    if settings.request_gap_seconds < MIN_REQUEST_GAP_SECONDS:
        print(
            f"REQUEST_GAP_SECONDS={settings.request_gap_seconds} is below the conservative minimum; "
            f"using {MIN_REQUEST_GAP_SECONDS}.",
            file=sys.stderr,
        )
        settings.request_gap_seconds = MIN_REQUEST_GAP_SECONDS
    if settings.pages_per_run > MAX_PAGES_PER_RUN:
        print(
            f"SCRAPE_PAGES_PER_RUN={settings.pages_per_run} is above the conservative maximum; "
            f"using {MAX_PAGES_PER_RUN}.",
            file=sys.stderr,
        )
        settings.pages_per_run = MAX_PAGES_PER_RUN
    if settings.domains_per_page <= 0:
        settings.domains_per_page = DEFAULT_DOMAINS_PER_PAGE
    if settings.tld_filter_mode not in {"include", "exclude"}:
        print(
            f"SCRAPE_TLD_FILTER_MODE={settings.tld_filter_mode!r} is invalid; using 'include'.",
            file=sys.stderr,
        )
        settings.tld_filter_mode = "include"
    if settings.human_delay_min_seconds < 0:
        settings.human_delay_min_seconds = 0
    if settings.human_delay_max_seconds < settings.human_delay_min_seconds:
        settings.human_delay_max_seconds = settings.human_delay_min_seconds
    return settings


def looks_account_blocked(text: str, url: str) -> bool:
    lowered = text.lower()
    return (
        "your account was disabled" in lowered
        or "accountdeactivated" in url.lower()
        or "account was deactivated" in lowered
        or "account has been disabled" in lowered
    )


def normalize_tld_filter(value: str) -> str | None:
    value = value.strip().lower()
    if not value:
        return None
    value = value.replace("*", "")
    while value.startswith(".."):
        value = value[1:]
    if not value.startswith("."):
        value = "." + value
    return value


def tld_filters() -> tuple[str, ...]:
    values = {normalize_tld_filter(item) for item in RAW_TLD_FILTERS.replace("\n", " ").split(",")}
    return tuple(sorted(v for v in values if v))


def domain_matches(domain: str, filters: tuple[str, ...]) -> bool:
    domain = domain.lower().strip(".")
    return any(domain.endswith(tld) for tld in filters)


def domain_allowed_by_tlds(domain: str, filters: tuple[str, ...], mode: str) -> bool:
    matched = domain_matches(domain, filters)
    return not matched if mode == "exclude" else matched


def page_url(base_url: str, page_number: int, domains_per_page: int = DEFAULT_DOMAINS_PER_PAGE) -> str:
    if page_number <= 1:
        return base_url
    start = (page_number - 1) * domains_per_page
    parsed = urlparse(base_url)
    query_values = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query_values["start"] = str(start)
    query = urlencode(query_values)
    return urlunparse(parsed._replace(query=query, fragment="listing"))


def filtered_listing_url(url: str, settings: Settings) -> str:
    parsed = urlparse(url)
    query_values = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if settings.domains_per_page > 0:
        query_values["flimit"] = str(settings.domains_per_page)
    if settings.site_tld_blocklist:
        query_values["ftldsblock"] = settings.site_tld_blocklist
    if settings.trust_flow_min > 0:
        query_values["fmseotf"] = str(settings.trust_flow_min)
    query = urlencode(query_values)
    return urlunparse(parsed._replace(query=query, fragment="listing"))


def clean_text(node: Any) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def parse_number(value: Any) -> float | None:
    text = str(value or "").replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def record_trust_flow(record: dict[str, Any]) -> float | None:
    for key, value in record.items():
        normalized = key.lower().replace(" ", "").replace("_", "")
        if normalized in {"tf", "trustflow"}:
            return parse_number(value)
    return None


def extract_domain(cell: Any) -> str | None:
    for link in cell.select("a.namelinks, a"):
        candidate = clean_text(link).split()[0].lower()
        if DOMAIN_RE.match(candidate):
            return candidate
    for token in clean_text(cell).split():
        candidate = token.lower().strip()
        if DOMAIN_RE.match(candidate):
            return candidate
    return None


def parse_domains(
    html: str,
    source_url: str,
    filters: tuple[str, ...],
    tld_filter_mode: str,
    only_available: bool,
    trust_flow_min: int,
) -> tuple[list[dict[str, Any]], bool]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.base1")
    if table is None:
        return [], False

    headers = [clean_text(th) or f"col_{index}" for index, th in enumerate(table.select("thead th"))]
    rows: list[dict[str, Any]] = []

    for tr in table.select("tbody tr"):
        cells = tr.find_all("td", recursive=False)
        if not cells:
            continue

        domain = extract_domain(cells[0])
        if not domain or not domain_allowed_by_tlds(domain, filters, tld_filter_mode):
            continue

        values = [clean_text(cell) for cell in cells]
        record = {headers[i] if i < len(headers) else f"col_{i}": value for i, value in enumerate(values)}
        record["Domain"] = domain
        record["source_url"] = source_url
        record["scraped_at"] = datetime.now(timezone.utc).isoformat()

        status = str(record.get("Status", "")).lower()
        if only_available and "available" not in status:
            continue

        trust_flow = record_trust_flow(record)
        if trust_flow_min > 0 and trust_flow is not None and trust_flow < trust_flow_min:
            continue

        rows.append(record)

    return rows, True


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(data if isinstance(data, list) else [])


def load_page_cursor(settings: Settings) -> int:
    if not settings.advance_pages or not settings.page_cursor_file.exists():
        return settings.start_page
    try:
        data = json.loads(settings.page_cursor_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return settings.start_page
    page_number = data.get("next_page") if isinstance(data, dict) else None
    if not isinstance(page_number, int) or page_number < settings.start_page:
        return settings.start_page
    return page_number


def load_page_cursor_url(settings: Settings) -> str:
    if not settings.advance_pages or not settings.page_cursor_file.exists():
        return ""
    try:
        data = json.loads(settings.page_cursor_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    next_url = data.get("next_url") if isinstance(data, dict) else None
    return str(next_url) if next_url else ""


def load_page_cursor_batch(settings: Settings) -> int:
    if not settings.advance_pages or not settings.page_cursor_file.exists():
        return 1
    try:
        data = json.loads(settings.page_cursor_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 1
    batch = data.get("next_batch") if isinstance(data, dict) else None
    if isinstance(batch, int) and batch > 0:
        return batch
    page_number = data.get("next_page") if isinstance(data, dict) else None
    if isinstance(page_number, int) and page_number > settings.start_page:
        return page_number
    return 1


def save_page_cursor(settings: Settings, next_page: int, next_url: str = "", next_batch: int | None = None) -> None:
    if not settings.advance_pages:
        return
    data = {
        "next_page": max(settings.start_page, next_page),
        "next_batch": next_batch if next_batch is not None else max(1, next_page),
        "next_url": next_url,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    settings.page_cursor_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_seen(path: Path, seen: set[str]) -> None:
    path.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def save_results(settings: Settings, new_rows: list[dict[str, Any]], seen: set[str]) -> None:
    previous: list[dict[str, Any]] = []
    if settings.output_json.exists():
        try:
            loaded = json.loads(settings.output_json.read_text(encoding="utf-8"))
            previous = loaded if isinstance(loaded, list) else []
        except json.JSONDecodeError:
            previous = []

    combined = previous + new_rows
    settings.output_json.write_text(json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")

    if new_rows:
        with settings.output_jsonl.open("a", encoding="utf-8") as fh:
            for row in new_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    save_seen(settings.seen_file, seen)


def extract_domains_from_rows(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    domain_index = 0
    for index, header in enumerate(headers):
        if header.strip().lower() == "domain":
            domain_index = index
            break

    domains: list[str] = []
    seen_in_page: set[str] = set()
    for row in rows:
        if domain_index >= len(row):
            continue
        domain = row[domain_index].strip().split()[0].lower()
        if DOMAIN_RE.match(domain) and domain not in seen_in_page:
            seen_in_page.add(domain)
            domains.append(domain)
    return domains


def append_domain_csv(path: Path, domains: list[str]) -> int:
    if not domains:
        return 0

    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        if write_header:
            writer.writerow(["domain"])
        for domain in domains:
            writer.writerow([domain])
    return len(domains)


def clipboard_text_to_csv_rows(text: str) -> tuple[list[str], list[list[str]]]:
    lines = [line for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]
    if not lines:
        return [], []
    delimiter = "\t" if any("\t" in line for line in lines[:3]) else ","
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    parsed = [[cell.strip() for cell in row] for row in reader if row]
    if not parsed:
        return [], []
    return parsed[0], parsed[1:]


def save_export_domains_mongo(settings: Settings, domains: list[str], source_url: str, exported_at: datetime, batch: int) -> None:
    if not settings.mongo_uri or not settings.mongo_db or not settings.mongo_collection or not domains:
        return

    with MongoClient(settings.mongo_uri) as client:
        collection = client[settings.mongo_db][settings.mongo_collection]
        collection.create_index([("domain", ASCENDING)], unique=True)
        collection.create_index([("exported_at", ASCENDING)])
        collection.create_index([("batch", ASCENDING)])
        operations = []
        for domain in domains:
            document = {
                "domain": domain,
                "batch": batch,
                "exported_at": exported_at,
                "updated_at": exported_at,
                "data": {
                    "Domain": domain,
                    "batch": batch,
                    "exported_at": exported_at.isoformat(),
                },
            }
            operations.append(
                UpdateOne(
                    {"domain": domain},
                    {
                        "$set": document,
                        "$setOnInsert": {"first_seen_at": exported_at},
                    },
                    upsert=True,
                )
            )
        if operations:
            collection.bulk_write(operations, ordered=False)


def check_mongo(settings: Settings) -> None:
    if not settings.mongo_uri:
        raise RuntimeError("MONGO_URI is empty in .env")
    with MongoClient(settings.mongo_uri) as client:
        collection = client[settings.mongo_db][settings.mongo_collection]
        collection.create_index([("domain", ASCENDING)], unique=True)
        count = collection.count_documents({})
    print(f"MongoDB OK: database={settings.mongo_db}, collection={settings.mongo_collection}, documents={count}")


def adspower_get(settings: Settings, path: str, query: dict[str, str] | None = None) -> dict[str, Any]:
    if not settings.adspower_api_key:
        raise RuntimeError("Missing ADSPOWER_API_KEY in .env")

    last_error: urllib.error.URLError | None = None
    for attempt in range(4):
        try:
            return adspower_get_once(settings, path, query)
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt == 0:
                start_adspower_app(settings)
            time.sleep(5)

    raise RuntimeError(f"AdsPower API request failed: {last_error}") from last_error


def adspower_get_once(settings: Settings, path: str, query: dict[str, str] | None = None) -> dict[str, Any]:
    url = f"{settings.adspower_api_url}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"

    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {settings.adspower_api_key}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read().decode("utf-8", "replace")

    data = json.loads(payload)
    if data.get("code") != 0:
        raise RuntimeError(f"AdsPower API error: {data.get('msg', data)}")
    return data


def start_adspower_app(settings: Settings) -> None:
    if sys.platform != "win32":
        return

    candidates = [
        settings.adspower_app_path,
        r"C:\Program Files\AdsPower Global\AdsPower Global.exe",
        r"C:\Program Files (x86)\AdsPower Global\AdsPower Global.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            os.startfile(candidate)  # type: ignore[attr-defined]
            return


def start_adspower_browser(settings: Settings) -> str:
    user_id = resolve_adspower_user_id(settings)

    data = adspower_get(settings, "/api/v1/browser/start", {"user_id": user_id})
    ws = data.get("data", {}).get("ws", {}).get("puppeteer")
    if not ws:
        raise RuntimeError(f"AdsPower did not return a Playwright/Puppeteer websocket: {data}")
    return str(ws)


def resolve_adspower_user_id(settings: Settings) -> str:
    if settings.adspower_profile_name:
        data = adspower_get(settings, "/api/v1/user/list", {"page": "1", "page_size": "100"})
        profiles = data.get("data", {}).get("list", [])
        for profile in profiles:
            if str(profile.get("name", "")).strip().lower() == settings.adspower_profile_name.lower():
                return str(profile["user_id"])
        raise RuntimeError(f"AdsPower profile named {settings.adspower_profile_name!r} was not found")

    if settings.adspower_user_id:
        return settings.adspower_user_id
    raise RuntimeError("Missing ADSPOWER_USER_ID or ADSPOWER_PROFILE_NAME in .env")


async def is_logged_in(page: Page, navigate: bool = True) -> bool:
    if navigate:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await raise_if_account_blocked(page)
    return await page.locator('a[href="/logout/"], a[href="https://member.expireddomains.net/logout/"]').count() > 0


async def raise_if_account_blocked(page: Page) -> None:
    body = " ".join((await page.locator("body").inner_text()).split())
    if looks_account_blocked(body, page.url):
        await save_debug_page(page, "scraped_page.json")
        raise AccountBlockedError(
            "ExpiredDomains.net account is disabled/deactivated. Stop automation and resolve the account with the site owner."
        )


async def login_if_needed(context: BrowserContext, settings: Settings) -> None:
    page = await context.new_page()
    try:
        await login_if_needed_on_page(page, settings)
    finally:
        await page.close()


async def login_if_needed_on_page(page: Page, settings: Settings, navigate: bool = True) -> None:
    if await is_logged_in(page, navigate=navigate):
        return
    if not settings.login or not settings.password:
        raise RuntimeError("Missing EXPIREDDOMAINS_LOGIN or EXPIREDDOMAINS_PASSWORD in .env")

    attempted_labels: list[str] = []
    last_message = ""
    for label, login, password in login_attempts(settings):
        attempted_labels.append(label)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await page.fill('form[action="/logincheck/"] input[name="login"]', login)
        await page.fill('form[action="/logincheck/"] input[name="password"]', password)
        checkbox = page.locator('form[action="/logincheck/"] input[name="rememberme"]')
        if await checkbox.count():
            await checkbox.check()
        await page.click('form[action="/logincheck/"] button[type="submit"]')
        await page.wait_for_load_state("domcontentloaded")

        if await is_logged_in(page, navigate=False):
            return
        last_message = await first_notice(page)
        await save_debug_page(page, "scraped_page.json")

    tried = ", ".join(attempted_labels)
    detail = f" Site message: {last_message}" if last_message else ""
    raise RuntimeError(f"Login failed after trying: {tried}. ExpiredDomains.net usually needs the account username, not the email.{detail}")


async def wait_for_manual_login(page: Page, settings: Settings) -> None:
    deadline = time.monotonic() + max(settings.manual_login_wait_seconds, 0)
    print(
        "ExpiredDomains.net is not logged in. Log in manually in the open AdsPower tab; "
        f"waiting up to {settings.manual_login_wait_seconds} seconds.",
        file=sys.stderr,
    )
    while time.monotonic() < deadline:
        if await is_logged_in(page, navigate=False):
            return
        await asyncio.sleep(5)
    await save_debug_page(page, "scraped_page.json")
    raise RuntimeError("ExpiredDomains.net manual login was not completed before the wait timeout.")


def first_expireddomains_page(context: BrowserContext) -> Page | None:
    for page in context.pages:
        host = urlparse(page.url).hostname or ""
        if host.endswith("expireddomains.net"):
            return page
    return context.pages[0] if context.pages else None


def existing_adspower_page(context: BrowserContext) -> Page:
    page = first_expireddomains_page(context)
    if page is None:
        raise RuntimeError("AdsPower browser profile has no open tabs. Open one tab in the profile, then run again.")
    return page


async def open_member_area_and_login_if_needed(context: BrowserContext, settings: Settings) -> bool:
    if not settings.member_area_warmup_url:
        return False

    page = first_expireddomains_page(context)
    if page is None:
        page = await context.new_page()
    elif await is_logged_in(page, navigate=False):
        return True

    login_form = page.locator('form[action="/logincheck/"] input[name="login"]')
    if await login_form.count():
        if settings.auto_login:
            await login_if_needed_on_page(page, settings, navigate=False)
        else:
            await wait_for_manual_login(page, settings)
        return True

    page = await goto_with_retry(context, page, settings.member_area_warmup_url)
    await page.wait_for_load_state("domcontentloaded")
    await raise_if_account_blocked(page)

    if await is_logged_in(page, navigate=False):
        return True

    login_form = page.locator('form[action="/logincheck/"] input[name="login"]')
    if await login_form.count():
        if settings.auto_login:
            await login_if_needed_on_page(page, settings, navigate=False)
        else:
            await wait_for_manual_login(page, settings)
    return True


def login_attempts(settings: Settings) -> list[tuple[str, str, str]]:
    register_username = os.getenv("EXPIREDDOMAINS_REGISTER_USERNAME", "").strip()
    register_password = os.getenv("EXPIREDDOMAINS_REGISTER_PASSWORD", "").strip()
    login_prefix = settings.login.split("@", 1)[0].strip() if "@" in settings.login else ""
    register_email = os.getenv("EXPIREDDOMAINS_REGISTER_EMAIL", "").strip()
    register_email_prefix = register_email.split("@", 1)[0].strip() if "@" in register_email else ""

    username_candidates = [
        ("EXPIREDDOMAINS_LOGIN", settings.login),
        ("EXPIREDDOMAINS_LOGIN email prefix", login_prefix),
        ("EXPIREDDOMAINS_REGISTER_USERNAME", register_username),
        ("EXPIREDDOMAINS_REGISTER_EMAIL", register_email),
        ("EXPIREDDOMAINS_REGISTER_EMAIL prefix", register_email_prefix),
    ]
    password_candidates = [
        ("EXPIREDDOMAINS_PASSWORD", settings.password),
        ("EXPIREDDOMAINS_REGISTER_PASSWORD", register_password),
    ]

    unique: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for login_label, login in username_candidates:
        for password_label, password in password_candidates:
            label = f"{login_label} + {password_label}"
            key = (login, password)
            if login and password and key not in seen:
                seen.add(key)
                unique.append((label, login, password))
    return unique


async def first_notice(page: Page) -> str:
    for selector in (".alert", ".error", ".warning", ".box-content", "#content"):
        locator = page.locator(selector)
        if await locator.count():
            text = " ".join((await locator.first.inner_text()).split())
            if "supplied login" in text.lower() or "unknown" in text.lower() or "activation" in text.lower():
                return text[:300]
    body = " ".join((await page.locator("body").inner_text()).split())
    for pattern in (
        r"The supplied login information are unknown\.",
        r"The supplied email address is unknown\.",
        r"The supplied email is already in use\.",
        r"[^.]*activation[^.]*\.",
    ):
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            return match.group(0)[:300]
    return ""


async def save_debug_page(page: Page, path: str) -> None:
    data = {
        "url": page.url,
        "title": await page.title(),
        "body_text": (await page.locator("body").inner_text())[:7000],
    }
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


async def goto_with_retry(context: BrowserContext, page: Page, url: str, attempts: int = 3) -> Page:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return page
        except PlaywrightError as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            await asyncio.sleep(2)
            if page.is_closed():
                existing_page = first_expireddomains_page(context)
                if existing_page is None:
                    raise RuntimeError("Browser tab was closed during navigation and no existing tab is available.")
                page = existing_page
    raise RuntimeError(f"Navigation failed for {url}: {last_error}") from last_error


async def wait_for_network_quiet(page: Page, timeout_ms: int = 10000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightError:
        print("Page did not become fully idle; continuing after domcontentloaded.", file=sys.stderr, flush=True)


async def human_pause(settings: Settings, reason: str = "") -> None:
    delay = random.uniform(settings.human_delay_min_seconds, settings.human_delay_max_seconds)
    if delay <= 0:
        return
    if reason:
        print(f"Pausing {delay:.1f}s before {reason}...", flush=True)
    await asyncio.sleep(delay)


async def apply_site_listing_filters(page: Page, settings: Settings) -> None:
    if not settings.apply_site_filters:
        return

    if not await ensure_filter_panel_open(page):
        await save_debug_page(page, "scraped_page.json")
        raise RuntimeError("Could not open the Show Filter panel. Saved current page text to scraped_page.json.")

    changed = False
    if settings.site_tld_blocklist:
        await human_pause(settings, "setting TLD blocklist")
        await click_filter_tab(page, "Additional")
        changed = await set_tld_blocklist(page, settings.site_tld_blocklist) or changed

    if settings.trust_flow_min > 0:
        await human_pause(settings, "setting Trust Flow filter")
        await click_filter_tab(page, "Majestic")
        changed = await set_trust_flow_min(page, settings.trust_flow_min) or changed

    if settings.domains_per_page > 0:
        await human_pause(settings, "setting page size")
        await click_filter_tab(page, "Common")
        changed = await set_domains_per_page(page, settings.domains_per_page) or changed

    if not changed:
        await save_debug_page(page, "scraped_page.json")
        raise RuntimeError("Could not apply site filters. Saved current page text to scraped_page.json.")

    await human_pause(settings, "applying filters")
    await click_first_visible(
        page,
        (
            'input[type="submit"]',
            'button[type="submit"]',
            'input[value*="Apply"]',
            'button:has-text("Apply")',
            'input[value*="Filter"]',
            'button:has-text("Filter")',
            'input[value*="Show Domains"]',
            'button:has-text("Show Domains")',
        ),
        required=True,
    )
    await page.wait_for_load_state("domcontentloaded")
    await wait_for_network_quiet(page)
    await save_filter_debug(page, "filter_debug.json")


async def click_first_visible(page: Page, selectors: tuple[str, ...], required: bool = False) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        count = await locator.count()
        for index in range(count):
            item = locator.nth(index)
            try:
                if await item.is_visible():
                    await item.click()
                    return True
            except PlaywrightError:
                continue
    if required:
        raise RuntimeError(f"Could not find a visible control matching: {', '.join(selectors)}")
    return False


async def ensure_filter_panel_open(page: Page) -> bool:
    if await filter_panel_has_controls(page):
        return True

    clicked = await click_first_visible(
        page,
        (
            'a:has-text("Show Filter")',
            'button:has-text("Show Filter")',
            'input[value="Show Filter"]',
        ),
    )
    if not clicked:
        clicked = bool(
            await page.evaluate(
                """
                () => {
                    const links = [...document.querySelectorAll('a, button, input[type="button"], input[type="submit"]')];
                    const target = links.find((element) => /show\\s+filter/i.test(element.textContent || element.value || element.title || ''));
                    if (!target) return false;
                    target.click();
                    return true;
                }
                """
            )
        )
    if clicked:
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(500)
    return await filter_panel_has_controls(page)


async def filter_panel_has_controls(page: Page) -> bool:
    return bool(
        await page.evaluate(
            """
            () => {
                const text = document.body ? document.body.innerText : '';
                return (
                    /\\bCommon\\b/i.test(text)
                    && /\\bAdditional\\b/i.test(text)
                    && /\\bMajestic\\b/i.test(text)
                ) || /Domain Name Allowlist/i.test(text);
            }
            """
        )
    )


async def save_filter_debug(page: Page, path: str) -> None:
    data = await page.evaluate(
        """
        () => {
            const visible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            };
            const controls = [...document.querySelectorAll('input, textarea, select')]
                .filter(visible)
                .map((element) => ({
                    tag: element.tagName.toLowerCase(),
                    type: element.getAttribute('type') || '',
                    name: element.getAttribute('name') || '',
                    id: element.id || '',
                    value: element.value || '',
                    nearby_text: (element.closest('fieldset, .box, .filterbox, .formbox, table, tr, div')?.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 300),
                }));
            return {
                url: location.href,
                exact_filters: {
                    flimit: document.querySelector('select[name="flimit"]')?.value || '',
                    ftldsblock: document.querySelector('input[name="ftldsblock"]')?.value || '',
                    fmseotf: document.querySelector('input[name="fmseotf"]')?.value || '',
                },
                controls,
            };
        }
        """
    )
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


async def click_filter_tab(page: Page, tab_name: str) -> None:
    clicked = await click_first_visible(
        page,
        (
            f'a:has-text("{tab_name}")',
            f'button:has-text("{tab_name}")',
            f'li:has-text("{tab_name}") a',
            f'[role="tab"]:has-text("{tab_name}")',
        ),
    )
    if clicked:
        await page.wait_for_timeout(300)


async def set_filter_control(page: Page, label: str, value: str) -> bool:
    return bool(
        await page.evaluate(
        """
        ({ labelText, value }) => {
            const visible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            };
            const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const controlsNearLabel = (labelText) => {
                const needle = normalize(labelText);
                const labels = [...document.querySelectorAll('label, td, th, div, span')]
                    .filter((node) => visible(node) && normalize(node.textContent).includes(needle));
                for (const label of labels) {
                    const row = label.closest('tr, .row, .form-group, li, p, div') || label.parentElement;
                    if (!row) continue;
                    const controls = [...row.querySelectorAll('input, textarea, select')].filter(visible);
                    if (controls.length) return controls;
                    const next = row.nextElementSibling;
                    if (next) {
                        const nextControls = [...next.querySelectorAll('input, textarea, select')].filter(visible);
                        if (nextControls.length) return nextControls;
                    }
                }
                return [];
            };
            const setNativeValue = (element, value) => {
                if (!element) return false;
                if (element.tagName === 'SELECT') {
                    const option = [...element.options].find((item) => item.value === String(value) || item.textContent.trim() === String(value));
                    if (option) {
                        element.value = option.value;
                    } else {
                        return false;
                    }
                } else {
                    element.value = String(value);
                }
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            };

            const controls = controlsNearLabel(labelText);
            if (!controls.length) return false;
            const preferred = labelText.toLowerCase().includes('domains per page')
                ? controls.find((control) => control.tagName === 'SELECT') || controls[0]
                : controls[0];
            if (setNativeValue(preferred, value)) return true;

            for (const control of controls) {
                if (setNativeValue(control, value)) return true;
            }
            return false;
        }
        """,
        {
            "labelText": label,
            "value": value,
        },
        )
    )


async def set_tld_blocklist(page: Page, blocklist: str) -> bool:
    direct = await page.locator('input#ftldsblock[name="ftldsblock"], input[name="ftldsblock"]').count()
    if direct:
        locator = page.locator('input#ftldsblock[name="ftldsblock"], input[name="ftldsblock"]').first
        await locator.fill(blocklist)
        value = await locator.input_value()
        if value == blocklist:
            return True

    exact_panel = bool(
        await page.evaluate(
            """
            ({ value }) => {
                const visible = (element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const setValue = (element) => {
                    element.focus();
                    element.value = value;
                    element.dispatchEvent(new Event('input', { bubbles: true }));
                    element.dispatchEvent(new Event('change', { bubbles: true }));
                    element.blur();
                    return element.value === value;
                };

                const panels = [...document.querySelectorAll('fieldset, .box, .filterbox, .formbox, table, tr, div')]
                    .filter(visible)
                    .filter((element) => /\\btld blocklist\\b/i.test(element.textContent || ''))
                    .sort((a, b) => (a.textContent || '').length - (b.textContent || '').length);
                for (const panel of panels) {
                    const controls = [...panel.querySelectorAll('textarea, input[type="text"], input:not([type])')]
                        .filter(visible);
                    if (controls.length && setValue(controls[0])) return true;
                }

                return false;
            }
            """,
            {"value": blocklist},
        )
    )
    if exact_panel:
        return True

    if await set_filter_control(page, "TLD Blocklist", blocklist):
        return True

    return bool(
        await page.evaluate(
            """
            ({ value }) => {
                const visible = (element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const text = (element) => [
                    element.name,
                    element.id,
                    element.className,
                    element.placeholder,
                    element.title,
                    element.getAttribute('aria-label'),
                    element.closest('tr, .row, .form-group, li, p, div')?.textContent,
                ].filter(Boolean).join(' ').toLowerCase();
                const setValue = (element) => {
                    element.focus();
                    element.value = value;
                    element.dispatchEvent(new Event('input', { bubbles: true }));
                    element.dispatchEvent(new Event('change', { bubbles: true }));
                    element.blur();
                    return element.value === value;
                };

                const controls = [...document.querySelectorAll('textarea, input[type="text"], input:not([type])')]
                    .filter(visible);
                const exact = controls.find((element) => /tld\\s*blocklist|tld.*block|block.*tld|tld.*exclude|exclude.*tld|extension.*exclude/.test(text(element)));
                if (exact && setValue(exact)) return true;

                const likely = controls.find((element) => /tld|domain extension|extension/.test(text(element)));
                if (likely && setValue(likely)) return true;

                const textarea = controls
                    .filter((element) => element.tagName === 'TEXTAREA')
                    .sort((a, b) => (b.offsetWidth * b.offsetHeight) - (a.offsetWidth * a.offsetHeight))[0];
                if (textarea && setValue(textarea)) return true;

                return false;
            }
            """,
            {"value": blocklist},
        )
    )


async def set_trust_flow_min(page: Page, trust_flow_min: int) -> bool:
    value = str(trust_flow_min)
    direct = await page.locator('input#fmseotf[name="fmseotf"], input[name="fmseotf"]').count()
    if direct:
        locator = page.locator('input#fmseotf[name="fmseotf"], input[name="fmseotf"]').first
        await locator.fill(value)
        selected = await locator.input_value()
        if selected == value:
            return True

    exact_row = bool(
        await page.evaluate(
            """
            ({ value }) => {
                const visible = (element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const setValue = (element) => {
                    element.focus();
                    element.value = value;
                    element.dispatchEvent(new Event('input', { bubbles: true }));
                    element.dispatchEvent(new Event('change', { bubbles: true }));
                    element.blur();
                    return element.value === value;
                };

                const rows = [...document.querySelectorAll('tr, .row, .form-group, p, div')]
                    .filter(visible)
                    .filter((element) => /\\btrust flow\\b/i.test(element.textContent || ''))
                    .sort((a, b) => (a.textContent || '').length - (b.textContent || '').length);
                for (const row of rows) {
                    const inputs = [...row.querySelectorAll('input[type="text"], input[type="number"], input:not([type])')]
                        .filter(visible);
                    if (inputs.length && setValue(inputs[0])) return true;
                }
                return false;
            }
            """,
            {"value": value},
        )
    )
    if exact_row:
        return True
    return await set_filter_control(page, "Trust Flow", value)


async def set_domains_per_page(page: Page, domains_per_page: int) -> bool:
    value = str(domains_per_page)
    direct = await page.locator('select#flimit[name="flimit"], select[name="flimit"]').count()
    if direct:
        locator = page.locator('select#flimit[name="flimit"], select[name="flimit"]').first
        await locator.select_option(value)
        selected = await locator.input_value()
        if selected == value:
            return True

    changed = await page.evaluate(
        """
        ({ value }) => {
            const visible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            };
            const normalize = (text) => (text || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const rows = [...document.querySelectorAll('tr, .row, .form-group, li, p, div')]
                .filter(visible)
                .filter((element) => /\\bdomains per page\\b/i.test(element.textContent || ''))
                .sort((a, b) => (a.textContent || '').length - (b.textContent || '').length);
            for (const row of rows) {
                const select = [...row.querySelectorAll('select')].find(visible);
                if (!select) continue;
                const option = [...select.options].find((item) => item.value === value || item.textContent.trim() === value);
                if (!option) return false;
                select.value = option.value;
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }
            const selects = [...document.querySelectorAll('select')].filter(visible);
            for (const select of selects) {
                const option = [...select.options].find((item) => item.value === value || item.textContent.trim() === value);
                const optionTexts = [...select.options].map((item) => item.textContent.trim()).join(' ');
                if (option && /25|50|100|200/.test(optionTexts)) {
                    select.value = option.value;
                    select.dispatchEvent(new Event('input', { bubbles: true }));
                    select.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
            }
            return false;
        }
        """,
        {"value": value},
    )
    if changed:
        return True
    return await set_filter_control(page, "Domains per Page", value)


async def click_copy_icon(page: Page) -> bool:
    return bool(
        await page.evaluate(
            """
            () => {
                const visible = (element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const haystack = (element) => [
                    element.getAttribute('title'),
                    element.getAttribute('alt'),
                    element.getAttribute('aria-label'),
                    element.getAttribute('href'),
                    element.getAttribute('onclick'),
                    element.textContent,
                    element.querySelector('img')?.getAttribute('title'),
                    element.querySelector('img')?.getAttribute('alt'),
                    element.querySelector('img')?.getAttribute('src'),
                ].filter(Boolean).join(' ').toLowerCase();
                const candidates = [...document.querySelectorAll('a, button, input[type="button"], img')]
                    .filter(visible)
                    .filter((element) => /copy|clipboard|csv|export/.test(haystack(element)));
                const target = candidates.find((element) => /copy|clipboard/.test(haystack(element))) || candidates[0];
                if (!target) return false;
                const clickable = target.closest('a, button, input[type="button"]') || target;
                clickable.click();
                return true;
            }
            """
        )
    )


async def read_clipboard_text(page: Page) -> str:
    try:
        return await page.evaluate("navigator.clipboard ? navigator.clipboard.readText() : ''")
    except PlaywrightError:
        return ""


async def visible_table_csv_rows(page: Page) -> tuple[list[str], list[list[str]]]:
    data = await page.evaluate(
        """
        () => {
            const table = document.querySelector('table.base1');
            if (!table) return { headers: [], rows: [] };
            const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const headers = [...table.querySelectorAll('thead th')].map((cell, index) => clean(cell.textContent) || `col_${index}`);
            const rows = [...table.querySelectorAll('tbody tr')].map((tr) => {
                return [...tr.querySelectorAll(':scope > td')].map((cell, index) => {
                    if (index === 0) {
                        const domainLink = cell.querySelector('a.namelinks, a[href*="/domain/"], a');
                        return clean(domainLink ? domainLink.textContent : cell.textContent).split(' ')[0];
                    }
                    return clean(cell.textContent);
                });
            }).filter((row) => row.length);
            return { headers, rows };
        }
        """
    )
    headers = [str(value) for value in data.get("headers", [])]
    rows = [[str(cell) for cell in row] for row in data.get("rows", [])]
    return headers, rows


async def visible_domain_link_rows(page: Page) -> tuple[list[str], list[list[str]]]:
    domains = await page.evaluate(
        """
        () => {
            const visible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            };
            return [...document.querySelectorAll('table.base1 a.namelinks, table.base1 tbody td:first-child a, a.namelinks')]
                .filter(visible)
                .map((link) => (link.textContent || '').replace(/\\s+/g, ' ').trim().split(' ')[0].toLowerCase())
                .filter(Boolean);
        }
        """
    )
    return ["domain"], [[str(domain)] for domain in domains]


async def save_export_debug(page: Page, path: str) -> None:
    data = await page.evaluate(
        """
        () => ({
            url: location.href,
            title: document.title,
            table_count: document.querySelectorAll('table.base1').length,
            namelink_count: document.querySelectorAll('a.namelinks').length,
            next_href: document.querySelector('a.next')?.href || '',
            body_preview: (document.body?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 3000),
        })
        """
    )
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


async def export_current_page_domains(page: Page, settings: Settings, batch: int) -> int:
    await human_pause(settings, "copying domains")
    clicked = await click_copy_icon(page)
    clipboard_text = ""
    if clicked:
        await page.wait_for_timeout(random.randint(700, 2200))
        clipboard_text = await read_clipboard_text(page)

    headers, rows = clipboard_text_to_csv_rows(clipboard_text)
    domains = extract_domains_from_rows(headers, rows)
    if not domains:
        headers, rows = await visible_table_csv_rows(page)
        domains = extract_domains_from_rows(headers, rows)
    if not domains:
        headers, rows = await visible_domain_link_rows(page)
        domains = extract_domains_from_rows(headers, rows)
    if not domains:
        await save_export_debug(page, "export_debug.json")
        raise RuntimeError("Could not export domain list from copy icon or visible table. Saved diagnostics to export_debug.json.")

    exported_at = datetime.now(timezone.utc)
    await human_pause(settings, "saving export")
    exported = append_domain_csv(settings.export_domains_file, domains)
    save_export_domains_mongo(settings, domains, page.url, exported_at, batch)
    return exported


async def next_page_url(page: Page) -> str:
    next_link = page.locator('a.next[rel="nofollow"], a.next, a:has-text("Next Page")').first
    if not await next_link.count():
        return ""
    href = await next_link.get_attribute("href")
    return urljoin(page.url, href) if href else ""


async def signup(settings: Settings) -> None:
    load_dotenv(override=True)
    username = os.getenv("EXPIREDDOMAINS_REGISTER_USERNAME", "").strip()
    email = os.getenv("EXPIREDDOMAINS_REGISTER_EMAIL", "").strip()
    password = os.getenv("EXPIREDDOMAINS_REGISTER_PASSWORD", "").strip()
    if not username or not email or not password:
        raise RuntimeError("Set EXPIREDDOMAINS_REGISTER_USERNAME, EXPIREDDOMAINS_REGISTER_EMAIL, and EXPIREDDOMAINS_REGISTER_PASSWORD in .env")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(settings.state_dir), headless=False)
        page = await context.new_page()
        await page.goto(urljoin(BASE_URL, "/register/"), wait_until="domcontentloaded")
        await page.fill('input[name="login"]', username)
        await page.fill('input[name="pass"]', password)
        await page.fill('input[name="pass2"]', password)
        await page.fill('input[name="email"]', email)
        await page.click('input[name="button_submit"]')
        await page.wait_for_load_state("domcontentloaded")
        print("Signup submitted. If the site sent an activation email, activate the account before running the scraper.")
        await context.close()


async def request_activation(settings: Settings) -> None:
    load_dotenv(override=True)
    email = os.getenv("EXPIREDDOMAINS_REGISTER_EMAIL", "").strip()
    if not email:
        raise RuntimeError("Set EXPIREDDOMAINS_REGISTER_EMAIL in .env")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(settings.state_dir), headless=settings.headless)
        page = await context.new_page()
        await page.goto(urljoin(BASE_URL, "/account/activatemail/"), wait_until="domcontentloaded")
        await page.fill('form[action="/account/activatemail/"] input[name="email"]', email)
        await page.press('form[action="/account/activatemail/"] input[name="email"]', "Enter")
        await page.wait_for_load_state("domcontentloaded")
        message = await first_notice(page)
        print(message or "Activation email request submitted. Check the mailbox and spam folder.")
        await context.close()


async def request_password_reset(settings: Settings) -> None:
    load_dotenv(override=True)
    email = os.getenv("EXPIREDDOMAINS_REGISTER_EMAIL", "").strip()
    if not email:
        raise RuntimeError("Set EXPIREDDOMAINS_REGISTER_EMAIL in .env")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(settings.state_dir), headless=settings.headless)
        page = await context.new_page()
        await page.goto(urljoin(BASE_URL, "/account/newpw/"), wait_until="domcontentloaded")
        await page.fill('form[action="/account/newpw/"] input[name="email"]', email)
        await page.press('form[action="/account/newpw/"] input[name="email"]', "Enter")
        await page.wait_for_load_state("domcontentloaded")
        message = await first_notice(page)
        print(message or "Password reset request submitted. Check the mailbox and spam folder.")
        await context.close()


async def login_visible(settings: Settings) -> None:
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(settings.state_dir), headless=False)
        page = await context.new_page()
        await page.goto(urljoin(BASE_URL, "/login/"), wait_until="domcontentloaded")
        print("A browser window is open. Log in manually, then return here and press Enter.")
        await asyncio.to_thread(input)
        if await is_logged_in(page):
            print(f"Login session saved in {settings.state_dir}")
        else:
            print("Login session was not detected. Check the browser window for the site message.")
        await context.close()


async def scrape_once(settings: Settings) -> list[dict[str, Any]]:
    filters = tld_filters()
    seen = load_seen(settings.seen_file)
    if not settings.export_domains_only and settings.target_total_domains > 0 and len(seen) >= settings.target_total_domains:
        print(f"Target reached: {len(seen)} domains saved, target is {settings.target_total_domains}.")
        return []

    start_page = load_page_cursor(settings)
    start_url = load_page_cursor_url(settings) if settings.export_domains_only else ""
    start_batch = load_page_cursor_batch(settings) if settings.export_domains_only else 1
    new_rows: list[dict[str, Any]] = []
    exported_rows = 0
    exported_batch = 0

    async with async_playwright() as p:
        browser = None
        if settings.adspower_enabled:
            print("Connecting to AdsPower browser profile...", flush=True)
            ws_endpoint = start_adspower_browser(settings)
            browser = await p.chromium.connect_over_cdp(ws_endpoint)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
        else:
            print(f"Opening Playwright browser session at {settings.state_dir}...", flush=True)
            context = await p.chromium.launch_persistent_context(str(settings.state_dir), headless=settings.headless)
        try:
            login_checked = False
            if settings.adspower_enabled:
                login_checked = await open_member_area_and_login_if_needed(context, settings)
            if settings.auto_login and not login_checked:
                await login_if_needed(context, settings)
            if settings.adspower_enabled:
                page = existing_adspower_page(context)
            else:
                page = await context.new_page()
            for page_number in range(start_page, start_page + settings.pages_per_run):
                url = start_url if settings.export_domains_only and start_url else page_url(settings.scrape_url, page_number, settings.domains_per_page)
                url = filtered_listing_url(url, settings)
                print(f"Opening page {page_number}: {url}", flush=True)
                await human_pause(settings, "opening the next page")
                page = await goto_with_retry(context, page, url)
                await wait_for_network_quiet(page)
                await raise_if_account_blocked(page)
                if page_number == start_page and not start_url:
                    print("Applying site filters...", flush=True)
                    await apply_site_listing_filters(page, settings)
                    await raise_if_account_blocked(page)
                if settings.export_domains_only:
                    batch = start_batch + (page_number - start_page)
                    exported_batch = batch
                    print(f"Exporting domains to {settings.export_domains_file} batch={batch}...", flush=True)
                    exported_rows += await export_current_page_domains(page, settings, batch)
                    print(f"Exported {exported_rows} domains so far.", flush=True)
                    next_page = page_number + 1
                    next_url = await next_page_url(page)
                    if next_url:
                        next_url = filtered_listing_url(next_url, settings)
                    save_page_cursor(settings, next_page, next_url, batch + 1)
                    if not next_url:
                        print("No Next Page link found; stopping after this page.")
                        break
                    if settings.request_gap_seconds > 0 and page_number < start_page + settings.pages_per_run - 1:
                        await asyncio.sleep(settings.request_gap_seconds)
                    continue
                rows, found_table = parse_domains(
                    await page.content(),
                    page.url,
                    filters,
                    settings.tld_filter_mode,
                    settings.only_available,
                    settings.trust_flow_min,
                )
                if not found_table:
                    title = await page.title()
                    if not settings.auto_login and "login" in title.lower():
                        await save_debug_page(page, "scraped_page.json")
                        if settings.adspower_enabled:
                            raise RuntimeError("AdsPower profile is not logged in to ExpiredDomains.net. Log in once inside the AdsPower profile, keep EXPIREDDOMAINS_AUTO_LOGIN=false, then run again.")
                        raise RuntimeError(f"Browser session is not logged in to ExpiredDomains.net. Run {sys.executable} main.py --login-visible once, log in manually, then run the scraper again.")
                    raise RuntimeError(f"No domain table found at {url}. Page title: {title!r}. Login or account access may be required.")
                for row in rows:
                    domain = row["Domain"].lower()
                    if domain in seen:
                        continue
                    seen.add(domain)
                    new_rows.append(row)
                    if settings.target_total_domains > 0 and len(seen) >= settings.target_total_domains:
                        break
                save_page_cursor(settings, page_number + 1)
                if settings.target_total_domains > 0 and len(seen) >= settings.target_total_domains:
                    break
                if settings.request_gap_seconds > 0 and page_number < start_page + settings.pages_per_run - 1:
                    await asyncio.sleep(settings.request_gap_seconds)
            if not settings.adspower_enabled:
                await page.close()
        finally:
            if browser is not None:
                pass
            else:
                await context.close()

    if settings.export_domains_only:
        return [{"exported_rows": exported_rows, "batch": exported_batch}]

    save_results(settings, new_rows, seen)
    return new_rows


async def run_forever(settings: Settings) -> None:
    while True:
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            rows = await scrape_once(settings)
            if settings.export_domains_only:
                exported_rows = int(rows[0].get("exported_rows", 0)) if rows else 0
                batch = int(rows[0].get("batch", 0)) if rows else 0
                print(f"[{started}] exported {exported_rows} domains to {settings.export_domains_file}, batch={batch}")
                await asyncio.sleep(settings.interval_seconds)
                continue
            total = len(load_seen(settings.seen_file))
            print(f"[{started}] saved {len(rows)} new matching domains, total={total}")
            if settings.target_total_domains > 0 and total >= settings.target_total_domains:
                print(f"[{started}] target reached: {total}/{settings.target_total_domains}")
                break
        except AccountBlockedError as exc:
            print(f"[{started}] STOPPED: {exc}", file=sys.stderr)
            break
        except Exception as exc:
            print(f"[{started}] ERROR: {exc}")
        await asyncio.sleep(settings.interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="ExpiredDomains.net recurring scraper")
    parser.add_argument("--once", action="store_true", help="Scrape once and exit")
    parser.add_argument("--signup", action="store_true", help="Submit the signup form using registration env vars")
    parser.add_argument("--request-activation", action="store_true", help="Request another activation email")
    parser.add_argument("--request-password-reset", action="store_true", help="Request a password reset email")
    parser.add_argument("--login-visible", action="store_true", help="Open a visible browser so you can log in manually and save the session")
    parser.add_argument("--check-db", action="store_true", help="Create/check the MongoDB database and collection, then exit")
    args = parser.parse_args()

    settings = load_settings()
    try:
        if args.signup:
            asyncio.run(signup(settings))
        elif args.request_activation:
            asyncio.run(request_activation(settings))
        elif args.request_password_reset:
            asyncio.run(request_password_reset(settings))
        elif args.login_visible:
            asyncio.run(login_visible(settings))
        elif args.check_db:
            check_mongo(settings)
        elif args.once:
            rows = asyncio.run(scrape_once(settings))
            if settings.export_domains_only:
                exported_rows = int(rows[0].get("exported_rows", 0)) if rows else 0
                batch = int(rows[0].get("batch", 0)) if rows else 0
                print(f"exported {exported_rows} domains to {settings.export_domains_file}, batch={batch}")
            else:
                print(f"saved {len(rows)} new matching domains")
        else:
            asyncio.run(run_forever(settings))
    except (AccountBlockedError, RuntimeError, PlaywrightError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
