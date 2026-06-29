import asyncio
import csv
import gzip
import io
import json
import os
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from bson import json_util
from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient

from seo_checker import load_config as load_seo_config
from seo_checker import load_domains, rebuild_rankings, run_checks_incremental, save_results
from website_seo_checker import load_config as load_wsc_config
from website_seo_checker import run_nawala_for_saved_wsc
from website_seo_checker import run_next_batch as run_wsc_next_batch


PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env", override=True)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017").strip()
MONGO_DB = os.getenv("MONGO_DB", "expired_domains_db").strip()
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "expired_domains").strip()
MONGO_DUPLICATE_COLLECTION = os.getenv("MONGO_DUPLICATE_COLLECTION", f"{MONGO_COLLECTION}_duplicates").strip()
API_KEY = os.getenv("API_KEY", "").strip()
SEO_COLLECTION = os.getenv("SEO_COLLECTION", "domain_seo_checks").strip()
SEO_RANKING_COLLECTION = os.getenv("SEO_RANKING_COLLECTION", "domain_seo_rankings").strip()
DB_BACKUP_FORMAT = "expired_domains_dashboard_mongo_backup_v1"

MAIN_SCRIPT = PROJECT_DIR / "main.py"
MAIN_LOG = PROJECT_DIR / "main_process.log"
MAIN_STATUS_FILE = PROJECT_DIR / os.getenv("MAIN_STATUS_FILE", "main_status.json")
EXPORT_DOMAINS_FILE = PROJECT_DIR / os.getenv("EXPORT_DOMAINS_FILE", "expired_domains_export.csv")
ENV_FILE = PROJECT_DIR / ".env"
MIN_SCRAPE_INTERVAL_SECONDS = 60
MAX_SCRAPE_INTERVAL_SECONDS = 86_400
DEFAULT_SCRAPE_PAGE_LIMIT = 51

scraper_process: subprocess.Popen | None = None
scraper_log_handle: Any | None = None
scraper_lock = threading.RLock()
seo_jobs: dict[str, dict[str, Any]] = {}
seo_jobs_lock = threading.Lock()
wsc_jobs: dict[str, dict[str, Any]] = {}
wsc_jobs_lock = threading.Lock()
nawala_jobs: dict[str, dict[str, Any]] = {}
nawala_jobs_lock = threading.Lock()


def collection():
    client = MongoClient(MONGO_URI)
    col = client[MONGO_DB][MONGO_COLLECTION]
    move_duplicate_domains(client, col)
    col.create_index([("domain", ASCENDING)], unique=True)
    col.create_index([("batch", ASCENDING)])
    col.create_index([("exported_at", DESCENDING)])
    return client, col


def duplicate_collection(client: MongoClient | None = None):
    own_client = client is None
    mongo_client = client or MongoClient(MONGO_URI)
    col = mongo_client[MONGO_DB][MONGO_DUPLICATE_COLLECTION]
    col.create_index([("domain", ASCENDING)])
    col.create_index([("moved_at", DESCENDING)])
    col.create_index([("exported_at", DESCENDING)])
    if own_client:
        return mongo_client, col
    return col


def move_duplicate_domains(client: MongoClient, col: Any) -> int:
    duplicate_groups = list(
        col.aggregate(
            [
                {"$group": {"_id": "$domain", "count": {"$sum": 1}}},
                {"$match": {"count": {"$gt": 1}}},
            ]
        )
    )
    if not duplicate_groups:
        return 0

    duplicates_col = duplicate_collection(client)
    moved = 0
    now = datetime.utcnow()
    for group in duplicate_groups:
        domain = group.get("_id")
        docs = list(
            col.find({"domain": domain}).sort(
                [
                    ("exported_at", DESCENDING),
                    ("updated_at", DESCENDING),
                    ("_id", DESCENDING),
                ]
            )
        )
        for duplicate in docs[1:]:
            source_id = duplicate.pop("_id")
            duplicate["source_id"] = str(source_id)
            duplicate["source_collection"] = MONGO_COLLECTION
            duplicate["moved_at"] = now
            duplicate["duplicate_reason"] = "duplicate_domain"
            duplicates_col.insert_one(duplicate)
            col.delete_one({"_id": source_id})
            moved += 1
    return moved


def seo_ranking_collection():
    client = MongoClient(MONGO_URI)
    col = client[MONGO_DB][SEO_RANKING_COLLECTION]
    col.create_index([("domain", ASCENDING)], unique=True)
    col.create_index([("rank", ASCENDING)])
    col.create_index([("seo_score", DESCENDING)])
    return client, col


def seo_check_collection():
    client = MongoClient(MONGO_URI)
    col = client[MONGO_DB][SEO_COLLECTION]
    col.create_index([("domain", ASCENDING)], unique=True)
    col.create_index([("checked_at", DESCENDING)])
    col.create_index([("seo_score", DESCENDING)])
    col.create_index([("reachable", ASCENDING)])
    return client, col


def serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items() if key not in {"_id", "source_url"}}
    return value


def scraper_status() -> dict[str, Any]:
    with scraper_lock:
        running = scraper_process is not None and scraper_process.poll() is None
        external_pids = find_external_scraper_pids()
        status_pid = read_live_status().get("pid")
        if not external_pids and isinstance(status_pid, int) and is_pid_running(status_pid):
            external_pids = [status_pid]
        effective_running = running or bool(external_pids)
        return {
            "running": effective_running,
            "pid": scraper_process.pid if running and scraper_process is not None else (external_pids[0] if external_pids else None),
            "returncode": scraper_process.poll() if scraper_process is not None else None,
            "script": str(MAIN_SCRIPT),
            "log": str(MAIN_LOG),
        }


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def find_external_scraper_pids() -> list[int]:
    current_pid = os.getpid()
    script_path = str(MAIN_SCRIPT).lower()
    pids: list[int] = []
    if os.name == "nt":
        escaped_script_path = script_path.replace("'", "''")
        command = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and $_.ProcessId -ne $PID -and "
            f"$_.CommandLine.ToLower().Contains('{escaped_script_path}') }} | "
            "ForEach-Object { $_.ProcessId }"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                if pid != current_pid:
                    pids.append(pid)
        return sorted(set(pids))

    try:
        result = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[0].isdigit() and script_path in parts[1].lower():
            pid = int(parts[0])
            if pid != current_pid:
                pids.append(pid)
    return sorted(set(pids))


def terminate_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return
    os.kill(pid, 15)


def mark_scraper_stopped() -> None:
    live = read_live_status()
    live.update({"status": "stopped", "phase": "stopped", "next_run_in_seconds": None})
    live["updated_at"] = datetime.utcnow().isoformat()
    MAIN_STATUS_FILE.write_text(json.dumps(live, indent=2, ensure_ascii=False), encoding="utf-8")


def close_scraper_log() -> None:
    global scraper_log_handle
    if scraper_log_handle is not None:
        try:
            scraper_log_handle.close()
        finally:
            scraper_log_handle = None


def read_main_log(lines: int = 80) -> list[str]:
    if not MAIN_LOG.exists():
        return []
    text = MAIN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return text[-max(1, lines):]


def read_live_status() -> dict[str, Any]:
    if not MAIN_STATUS_FILE.exists():
        return {}
    try:
        loaded = json.loads(MAIN_STATUS_FILE.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError:
        return {}


def parse_status_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def live_status_with_countdown() -> dict[str, Any]:
    live = read_live_status()
    remaining = live.get("next_run_in_seconds")
    is_waiting = str(live.get("status") or "").lower() == "sleeping" or "wait" in str(live.get("phase") or "").lower()
    if is_waiting and isinstance(remaining, (int, float)):
        updated_at = parse_status_datetime(live.get("updated_at"))
        if updated_at is not None:
            elapsed = max(0, int((datetime.utcnow() - updated_at).total_seconds()))
            live["next_run_in_seconds"] = max(0, int(remaining) - elapsed)
    return live


def page_cursor_file() -> Path:
    load_dotenv(ENV_FILE, override=True)
    return PROJECT_DIR / os.getenv("SCRAPE_PAGE_CURSOR_FILE", "page_cursor.json").strip()


def scrape_start_page() -> int:
    try:
        return max(1, int(env_value("SCRAPE_START_PAGE", "1") or "1"))
    except ValueError:
        return 1


def scrape_page_limit() -> int:
    try:
        return max(scrape_start_page(), int(env_value("SCRAPE_DAILY_PAGE_LIMIT", str(DEFAULT_SCRAPE_PAGE_LIMIT)) or DEFAULT_SCRAPE_PAGE_LIMIT))
    except ValueError:
        return DEFAULT_SCRAPE_PAGE_LIMIT


def read_page_cursor() -> dict[str, Any]:
    cursor_file = page_cursor_file()
    page_limit = scrape_page_limit()
    if not cursor_file.exists():
        return {
            "next_page": scrape_start_page(),
            "next_batch": 1,
            "next_url": "",
            "exists": False,
            "at_page_limit": False,
            "page_limit": page_limit,
        }
    try:
        loaded = json.loads(cursor_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        loaded = {}
    cursor = loaded if isinstance(loaded, dict) else {}
    next_page = cursor.get("next_page")
    if not isinstance(next_page, int) or next_page < 1:
        next_page = scrape_start_page()
    next_batch = cursor.get("next_batch")
    if not isinstance(next_batch, int) or next_batch < 1:
        next_batch = 1
    return {
        **cursor,
        "next_page": next_page,
        "next_batch": next_batch,
        "next_url": str(cursor.get("next_url") or ""),
        "exists": True,
        "file": str(cursor_file),
        "at_page_limit": next_page >= page_limit,
        "page_limit": page_limit,
    }


def reset_page_cursor() -> dict[str, Any]:
    cursor_file = page_cursor_file()
    start_page = scrape_start_page()
    now = datetime.utcnow().isoformat()
    data = {
        "next_page": start_page,
        "next_batch": 1,
        "next_url": "",
        "cycle_started_at": now,
        "updated_at": now,
        "reset_reason": "dashboard_start_from_page_1",
    }
    cursor_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    live = read_live_status()
    live.update(
        {
            "page": start_page,
            "batch": 1,
            "url": "",
            "phase": "page_cursor_reset",
            "status": "stopped" if not scraper_status()["running"] else live.get("status", "running"),
            "updated_at": datetime.utcnow().isoformat(),
        }
    )
    MAIN_STATUS_FILE.write_text(json.dumps(live, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"message": "page cursor reset", "cursor": read_page_cursor()}


def export_count() -> int:
    if not EXPORT_DOMAINS_FILE.exists():
        return 0
    with EXPORT_DOMAINS_FILE.open("r", encoding="utf-8-sig", errors="replace") as fh:
        count = sum(1 for line in fh if line.strip())
    return max(0, count - 1)


def domain_counts() -> dict[str, Any]:
    client, col = collection()
    try:
        wsc_config = load_wsc_config()
        wsc_count = client[wsc_config["mongo_db"]][wsc_config["result_collection"]].count_documents({})
        nawala_count = client[wsc_config["mongo_db"]][wsc_config["nawala_final_collection"]].count_documents({})
        db_domains = col.count_documents({})
        unique_domains = len(col.distinct("domain"))
        return {
            "db_domains": db_domains,
            "mongo_domains": db_domains,
            "unique_domains": unique_domains,
            "without_duplicates": unique_domains,
            "duplicate_domains": client[MONGO_DB][MONGO_DUPLICATE_COLLECTION].count_documents({}),
            "duplicate_collection": MONGO_DUPLICATE_COLLECTION,
            "seo_rank_checks": wsc_count,
            "wsc_collection": wsc_config["result_collection"],
            "nawala_check_urls": nawala_count,
            "nawala_checked_urls": nawala_count,
            "nawala_final_collection": wsc_config["nawala_final_collection"],
        }
    finally:
        client.close()


def database_backup_filename() -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"{MONGO_DB}_backup_{stamp}.json.gz"


def database_backup_bytes() -> tuple[str, bytes]:
    client = MongoClient(MONGO_URI)
    try:
        db = client[MONGO_DB]
        collections = []
        for name in sorted(db.list_collection_names()):
            if name.startswith("system."):
                continue
            col = db[name]
            documents = list(col.find({}))
            collections.append(
                {
                    "name": name,
                    "count": len(documents),
                    "documents": documents,
                }
            )
        payload = {
            "format": DB_BACKUP_FORMAT,
            "created_at": datetime.utcnow(),
            "database": MONGO_DB,
            "collections": collections,
        }
        raw = json_util.dumps(payload, indent=2).encode("utf-8")
        return database_backup_filename(), gzip.compress(raw)
    finally:
        client.close()


def safe_collection_name(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    value = name.strip()
    if not value or value.startswith("system.") or "$" in value or "\x00" in value:
        return None
    return value


def ensure_known_indexes(db: Any) -> None:
    db[MONGO_COLLECTION].create_index([("domain", ASCENDING)], unique=True)
    db[MONGO_COLLECTION].create_index([("batch", ASCENDING)])
    db[MONGO_COLLECTION].create_index([("exported_at", DESCENDING)])
    db[MONGO_DUPLICATE_COLLECTION].create_index([("domain", ASCENDING)])
    db[MONGO_DUPLICATE_COLLECTION].create_index([("moved_at", DESCENDING)])
    db[MONGO_DUPLICATE_COLLECTION].create_index([("exported_at", DESCENDING)])
    db[SEO_COLLECTION].create_index([("domain", ASCENDING)], unique=True)
    db[SEO_COLLECTION].create_index([("checked_at", DESCENDING)])
    db[SEO_COLLECTION].create_index([("seo_score", DESCENDING)])
    db[SEO_COLLECTION].create_index([("reachable", ASCENDING)])
    db[SEO_RANKING_COLLECTION].create_index([("domain", ASCENDING)], unique=True)
    db[SEO_RANKING_COLLECTION].create_index([("rank", ASCENDING)])
    db[SEO_RANKING_COLLECTION].create_index([("seo_score", DESCENDING)])
    try:
        wsc_config = load_wsc_config()
        db[wsc_config["result_collection"]].create_index([("domain", ASCENDING)], unique=True)
        db[wsc_config["result_collection"]].create_index([("checked_at", DESCENDING)])
        db[wsc_config["nawala_final_collection"]].create_index([("domain", ASCENDING)], unique=True)
        db[wsc_config["nawala_final_collection"]].create_index([("nawala_checked_at", DESCENDING)])
        db[wsc_config["nawala_final_collection"]].create_index([("nawala_blocked", ASCENDING)])
    except Exception:
        pass


def restore_database_backup(uploaded_bytes: bytes) -> dict[str, Any]:
    try:
        raw = gzip.decompress(uploaded_bytes)
    except (OSError, EOFError) as exc:
        raise ValueError("Upload must be a valid .gz backup file.") from exc

    try:
        payload = json_util.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Backup file does not contain valid JSON data.") from exc

    if not isinstance(payload, dict) or payload.get("format") != DB_BACKUP_FORMAT:
        raise ValueError("Backup file format is not supported.")

    collections = payload.get("collections")
    if not isinstance(collections, list):
        raise ValueError("Backup file is missing collections.")

    prepared: list[tuple[str, list[dict[str, Any]]]] = []
    for item in collections:
        if not isinstance(item, dict):
            raise ValueError("Backup file contains an invalid collection entry.")
        name = safe_collection_name(item.get("name"))
        documents = item.get("documents")
        if name is None or not isinstance(documents, list):
            raise ValueError("Backup file contains an invalid collection name or document list.")
        if not all(isinstance(document, dict) for document in documents):
            raise ValueError(f"Collection {name} contains invalid documents.")
        prepared.append((name, documents))

    client = MongoClient(MONGO_URI)
    try:
        db = client[MONGO_DB]
        restored = []
        for name, documents in prepared:
            col = db[name]
            col.drop()
            if documents:
                col.insert_many(documents, ordered=False)
            restored.append({"collection": name, "documents": len(documents)})
        ensure_known_indexes(db)
        return {
            "ok": True,
            "message": "Database restored.",
            "database": MONGO_DB,
            "source_database": payload.get("database"),
            "created_at": serialize(payload.get("created_at")),
            "collections": restored,
            "total_documents": sum(item["documents"] for item in restored),
        }
    finally:
        client.close()


def main_response(message: str) -> dict[str, Any]:
    status = scraper_status()
    live_status = live_status_with_countdown()
    if not status["running"] and str(live_status.get("status") or "").lower() in {"running", "sleeping"}:
        live_status = {**live_status, "status": "stopped", "phase": "stopped", "next_run_in_seconds": None}
    return {
        "ok": True,
        "message": message,
        **status,
        **domain_counts(),
        "live_status": live_status,
        "page_cursor": read_page_cursor(),
    }


def compact_main_status() -> dict[str, Any]:
    live = live_status_with_countdown()
    cursor = read_page_cursor()
    return {
        "exported_rows": live.get("exported_rows", live.get("last_exported_rows")),
        "batch": live.get("batch", live.get("last_batch")),
        "last_run_started": live.get("last_run_started"),
        "last_exported_rows": live.get("last_exported_rows"),
        "last_batch": live.get("last_batch"),
        "next_run_in_seconds": live.get("next_run_in_seconds"),
        "page_cursor": cursor,
    }


def env_value(name: str, default: str = "") -> str:
    load_dotenv(ENV_FILE, override=True)
    return os.getenv(name, default).strip()


def write_env_value(name: str, value: str) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    replaced = False
    output: list[str] = []
    for line in lines:
        if line.startswith(f"{name}="):
            output.append(f"{name}={value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"{name}={value}")
    ENV_FILE.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.environ[name] = value


def get_scraper_settings() -> dict[str, Any]:
    interval = int(env_value("SCRAPE_INTERVAL_SECONDS", "1800") or "1800")
    live = live_status_with_countdown()
    status = scraper_status()
    effective_interval = live.get("interval_seconds")
    return {
        "scrape_interval_seconds": interval,
        "minimum_seconds": MIN_SCRAPE_INTERVAL_SECONDS,
        "maximum_seconds": MAX_SCRAPE_INTERVAL_SECONDS,
        "running": bool(status["running"]),
        "effective_current_run_seconds": effective_interval,
        "next_run_in_seconds": live.get("next_run_in_seconds"),
        "restart_required": bool(status["running"] and effective_interval != interval),
        "page_cursor": read_page_cursor(),
    }


def update_scraper_interval(seconds: int) -> dict[str, Any]:
    normalized = max(MIN_SCRAPE_INTERVAL_SECONDS, min(MAX_SCRAPE_INTERVAL_SECONDS, int(seconds)))
    write_env_value("SCRAPE_INTERVAL_SECONDS", str(normalized))
    data = get_scraper_settings()
    data["saved"] = True
    data["requested_seconds"] = seconds
    data["scrape_interval_seconds"] = normalized
    data["message"] = (
        "Scrape interval saved. Restart the scraper for this running process to use the new interval."
        if data["restart_required"]
        else "Scrape interval saved."
    )
    return data


def env_bool_value(name: str, default: bool = False) -> bool:
    value = env_value(name, "true" if default else "false").lower()
    return value in {"1", "true", "yes", "y", "on"}


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def get_telegram_settings() -> dict[str, Any]:
    token = env_value("TELEGRAM_BOT_TOKEN", "")
    chat_id = env_value("TELEGRAM_CHAT_ID", "")
    return {
        "enabled": env_bool_value("TELEGRAM_NOTIFICATIONS_ENABLED", False),
        "bot_token_set": bool(token),
        "bot_token_masked": mask_secret(token),
        "chat_id": chat_id,
        "notify_progress": env_bool_value("TELEGRAM_NOTIFY_PROGRESS", True),
    }


def update_telegram_settings(payload: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(payload.get("enabled"))
    notify_progress = bool(payload.get("notify_progress", True))
    chat_id = str(payload.get("chat_id") or "").strip()
    token = str(payload.get("bot_token") or "").strip()
    keep_existing_token = bool(payload.get("keep_existing_token"))

    write_env_value("TELEGRAM_NOTIFICATIONS_ENABLED", "true" if enabled else "false")
    write_env_value("TELEGRAM_NOTIFY_PROGRESS", "true" if notify_progress else "false")
    write_env_value("TELEGRAM_CHAT_ID", chat_id)
    if token or not keep_existing_token:
        write_env_value("TELEGRAM_BOT_TOKEN", token)

    data = get_telegram_settings()
    data["saved"] = True
    data["message"] = "Telegram settings saved."
    return data


def send_telegram_message(message: str) -> dict[str, Any]:
    token = env_value("TELEGRAM_BOT_TOKEN", "")
    chat_id = env_value("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise ValueError("Telegram bot token and chat ID are required.")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise ValueError(f"Telegram send failed: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Telegram returned an invalid response.") from exc
    if not data.get("ok"):
        raise ValueError(str(data.get("description") or "Telegram send failed."))
    return {"ok": True, "message": "Telegram message sent."}


def telegram_value(value: Any) -> str:
    return "-" if value in {None, ""} else str(value)


def telegram_stage(status: dict[str, Any]) -> tuple[str, str]:
    phase = str(status.get("phase") or "").lower()
    current_status = str(status.get("status") or "").lower()
    if "connect" in phase or "browser" in phase or phase == "started":
        return "1/8", "Starting browser session"
    if "opening_page" in phase or "starting_scrape" in phase:
        return "2/8", "Opening expired-domains page"
    if "filter" in phase:
        return "3/8", "Applying site filters"
    if "export" in phase or "copy" in phase:
        return "4/8", "Exporting domains"
    if "scrape_complete" in phase:
        return "5/8", "Scrape cycle complete"
    if "website_seo_checker" in phase:
        return "6/8", "Website SEO Checker"
    if "nawala" in phase:
        return "7/8", "Nawala block check"
    if "wait" in phase or current_status == "sleeping":
        return "8/8", "Waiting for next run"
    if current_status in {"error", "stopped", "complete"}:
        return "Final", current_status.title()
    return "-", str(status.get("phase") or current_status or "Unknown")


def telegram_dashboard_message(live: dict[str, Any], counts: dict[str, Any]) -> str:
    stage_number, stage_name = telegram_stage(live)
    wsc_errors = live.get("wsc_errors")
    wsc_error_count = len(wsc_errors) if isinstance(wsc_errors, list) else (1 if live.get("wsc_error") else 0)
    return "\n".join(
        [
            "Expired Domains dashboard test",
            f"Stage: {stage_number} - {stage_name}",
            "",
            "Run status",
            f"- Status: {telegram_value(live.get('status'))}",
            f"- Phase: {telegram_value(live.get('phase'))}",
            "",
            "Scrape progress",
            f"- Page: {telegram_value(live.get('page'))}",
            f"- Batch: {telegram_value(live.get('batch', live.get('last_batch')))}",
            f"- Exported rows: {telegram_value(live.get('exported_rows', live.get('last_exported_rows')))}",
            "",
            "Database",
            f"- Domains: {telegram_value(counts.get('db_domains'))}",
            f"- Unique: {telegram_value(counts.get('unique_domains'))}",
            f"- Duplicates: {telegram_value(counts.get('duplicate_domains'))}",
            f"- SEO rank checks: {telegram_value(counts.get('seo_rank_checks'))}",
            f"- Nawala URLs: {telegram_value(counts.get('nawala_check_urls'))}",
            "",
            "Website SEO Checker",
            f"- Batch: {telegram_value(live.get('wsc_batch'))} / {telegram_value(live.get('wsc_total_batches'))}",
            f"- Submitted: {telegram_value(live.get('wsc_submitted'))}",
            f"- Saved: {telegram_value(live.get('wsc_saved'))}",
            f"- Errors: {wsc_error_count}",
            "",
            "Nawala check",
            f"- Submitted: {telegram_value(live.get('nawala_submitted'))}",
            f"- Saved: {telegram_value(live.get('nawala_final_saved', live.get('nawala_saved')))}",
            f"- Blocked: {telegram_value(live.get('nawala_blocked'))}",
            "",
            "Schedule",
            f"- Interval: {telegram_value(live.get('interval_seconds'))}s",
            f"- Next run in: {telegram_value(live.get('next_run_in_seconds'))}s",
            f"- Updated: {telegram_value(live.get('updated_at'))}",
        ]
    )


def test_telegram_notification() -> dict[str, Any]:
    live = live_status_with_countdown()
    message = telegram_dashboard_message(live, domain_counts())
    return send_telegram_message(message)


def start_main_process() -> dict[str, Any]:
    global scraper_process, scraper_log_handle
    with scraper_lock:
        if scraper_process is not None and scraper_process.poll() is None:
            return {"message": "start successful"}

        close_scraper_log()
        scraper_log_handle = MAIN_LOG.open("a", encoding="utf-8")
        scraper_log_handle.write(f"\n[{datetime.utcnow().isoformat()}] starting main.py\n")
        scraper_log_handle.flush()
        scraper_process = subprocess.Popen(
            [sys.executable, str(MAIN_SCRIPT)],
            cwd=str(PROJECT_DIR),
            stdout=scraper_log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return {"message": "start successful"}


def stop_main_process() -> dict[str, Any]:
    global scraper_process
    with scraper_lock:
        external_pids = find_external_scraper_pids()
        if scraper_process is None or scraper_process.poll() is not None:
            for pid in external_pids:
                terminate_pid(pid)
            close_scraper_log()
            mark_scraper_stopped()
            return {"message": "stop successful"}

        scraper_process.terminate()
        try:
            scraper_process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            scraper_process.kill()
            scraper_process.wait(timeout=10)
        for pid in external_pids:
            if pid != scraper_process.pid:
                terminate_pid(pid)
        close_scraper_log()
        mark_scraper_stopped()
        return {"message": "stop successful"}


def restart_main_process() -> dict[str, Any]:
    stop_main_process()
    result = start_main_process()
    result["message"] = "restart successful"
    return result


def start_main_process_from_first_page() -> dict[str, Any]:
    reset = reset_page_cursor()
    stop_main_process()
    result = start_main_process()
    result["message"] = "start from page 1 successful"
    result["cursor"] = reset["cursor"]
    return result


def get_domains(
    limit: int = 100,
    skip: int = 0,
    batch: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    client, col = collection()
    try:
        query: dict[str, Any] = {}
        if batch is not None:
            query["batch"] = batch
        exported_at_query: dict[str, datetime] = {}
        if date_from is not None:
            exported_at_query["$gte"] = date_from
        if date_to is not None:
            exported_at_query["$lte"] = date_to
        if exported_at_query:
            query["exported_at"] = exported_at_query
        total = col.count_documents(query)
        unique_total = len(col.distinct("domain", query))
        rows = list(
            col.find(query, {"_id": 0})
            .sort([("batch", ASCENDING), ("exported_at", ASCENDING), ("domain", ASCENDING)])
            .skip(skip)
            .limit(limit)
        )
        return {
            "total": total,
            "unique_total": unique_total,
            "without_duplicates": unique_total,
            "duplicates_in_result": max(0, total - unique_total),
            "limit": limit,
            "skip": skip,
            "items": serialize(rows),
        }
    finally:
        client.close()


def get_duplicate_domains(limit: int = 100, skip: int = 0, domain: str | None = None) -> dict[str, Any]:
    client, col = duplicate_collection()
    try:
        query: dict[str, Any] = {}
        if domain:
            query["domain"] = {"$regex": domain.strip().lower(), "$options": "i"}
        total = col.count_documents(query)
        rows = list(
            col.find(query, {"_id": 0})
            .sort([("moved_at", DESCENDING), ("exported_at", DESCENDING), ("domain", ASCENDING)])
            .skip(skip)
            .limit(limit)
        )
        return {"total": total, "limit": limit, "skip": skip, "items": serialize(rows)}
    finally:
        client.close()


def get_domain(domain: str) -> dict[str, Any] | None:
    client, col = collection()
    try:
        row = col.find_one({"domain": domain.lower()}, {"_id": 0})
        return serialize(row) if row else None
    finally:
        client.close()


def get_batches() -> dict[str, Any]:
    client, col = collection()
    try:
        pipeline = [
            {"$group": {"_id": "$batch", "count": {"$sum": 1}, "exported_at": {"$max": "$exported_at"}}},
            {"$sort": {"_id": 1}},
        ]
        rows = [
            {"batch": row["_id"], "count": row["count"], "exported_at": row.get("exported_at")}
            for row in col.aggregate(pipeline)
        ]
        return {"items": serialize(rows)}
    finally:
        client.close()


def set_seo_job(job_id: str, **values: Any) -> None:
    with seo_jobs_lock:
        current = seo_jobs.get(job_id, {})
        current.update(values)
        seo_jobs[job_id] = current


def run_seo_job(job_id: str, limit: int, force: bool, recheck_days: int) -> None:
    try:
        set_seo_job(job_id, status="running", started_at=datetime.utcnow(), message="SEO check running")
        config = load_seo_config()
        domains = load_domains(config, limit, recheck_days, force_all=force)
        results = asyncio.run(run_checks_incremental(config, domains))
        save_results(config, results)
        ranked = rebuild_rankings(config)
        reachable = sum(1 for result in results if result.get("reachable"))
        set_seo_job(
            job_id,
            status="complete",
            completed_at=datetime.utcnow(),
            checked=len(results),
            reachable=reachable,
            unreachable=len(results) - reachable,
            rankings=ranked,
            message="SEO check complete",
        )
    except Exception as exc:
        set_seo_job(job_id, status="error", completed_at=datetime.utcnow(), error=str(exc), message="SEO check failed")


def start_seo_check(limit: int = 2000, force: bool = True, recheck_days: int = 14) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    set_seo_job(
        job_id,
        id=job_id,
        status="queued",
        created_at=datetime.utcnow(),
        limit=limit,
        force=force,
        recheck_days=recheck_days,
    )
    threading.Thread(target=run_seo_job, args=(job_id, limit, force, recheck_days), daemon=True).start()
    return {"job_id": job_id, "status": "queued", "limit": limit, "force": force}


def get_seo_job(job_id: str) -> dict[str, Any] | None:
    with seo_jobs_lock:
        job = seo_jobs.get(job_id)
        return serialize(job) if job else None


def get_seo_rankings(limit: int = 100, skip: int = 0, reachable: bool | None = None) -> dict[str, Any]:
    client, col = seo_ranking_collection()
    try:
        query: dict[str, Any] = {}
        if reachable is not None:
            query["reachable"] = reachable
        total = col.count_documents(query)
        rows = list(col.find(query, {"_id": 0}).sort([("rank", ASCENDING)]).skip(skip).limit(limit))
        return {"total": total, "limit": limit, "skip": skip, "items": serialize(rows)}
    finally:
        client.close()


def get_seo_checks(
    limit: int = 100,
    skip: int = 0,
    reachable: bool | None = None,
    domain: str | None = None,
    min_score: int | None = None,
    max_score: int | None = None,
) -> dict[str, Any]:
    client, col = seo_check_collection()
    try:
        query: dict[str, Any] = {}
        if reachable is not None:
            query["reachable"] = reachable
        if domain:
            query["domain"] = {"$regex": domain.strip().lower(), "$options": "i"}
        score_query: dict[str, int] = {}
        if min_score is not None:
            score_query["$gte"] = min_score
        if max_score is not None:
            score_query["$lte"] = max_score
        if score_query:
            query["seo_score"] = score_query

        total = col.count_documents(query)
        rows = list(
            col.find(query, {"_id": 0})
            .sort([("checked_at", DESCENDING), ("seo_score", DESCENDING), ("domain", ASCENDING)])
            .skip(skip)
            .limit(limit)
        )
        return {"total": total, "limit": limit, "skip": skip, "items": serialize(rows)}
    finally:
        client.close()


def as_percent(value: Any) -> str:
    if value is None or value == "":
        return "n/a"
    if isinstance(value, bool):
        return "100%" if value else "0%"
    if isinstance(value, (int, float)):
        return f"{int(value)}%"
    return str(value)


def sheet_row(seo: dict[str, Any], domain_doc: dict[str, Any] | None = None) -> dict[str, Any]:
    data = (domain_doc or {}).get("data", {}) if domain_doc else {}
    title = seo.get("title") or "n/a"
    score = int(seo.get("seo_score") or 0)
    return {
        "URL": seo.get("domain") or (domain_doc or {}).get("domain", ""),
        "Title": title,
        "DA": data.get("DA", "n/a"),
        "PA": data.get("PA", "n/a"),
        "TBL": data.get("BL", data.get("TBL", seo.get("link_count", "n/a"))),
        "QBL": data.get("DP", data.get("QBL", "n/a")),
        "Q/T": f"{seo.get('title_length', 0)}/70" if title != "n/a" else "n/a",
        "OS": as_percent(score),
        "MT": "10/10" if seo.get("meta_description_ok") else "0/10",
        "SS": "100%" if seo.get("sitemap_xml") else "0%",
        "DH": "",
    }


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    headers = ["URL", "Title", "DA", "PA", "TBL", "QBL", "Q/T", "OS", "MT", "SS", "DH"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def get_seo_sheet(
    limit: int = 100,
    skip: int = 0,
    reachable: bool | None = None,
    domain: str | None = None,
    min_score: int | None = None,
) -> dict[str, Any]:
    client = MongoClient(MONGO_URI)
    try:
        db = client[MONGO_DB]
        seo_col = db[SEO_COLLECTION]
        domain_col = db[MONGO_COLLECTION]

        query: dict[str, Any] = {}
        if reachable is not None:
            query["reachable"] = reachable
        if domain:
            query["domain"] = {"$regex": domain.strip().lower(), "$options": "i"}
        if min_score is not None:
            query["seo_score"] = {"$gte": min_score}

        total = seo_col.count_documents(query)
        seo_rows = list(
            seo_col.find(query, {"_id": 0})
            .sort([("seo_score", DESCENDING), ("reachable", DESCENDING), ("domain", ASCENDING)])
            .skip(skip)
            .limit(limit)
        )
        domains = [row.get("domain") for row in seo_rows if row.get("domain")]
        domain_docs = {
            row["domain"]: row
            for row in domain_col.find({"domain": {"$in": domains}}, {"_id": 0})
            if row.get("domain")
        }
        rows = [sheet_row(row, domain_docs.get(row.get("domain"))) for row in seo_rows]
        return {
            "total": total,
            "limit": limit,
            "skip": skip,
            "columns": ["URL", "Title", "DA", "PA", "TBL", "QBL", "Q/T", "OS", "MT", "SS", "DH"],
            "items": serialize(rows),
        }
    finally:
        client.close()


def set_wsc_job(job_id: str, **values: Any) -> None:
    with wsc_jobs_lock:
        current = wsc_jobs.get(job_id, {})
        current.update(values)
        wsc_jobs[job_id] = current


def run_wsc_job(job_id: str, limit: int, force: bool) -> None:
    try:
        set_wsc_job(job_id, status="running", started_at=datetime.utcnow(), message="Website SEO Checker running")
        result = run_wsc_next_batch(limit=min(limit, 50), force=force)
        set_wsc_job(
            job_id,
            status="complete",
            completed_at=datetime.utcnow(),
            message="Website SEO Checker complete",
            **result,
        )
    except Exception as exc:
        set_wsc_job(job_id, status="error", completed_at=datetime.utcnow(), error=str(exc), message="Website SEO Checker failed")


def start_wsc_check(limit: int = 50, force: bool = False) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    normalized_limit = max(1, min(limit, 50))
    set_wsc_job(
        job_id,
        id=job_id,
        status="queued",
        created_at=datetime.utcnow(),
        limit=normalized_limit,
        force=force,
    )
    threading.Thread(target=run_wsc_job, args=(job_id, normalized_limit, force), daemon=True).start()
    return {"job_id": job_id, "status": "queued", "limit": normalized_limit, "force": force}


def get_wsc_job(job_id: str) -> dict[str, Any] | None:
    with wsc_jobs_lock:
        job = wsc_jobs.get(job_id)
        return serialize(job) if job else None


def set_nawala_job(job_id: str, **values: Any) -> None:
    with nawala_jobs_lock:
        current = nawala_jobs.get(job_id, {})
        current.update(values)
        nawala_jobs[job_id] = current


def run_nawala_job(job_id: str, limit: int, force: bool) -> None:
    try:
        set_nawala_job(job_id, status="running", started_at=datetime.utcnow(), message="Nawala check running")
        result = run_nawala_for_saved_wsc(limit=limit, force=force)
        set_nawala_job(
            job_id,
            status="complete",
            completed_at=datetime.utcnow(),
            message="Nawala check complete",
            **result,
        )
    except Exception as exc:
        set_nawala_job(job_id, status="error", completed_at=datetime.utcnow(), error=str(exc), message="Nawala check failed")


def start_nawala_check(limit: int = 1500, force: bool = False) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    normalized_limit = max(1, min(limit, 5000))
    set_nawala_job(
        job_id,
        id=job_id,
        status="queued",
        created_at=datetime.utcnow(),
        limit=normalized_limit,
        force=force,
    )
    threading.Thread(target=run_nawala_job, args=(job_id, normalized_limit, force), daemon=True).start()
    return {"job_id": job_id, "status": "queued", "limit": normalized_limit, "force": force}


def get_nawala_job(job_id: str) -> dict[str, Any] | None:
    with nawala_jobs_lock:
        job = nawala_jobs.get(job_id)
        return serialize(job) if job else None


def wsc_collection():
    config = load_wsc_config()
    client = MongoClient(config["mongo_uri"])
    col = client[config["mongo_db"]][config["result_collection"]]
    col.create_index([("domain", ASCENDING)], unique=True)
    col.create_index([("checked_at", DESCENDING)])
    return client, col


def nawala_final_collection():
    config = load_wsc_config()
    client = MongoClient(config["mongo_uri"])
    col = client[config["mongo_db"]][config["nawala_final_collection"]]
    col.create_index([("domain", ASCENDING)], unique=True)
    col.create_index([("nawala_checked_at", DESCENDING)])
    col.create_index([("nawala_blocked", ASCENDING)])
    return client, col


def numeric_value_expression(field: str) -> dict[str, Any]:
    raw_value = {
        "$toUpper": {
            "$trim": {
                "input": {
                    "$replaceAll": {
                        "input": {
                            "$replaceAll": {
                                "input": {"$toString": f"${field}"},
                                "find": "%",
                                "replacement": "",
                            }
                        },
                        "find": ",",
                        "replacement": "",
                    }
                }
            }
        }
    }
    numeric_text = {
        "$replaceAll": {
            "input": {
                "$replaceAll": {
                    "input": {
                        "$replaceAll": {
                            "input": raw_value,
                            "find": "B",
                            "replacement": "",
                        }
                    },
                    "find": "M",
                    "replacement": "",
                }
            },
            "find": "K",
            "replacement": "",
        }
    }
    base_number = {"$convert": {"input": numeric_text, "to": "double", "onError": None, "onNull": None}}
    multiplier = {
        "$switch": {
            "branches": [
                {"case": {"$regexMatch": {"input": raw_value, "regex": "B$"}}, "then": 1_000_000_000},
                {"case": {"$regexMatch": {"input": raw_value, "regex": "M$"}}, "then": 1_000_000},
                {"case": {"$regexMatch": {"input": raw_value, "regex": "K$"}}, "then": 1_000},
            ],
            "default": 1,
        }
    }
    return {"$multiply": [base_number, multiplier]}


def numeric_range_stage(field: str, alias: str, minimum: int | None, maximum: int | None) -> list[dict[str, Any]]:
    if minimum is None and maximum is None:
        return []
    match: dict[str, int] = {}
    if minimum is not None:
        match["$gte"] = minimum
    if maximum is not None:
        match["$lte"] = maximum
    return [
        {
            "$addFields": {
                alias: numeric_value_expression(field)
            }
        },
        {"$match": {alias: match}},
    ]


def get_wsc_results(
    limit: int = 100,
    skip: int = 0,
    domain: str | None = None,
    da_min: int | None = None,
    da_max: int | None = None,
    pa_min: int | None = None,
    pa_max: int | None = None,
    tbl_min: int | None = None,
    tbl_max: int | None = None,
    qbl_min: int | None = None,
    qbl_max: int | None = None,
    qt_min: int | None = None,
    qt_max: int | None = None,
    os_min: int | None = None,
    os_max: int | None = None,
    ss_min: int | None = None,
    ss_max: int | None = None,
) -> dict[str, Any]:
    config = load_wsc_config()
    client, col = wsc_collection()
    try:
        query: dict[str, Any] = {}
        if domain:
            query["domain"] = {"$regex": domain.strip().lower(), "$options": "i"}
        pipeline: list[dict[str, Any]] = [{"$match": query}]
        pipeline.extend(numeric_range_stage("DA", "_da_num", da_min, da_max))
        pipeline.extend(numeric_range_stage("PA", "_pa_num", pa_min, pa_max))
        pipeline.extend(numeric_range_stage("TBL", "_tbl_num", tbl_min, tbl_max))
        pipeline.extend(numeric_range_stage("QBL", "_qbl_num", qbl_min, qbl_max))
        pipeline.extend(numeric_range_stage("Q/T", "_qt_num", qt_min, qt_max))
        pipeline.extend(numeric_range_stage("OS", "_os_num", os_min, os_max))
        pipeline.extend(numeric_range_stage("SS", "_ss_num", ss_min, ss_max))

        count_rows = list([*col.aggregate([*pipeline, {"$count": "total"}])])
        total = int(count_rows[0]["total"]) if count_rows else 0
        rows = list(
            col.aggregate(
                [
                    *pipeline,
                    {"$sort": {"checked_at": DESCENDING, "domain": ASCENDING}},
                    {"$skip": skip},
                    {"$limit": limit},
                    {
                        "$project": {
                            "_id": 0,
                            "_da_num": 0,
                            "_pa_num": 0,
                            "_tbl_num": 0,
                            "_qbl_num": 0,
                            "_qt_num": 0,
                            "_os_num": 0,
                            "_ss_num": 0,
                        }
                    },
                ]
            )
        )
        return {"total": total, "limit": limit, "skip": skip, "collection": config["result_collection"], "items": serialize(rows)}
    finally:
        client.close()


def get_nawala_results(
    limit: int = 100,
    skip: int = 0,
    domain: str | None = None,
    blocked: bool | None = None,
    ss_min: int | None = None,
    ss_max: int | None = None,
) -> dict[str, Any]:
    config = load_wsc_config()
    client, col = nawala_final_collection()
    try:
        query: dict[str, Any] = {}
        if domain:
            query["domain"] = {"$regex": domain.strip().lower(), "$options": "i"}
        if blocked is not None:
            query["nawala_blocked"] = blocked
        pipeline: list[dict[str, Any]] = [{"$match": query}]
        pipeline.extend(numeric_range_stage("SS", "_ss_num", ss_min, ss_max))
        count_rows = list(col.aggregate([*pipeline, {"$count": "total"}]))
        total = int(count_rows[0]["total"]) if count_rows else 0
        full_total = col.count_documents({})
        rows = list(
            col.aggregate(
                [
                    *pipeline,
                    {"$sort": {"nawala_checked_at": DESCENDING, "domain": ASCENDING}},
                    {"$skip": skip},
                    {"$limit": limit},
                    {"$project": {"_id": 0, "_ss_num": 0}},
                ]
            )
        )
        return {
            "total": total,
            "full_total": full_total,
            "limit": limit,
            "skip": skip,
            "collection": config["nawala_final_collection"],
            "items": serialize(rows),
        }
    finally:
        client.close()


def get_seo_checker_false_results(
    limit: int = 1000,
    skip: int = 0,
    tbl_min: int | None = None,
    tbl_max: int | None = None,
    qbl_min: int | None = None,
    qbl_max: int | None = None,
    qt_min: int | None = None,
    qt_max: int | None = None,
    ss_min: int | None = 1,
    ss_max: int | None = 1,
    checked_from: datetime | None = None,
    checked_to: datetime | None = None,
    checked_filter: str = "none",
) -> dict[str, Any]:
    config = load_wsc_config()
    client, col = nawala_final_collection()
    try:
        query = {"nawala_blocked": False}
        checked_query: dict[str, datetime] = {}
        if checked_from is not None:
            checked_query["$gte"] = checked_from
        if checked_to is not None:
            checked_query["$lte"] = checked_to
        if checked_query:
            query["nawala_checked_at"] = checked_query
        pipeline: list[dict[str, Any]] = [{"$match": query}]
        pipeline.extend(numeric_range_stage("TBL", "_tbl_num", tbl_min, tbl_max))
        pipeline.extend(numeric_range_stage("QBL", "_qbl_num", qbl_min, qbl_max))
        pipeline.extend(numeric_range_stage("Q/T", "_qt_num", qt_min, qt_max))
        pipeline.extend(numeric_range_stage("SS", "_ss_num", ss_min, ss_max))
        count_rows = list(col.aggregate([*pipeline, {"$count": "total"}]))
        total = int(count_rows[0]["total"]) if count_rows else 0
        rows = list(
            col.aggregate(
                [
                    *pipeline,
                    {"$sort": {"nawala_checked_at": DESCENDING, "domain": ASCENDING}},
                    {"$skip": skip},
                    {"$limit": limit},
                    {"$project": {"_id": 0, "_tbl_num": 0, "_qbl_num": 0, "_qt_num": 0, "_ss_num": 0}},
                ]
            )
        )
        return {
            "total": total,
            "limit": limit,
            "skip": skip,
            "collection": config["nawala_final_collection"],
            "checked_from": serialize(checked_from),
            "checked_to": serialize(checked_to),
            "checked_filter": checked_filter,
            "items": serialize(rows),
        }
    finally:
        client.close()
