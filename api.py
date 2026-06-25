import os
import threading
import uuid
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Response, Security
from fastapi.security import APIKeyHeader
from pymongo import ASCENDING, DESCENDING, MongoClient
import csv
import io

from seo_checker import load_config as load_seo_config
from seo_checker import load_domains, rebuild_rankings, run_checks_incremental, save_results


load_dotenv(override=True)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017").strip()
MONGO_DB = os.getenv("MONGO_DB", "expired_domains_db").strip()
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "expired_domains").strip()
API_KEY = os.getenv("API_KEY", "").strip()
SEO_COLLECTION = os.getenv("SEO_COLLECTION", "domain_seo_checks").strip()
SEO_RANKING_COLLECTION = os.getenv("SEO_RANKING_COLLECTION", "domain_seo_rankings").strip()

app = FastAPI(title="Expired Domains API", version="1.0.0")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
seo_jobs: dict[str, dict[str, Any]] = {}
seo_jobs_lock = threading.Lock()


def require_api_key(api_key: str | None = Security(api_key_header)) -> None:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY is not configured")
    if api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def collection():
    client = MongoClient(MONGO_URI)
    col = client[MONGO_DB][MONGO_COLLECTION]
    col.create_index([("domain", ASCENDING)], unique=True)
    col.create_index([("batch", ASCENDING)])
    col.create_index([("exported_at", DESCENDING)])
    return client, col


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
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def set_seo_job(job_id: str, **values: Any) -> None:
    with seo_jobs_lock:
        job = seo_jobs.setdefault(job_id, {})
        job.update(values)


def run_seo_job(job_id: str, limit: int, force: bool, recheck_days: int) -> None:
    import asyncio

    set_seo_job(job_id, status="running", started_at=datetime.utcnow(), message="Loading domains")
    try:
        config = load_seo_config()
        domains = load_domains(config, limit, recheck_days, force_all=force)
        set_seo_job(job_id, total=len(domains), message=f"Checking {len(domains)} domains")
        if not domains:
            ranked = rebuild_rankings(config)
            set_seo_job(
                job_id,
                status="complete",
                completed_at=datetime.utcnow(),
                checked=0,
                reachable=0,
                unreachable=0,
                rankings=ranked,
                message="No domains needed SEO checks",
            )
            return

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


@app.get("/health")
def health() -> dict[str, Any]:
    client, col = collection()
    try:
        return {
            "ok": True,
            "database": MONGO_DB,
            "collection": MONGO_COLLECTION,
            "documents": col.count_documents({}),
        }
    finally:
        client.close()


@app.get("/domains")
def get_domains(
    _: None = Depends(require_api_key),
    limit: int = Query(100, ge=1, le=1000),
    skip: int = Query(0, ge=0),
    batch: int | None = Query(None, ge=1),
) -> dict[str, Any]:
    client, col = collection()
    try:
        query: dict[str, Any] = {}
        if batch is not None:
            query["batch"] = batch
        total = col.count_documents(query)
        rows = list(
            col.find(query, {"_id": 0})
            .sort([("batch", ASCENDING), ("exported_at", ASCENDING), ("domain", ASCENDING)])
            .skip(skip)
            .limit(limit)
        )
        return {"total": total, "limit": limit, "skip": skip, "items": serialize(rows)}
    finally:
        client.close()


@app.get("/domains/{domain}")
def get_domain(domain: str, _: None = Depends(require_api_key)) -> dict[str, Any]:
    client, col = collection()
    try:
        row = col.find_one({"domain": domain.lower()}, {"_id": 0})
        if not row:
            raise HTTPException(status_code=404, detail="Domain not found")
        return serialize(row)
    finally:
        client.close()


@app.get("/batches")
def get_batches(_: None = Depends(require_api_key)) -> dict[str, Any]:
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


@app.post("/seo/check")
def start_seo_check(
    background_tasks: BackgroundTasks,
    _: None = Depends(require_api_key),
    limit: int = Query(2000, ge=1, le=10000),
    force: bool = Query(True),
    recheck_days: int = Query(14, ge=0),
) -> dict[str, Any]:
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
    background_tasks.add_task(run_seo_job, job_id, limit, force, recheck_days)
    return {"job_id": job_id, "status": "queued", "limit": limit, "force": force}


@app.get("/seo/jobs/{job_id}")
def get_seo_job(job_id: str, _: None = Depends(require_api_key)) -> dict[str, Any]:
    with seo_jobs_lock:
        job = seo_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="SEO job not found")
        return serialize(job)


@app.get("/seo/rankings")
def get_seo_rankings(
    _: None = Depends(require_api_key),
    limit: int = Query(100, ge=1, le=1000),
    skip: int = Query(0, ge=0),
    reachable: bool | None = Query(None),
) -> dict[str, Any]:
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


@app.get("/seo/checks")
def get_seo_checks(
    _: None = Depends(require_api_key),
    limit: int = Query(100, ge=1, le=1000),
    skip: int = Query(0, ge=0),
    reachable: bool | None = Query(None),
    domain: str | None = Query(None),
    min_score: int | None = Query(None, ge=0, le=100),
    max_score: int | None = Query(None, ge=0, le=100),
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


@app.get("/seo/sheet", response_model=None)
def get_seo_sheet(
    _: None = Depends(require_api_key),
    limit: int = Query(100, ge=1, le=5000),
    skip: int = Query(0, ge=0),
    reachable: bool | None = Query(None),
    domain: str | None = Query(None),
    min_score: int | None = Query(None, ge=0, le=100),
    format: str = Query("json", pattern="^(json|csv)$"),
):
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

        if format == "csv":
            return Response(
                content=rows_to_csv(rows),
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="seo_sheet.csv"'},
            )

        columns = ["URL", "Title", "DA", "PA", "TBL", "QBL", "Q/T", "OS", "MT", "SS", "DH"]
        return {"total": total, "limit": limit, "skip": skip, "columns": columns, "items": serialize(rows)}
    finally:
        client.close()
