import json
import os
import secrets
from datetime import datetime, timedelta, time
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import services
from .openapi import openapi_schema


DASHBOARD_SESSION_KEY = "dashboard_authenticated"


def dashboard_username() -> str:
    return os.getenv("DASHBOARD_USERNAME", "admin").strip() or "admin"


def dashboard_password() -> str:
    return os.getenv("DASHBOARD_PASSWORD", services.API_KEY).strip()


def dashboard_logged_in(request: HttpRequest) -> bool:
    return bool(request.session.get(DASHBOARD_SESSION_KEY))


def require_dashboard_login(view_func):
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any):
        if not dashboard_logged_in(request):
            return redirect(f"/login/?next={request.get_full_path()}")
        return view_func(request, *args, **kwargs)

    return wrapped


@require_dashboard_login
def dashboard(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "dashboard/index.html",
        {"api_key_json": json.dumps(services.API_KEY), "dashboard_username": dashboard_username()},
    )


@require_dashboard_login
def swagger_docs(request: HttpRequest) -> HttpResponse:
    return render(request, "dashboard/swagger.html")


@require_GET
@require_dashboard_login
def api_schema(request: HttpRequest) -> JsonResponse:
    return JsonResponse(openapi_schema())


@csrf_exempt
def login_view(request: HttpRequest) -> HttpResponse:
    if dashboard_logged_in(request):
        return redirect(request.GET.get("next") or "/")

    error = ""
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        if secrets.compare_digest(username, dashboard_username()) and secrets.compare_digest(password, dashboard_password()):
            request.session[DASHBOARD_SESSION_KEY] = True
            request.session["dashboard_username"] = username
            return redirect(request.POST.get("next") or "/")
        error = "Invalid username or password."

    return render(request, "dashboard/login.html", {"error": error, "next": request.GET.get("next", "/")})


@require_GET
def logout_view(request: HttpRequest) -> HttpResponse:
    request.session.flush()
    return redirect("/login/")


def parse_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value not in {None, ""} else default
    except ValueError:
        parsed = default
    return max(minimum, min(maximum, parsed))


def parse_bool(value: str | None) -> bool | None:
    if value in {None, ""}:
        return None
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def request_value(request: HttpRequest, query_name: str, header_name: str) -> str | None:
    return request.headers.get(header_name) or request.GET.get(query_name)


def request_int(
    request: HttpRequest,
    query_name: str,
    header_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int | None:
    value = request_value(request, query_name, header_name)
    return parse_int(value, default, minimum, maximum) if value not in {None, ""} else None


def parse_date_start(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), time.min)
    except ValueError:
        return None


def parse_date_end(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), time.max)
    except ValueError:
        return None


def parse_time_value(value: str | None, default: time) -> time | None:
    if not value:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            pass
    return default


def parse_datetime_value(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def request_checked_range(request: HttpRequest) -> tuple[datetime | None, datetime | None, str]:
    last_hours_value = request.headers.get("X-Last-Hours") or request.GET.get("last_hours")
    last_hours = parse_int(last_hours_value, 24, 0, 24 * 365) if last_hours_value not in {None, ""} else 24
    if last_hours > 0:
        checked_to = datetime.utcnow()
        return checked_to - timedelta(hours=last_hours), checked_to, f"last_{last_hours}_hours"

    checked_from = parse_datetime_value(request.headers.get("X-Checked-From") or request.GET.get("checked_from"))
    checked_to = parse_datetime_value(request.headers.get("X-Checked-To") or request.GET.get("checked_to"))
    if checked_from or checked_to:
        return checked_from, checked_to, "checked_datetime"

    date_from = request.headers.get("X-Date-From") or request.GET.get("date_from")
    date_to = request.headers.get("X-Date-To") or request.GET.get("date_to")
    if not date_from and not date_to:
        return None, None, "none"

    try:
        start_date = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else None
        end_date = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else start_date
    except ValueError:
        return None, None, "none"
    if start_date is None and end_date is not None:
        start_date = end_date
    if start_date is None or end_date is None:
        return None, None, "none"

    start_time = parse_time_value(request.headers.get("X-Time-From") or request.GET.get("time_from"), time.min) or time.min
    end_time = parse_time_value(request.headers.get("X-Time-To") or request.GET.get("time_to"), time.max) or time.max
    return datetime.combine(start_date, start_time), datetime.combine(end_date, end_time), "date_time"


def require_api_key(request: HttpRequest) -> JsonResponse | None:
    if not services.API_KEY:
        return JsonResponse({"detail": "API_KEY is not configured"}, status=500)
    supplied = request.headers.get("200m-API-Key") or request.headers.get("X-API-Key")
    if supplied != services.API_KEY:
        return JsonResponse({"detail": "Invalid or missing API key"}, status=401)
    return None


def protected_json(request: HttpRequest, data: dict[str, Any], status: int = 200) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    return JsonResponse(data, status=status)


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    client, col = services.collection()
    try:
        return JsonResponse(
            {
                "ok": True,
                "database": services.MONGO_DB,
                "collection": services.MONGO_COLLECTION,
                "documents": col.count_documents({}),
            }
        )
    finally:
        client.close()


@csrf_exempt
@require_POST
def main_start(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    return JsonResponse(services.start_main_process())


@require_GET
def scraper_settings(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    return JsonResponse(services.get_scraper_settings())


@csrf_exempt
@require_POST
def scraper_interval(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except json.JSONDecodeError:
        payload = {}
    value = payload.get("seconds") or request.GET.get("seconds") or request.headers.get("X-Scrape-Interval-Seconds")
    seconds = parse_int(str(value) if value is not None else None, 2700, services.MIN_SCRAPE_INTERVAL_SECONDS, services.MAX_SCRAPE_INTERVAL_SECONDS)
    return JsonResponse(services.update_scraper_interval(seconds))


@csrf_exempt
@require_POST
def scraper_start_from_first_page(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    return JsonResponse(services.start_main_process_from_first_page())


@csrf_exempt
@require_POST
def main_stop(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    return JsonResponse(services.stop_main_process())


@csrf_exempt
@require_POST
def main_restart(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    return JsonResponse(services.restart_main_process())


@require_GET
def database_backup(request: HttpRequest) -> HttpResponse:
    if not dashboard_logged_in(request):
        error = require_api_key(request)
        if error is not None:
            return error
    filename, content = services.database_backup_bytes()
    response = HttpResponse(content, content_type="application/gzip")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@csrf_exempt
@require_POST
def database_restore(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    uploaded = request.FILES.get("backup")
    if uploaded is None:
        return JsonResponse({"detail": "Upload a gzip backup file using the backup field."}, status=400)
    try:
        data = services.restore_database_backup(uploaded.read())
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    return JsonResponse(data)


@require_GET
def main_status(request: HttpRequest) -> JsonResponse:
    return protected_json(request, services.compact_main_status())


@require_GET
def main_logs(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    lines = parse_int(request.GET.get("lines"), 100, 1, 1000)
    response = services.main_response("main.py logs")
    response["lines"] = lines
    response["logs"] = services.read_main_log(lines)
    return JsonResponse(response)


@require_GET
def domains(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    limit = parse_int(request.GET.get("limit"), 100, 1, 1000)
    skip = parse_int(request.GET.get("skip"), 0, 0, 1_000_000)
    batch = parse_int(request.GET.get("batch"), 0, 1, 1_000_000) if request.GET.get("batch") else None
    return JsonResponse(
        services.get_domains(
            limit=limit,
            skip=skip,
            batch=batch,
            date_from=parse_date_start(request.GET.get("date_from")),
            date_to=parse_date_end(request.GET.get("date_to")),
        )
    )


@require_GET
def duplicate_domains(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    limit = parse_int(request.GET.get("limit"), 100, 1, 1000)
    skip = parse_int(request.GET.get("skip"), 0, 0, 1_000_000)
    return JsonResponse(services.get_duplicate_domains(limit=limit, skip=skip, domain=request.GET.get("domain")))


@require_GET
def domain_detail(request: HttpRequest, domain: str) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    row = services.get_domain(domain)
    if row is None:
        return JsonResponse({"detail": "Domain not found"}, status=404)
    return JsonResponse(row)


@require_GET
def batches(request: HttpRequest) -> JsonResponse:
    return protected_json(request, services.get_batches())


@csrf_exempt
@require_POST
def seo_check(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    limit = parse_int(request.GET.get("limit"), 2000, 1, 10000)
    force = parse_bool(request.GET.get("force"))
    recheck_days = parse_int(request.GET.get("recheck_days"), 14, 0, 3650)
    return JsonResponse(services.start_seo_check(limit=limit, force=True if force is None else force, recheck_days=recheck_days))


@require_GET
def seo_job(request: HttpRequest, job_id: str) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    job = services.get_seo_job(job_id)
    if job is None:
        return JsonResponse({"detail": "SEO job not found"}, status=404)
    return JsonResponse(job)


@require_GET
def seo_checks(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    limit = parse_int(request.GET.get("limit"), 100, 1, 1000)
    skip = parse_int(request.GET.get("skip"), 0, 0, 1_000_000)
    reachable = parse_bool(request.GET.get("reachable"))
    min_score = parse_int(request.GET.get("min_score"), 0, 0, 100) if request.GET.get("min_score") else None
    max_score = parse_int(request.GET.get("max_score"), 100, 0, 100) if request.GET.get("max_score") else None
    return JsonResponse(
        services.get_seo_checks(
            limit=limit,
            skip=skip,
            reachable=reachable,
            domain=request.GET.get("domain"),
            min_score=min_score,
            max_score=max_score,
        )
    )


@require_GET
def seo_rankings(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    limit = parse_int(request.GET.get("limit"), 100, 1, 1000)
    skip = parse_int(request.GET.get("skip"), 0, 0, 1_000_000)
    reachable = parse_bool(request.GET.get("reachable"))
    return JsonResponse(services.get_seo_rankings(limit=limit, skip=skip, reachable=reachable))


@require_GET
def seo_sheet(request: HttpRequest) -> HttpResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    limit = parse_int(request.GET.get("limit"), 100, 1, 5000)
    skip = parse_int(request.GET.get("skip"), 0, 0, 1_000_000)
    reachable = parse_bool(request.GET.get("reachable"))
    min_score = parse_int(request.GET.get("min_score"), 0, 0, 100) if request.GET.get("min_score") else None
    data = services.get_seo_sheet(
        limit=limit,
        skip=skip,
        reachable=reachable,
        domain=request.GET.get("domain"),
        min_score=min_score,
    )
    if request.GET.get("format") == "csv":
        response = HttpResponse(services.rows_to_csv(data["items"]), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="seo_sheet.csv"'
        return response
    return JsonResponse(data)


@csrf_exempt
@require_POST
def wsc_check(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    limit = parse_int(request.GET.get("limit"), 50, 1, 50)
    force = parse_bool(request.GET.get("force"))
    return JsonResponse(services.start_wsc_check(limit=limit, force=False if force is None else force))


@require_GET
def wsc_job(request: HttpRequest, job_id: str) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    job = services.get_wsc_job(job_id)
    if job is None:
        return JsonResponse({"detail": "Website SEO Checker job not found"}, status=404)
    return JsonResponse(job)


@require_GET
def wsc_results(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    limit = parse_int(request.GET.get("limit"), 100, 1, 1000)
    skip = parse_int(request.GET.get("skip"), 0, 0, 1_000_000)
    return JsonResponse(
        services.get_wsc_results(
            limit=limit,
            skip=skip,
            domain=request_value(request, "domain", "X-Domain"),
            da_min=request_int(request, "da_min", "X-DA-Min", 0, 0, 100),
            da_max=request_int(request, "da_max", "X-DA-Max", 100, 0, 100),
            pa_min=request_int(request, "pa_min", "X-PA-Min", 0, 0, 100),
            pa_max=request_int(request, "pa_max", "X-PA-Max", 100, 0, 100),
            tbl_min=request_int(request, "tbl_min", "X-TBL-Min", 0, 0, 10_000_000_000),
            tbl_max=request_int(request, "tbl_max", "X-TBL-Max", 10_000_000_000, 0, 10_000_000_000),
            qbl_min=request_int(request, "qbl_min", "X-QBL-Min", 0, 0, 10_000_000_000),
            qbl_max=request_int(request, "qbl_max", "X-QBL-Max", 10_000_000_000, 0, 10_000_000_000),
            qt_min=request_int(request, "qt_min", "X-QT-Min", 0, 0, 100),
            qt_max=request_int(request, "qt_max", "X-QT-Max", 100, 0, 100),
            os_min=request_int(request, "os_min", "X-OS-Min", 0, 0, 100),
            os_max=request_int(request, "os_max", "X-OS-Max", 100, 0, 100),
            ss_min=request_int(request, "ss_min", "X-SS-Min", 0, 0, 100),
            ss_max=request_int(request, "ss_max", "X-SS-Max", 100, 0, 100),
        )
    )


@csrf_exempt
@require_POST
def nawala_check(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    limit = parse_int(request.GET.get("limit"), 1500, 1, 5000)
    force = parse_bool(request.GET.get("force"))
    return JsonResponse(services.start_nawala_check(limit=limit, force=False if force is None else force))


@require_GET
def nawala_job(request: HttpRequest, job_id: str) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    job = services.get_nawala_job(job_id)
    if job is None:
        return JsonResponse({"detail": "Nawala job not found"}, status=404)
    return JsonResponse(job)


@require_GET
def nawala_results(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    limit = parse_int(request.GET.get("limit"), 100, 1, 1000)
    skip = parse_int(request.GET.get("skip"), 0, 0, 1_000_000)
    blocked = parse_bool(request.GET.get("blocked"))
    return JsonResponse(
        services.get_nawala_results(
            limit=limit,
            skip=skip,
            domain=request.GET.get("domain"),
            blocked=blocked,
            ss_min=request_int(request, "ss_min", "X-SS-Min", 0, 0, 100),
            ss_max=request_int(request, "ss_max", "X-SS-Max", 100, 0, 100),
        )
    )


@require_GET
def seo_checker_false_results(request: HttpRequest) -> JsonResponse:
    error = require_api_key(request)
    if error is not None:
        return error
    limit = parse_int(request.GET.get("limit"), 1000, 1, 5000)
    skip = parse_int(request.GET.get("skip"), 0, 0, 1_000_000)
    tbl_min = request_int(request, "tbl_min", "X-TBL-Min", 0, 0, 10_000_000_000)
    tbl_max = request_int(request, "tbl_max", "X-TBL-Max", 10_000_000_000, 0, 10_000_000_000)
    qbl_min = request_int(request, "qbl_min", "X-QBL-Min", 0, 0, 10_000_000_000)
    qbl_max = request_int(request, "qbl_max", "X-QBL-Max", 10_000_000_000, 0, 10_000_000_000)
    qt_min = request_int(request, "qt_min", "X-QT-Min", 0, 0, 100)
    qt_max = request_int(request, "qt_max", "X-QT-Max", 100, 0, 100)
    ss_min = parse_int(request_value(request, "ss_min", "X-SS-Min") or "1", 1, 0, 100)
    ss_max = parse_int(request_value(request, "ss_max", "X-SS-Max") or "1", 1, 0, 100)
    checked_from, checked_to, checked_filter = request_checked_range(request)
    return JsonResponse(
        services.get_seo_checker_false_results(
            limit=limit,
            skip=skip,
            tbl_min=tbl_min,
            tbl_max=tbl_max,
            qbl_min=qbl_min,
            qbl_max=qbl_max,
            qt_min=qt_min,
            qt_max=qt_max,
            ss_min=ss_min,
            ss_max=ss_max,
            checked_from=checked_from,
            checked_to=checked_to,
            checked_filter=checked_filter,
        )
    )
