import csv
import io
import os
import time
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable, Dict, List, Tuple
from uuid import uuid4

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .services.audit import check_robots_and_sitemap, discover_site_urls, run_quick_audit
from .services.csv_utils import parse_csv_file, parse_gsc_csv
from .services.matching import (
    build_manual_review_csv,
    build_redirects_csv,
    infer_type,
    match_urls,
    normalize_url,
    rank_urgent_actions,
    result_row,
    summarize_matches,
)

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="SEO Recovery Platform", version="1.2.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def flag_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


FEATURE_FLAGS = {
    "ASYNC_ANALYSIS": flag_enabled("FEATURE_ASYNC_ANALYSIS", True),
    "CANCEL_JOB": flag_enabled("FEATURE_CANCEL_JOB", True),
    "RECOVERY_PANEL": flag_enabled("FEATURE_RECOVERY_PANEL", True),
}

RESULTS: Dict[str, Dict[str, Any]] = {}
JOBS: Dict[str, Dict[str, Any]] = {}
STATE_LOCK = Lock()


class AppError(Exception):
    def __init__(self, user_message: str, details: List[str] | None = None) -> None:
        super().__init__(user_message)
        self.log_id = str(uuid4())[:8]
        self.user_message = user_message
        self.details = details or []

    def to_lines(self) -> List[str]:
        if self.details:
            return [self.user_message, *self.details, f"Hata kodu: {self.log_id}"]
        return [self.user_message, f"Hata kodu: {self.log_id}"]

    def to_status_error(self) -> str:
        return f"{self.user_message} (Hata kodu: {self.log_id})"


class JobCancelled(Exception):
    pass


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "now": datetime.utcnow().isoformat(),
            "flags": FEATURE_FLAGS,
        },
    )


@app.post("/analyze/start")
async def analyze_start(
    old_urls: UploadFile | None = File(None),
    new_urls: UploadFile | None = File(None),
    gsc_pages: UploadFile | None = File(None),
    gsc_before_pages: UploadFile | None = File(None),
    gsc_after_pages: UploadFile | None = File(None),
    site_url: str = Form(default=""),
    run_audit: bool = Form(default=False),
    audit_limit: int = Form(default=150),
    crawl_site: bool = Form(default=False),
    crawl_limit: int = Form(default=200),
) -> JSONResponse:
    if not FEATURE_FLAGS["ASYNC_ANALYSIS"]:
        return JSONResponse({"error": "Asenkron analiz devre disi."}, status_code=503)

    old_raw = await old_urls.read() if old_urls and old_urls.filename else b""
    new_raw = await new_urls.read() if new_urls and new_urls.filename else b""
    gsc_raw_legacy = await gsc_pages.read() if gsc_pages and gsc_pages.filename else b""
    gsc_before_raw = await gsc_before_pages.read() if gsc_before_pages and gsc_before_pages.filename else gsc_raw_legacy
    gsc_after_raw = await gsc_after_pages.read() if gsc_after_pages and gsc_after_pages.filename else b""

    job_id = str(uuid4())
    with STATE_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "progress": 5,
            "message": "Analiz siraya alindi.",
            "error": "",
            "log_id": "",
            "created_at": datetime.utcnow().isoformat(),
            "started_epoch": time.time(),
            "eta_seconds": None,
            "cancel_requested": False,
            "can_cancel": FEATURE_FLAGS["CANCEL_JOB"],
        }

    worker = Thread(
        target=run_analysis_job,
        args=(
            job_id,
            old_raw,
            new_raw,
            gsc_before_raw,
            gsc_after_raw,
            site_url,
            run_audit,
            audit_limit,
            crawl_site,
            crawl_limit,
        ),
        daemon=True,
    )
    worker.start()
    return JSONResponse({"job_id": job_id})


@app.post("/analyze/cancel/{job_id}")
async def analyze_cancel(job_id: str) -> JSONResponse:
    if not FEATURE_FLAGS["CANCEL_JOB"]:
        return JSONResponse({"status": "disabled"}, status_code=403)
    with STATE_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return JSONResponse({"status": "not_found"}, status_code=404)
        if job.get("status") in {"done", "error", "cancelled"}:
            return JSONResponse({"status": "finished"})
        job["cancel_requested"] = True
        job["message"] = "Iptal islemi siraya alindi..."
        JOBS[job_id] = job
    return JSONResponse({"status": "cancelling"})


@app.get("/analyze/status/{job_id}")
async def analyze_status(job_id: str) -> JSONResponse:
    with STATE_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"status": "not_found", "progress": 0, "message": "Islem bulunamadi."}, status_code=404)
    payload = dict(job)
    if payload.get("status") == "done":
        payload["result_url"] = f"/result/{job_id}"
    return JSONResponse(payload)


@app.get("/result/{job_id}", response_class=HTMLResponse)
async def result_page(request: Request, job_id: str) -> HTMLResponse:
    result = RESULTS.get(job_id)
    if not result:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "errors": ["Sonuc bulunamadi. Analizi yeniden baslatin."], "flags": FEATURE_FLAGS},
            status_code=404,
        )
    context = result["context"]
    return templates.TemplateResponse("result.html", {"request": request, **context})


@app.post("/analyze", response_class=HTMLResponse)
async def analyze_fallback(
    request: Request,
    old_urls: UploadFile | None = File(None),
    new_urls: UploadFile | None = File(None),
    gsc_pages: UploadFile | None = File(None),
    gsc_before_pages: UploadFile | None = File(None),
    gsc_after_pages: UploadFile | None = File(None),
    site_url: str = Form(default=""),
    run_audit: bool = Form(default=False),
    audit_limit: int = Form(default=150),
    crawl_site: bool = Form(default=False),
    crawl_limit: int = Form(default=200),
) -> HTMLResponse:
    old_raw = await old_urls.read() if old_urls and old_urls.filename else b""
    new_raw = await new_urls.read() if new_urls and new_urls.filename else b""
    gsc_raw_legacy = await gsc_pages.read() if gsc_pages and gsc_pages.filename else b""
    gsc_before_raw = await gsc_before_pages.read() if gsc_before_pages and gsc_before_pages.filename else gsc_raw_legacy
    gsc_after_raw = await gsc_after_pages.read() if gsc_after_pages and gsc_after_pages.filename else b""
    job_id = str(uuid4())

    try:
        package = perform_analysis(
            old_raw,
            new_raw,
            gsc_before_raw,
            gsc_after_raw,
            site_url,
            run_audit,
            audit_limit,
            crawl_site,
            crawl_limit,
        )
    except AppError as exc:
        return templates.TemplateResponse("index.html", {"request": request, "errors": exc.to_lines(), "flags": FEATURE_FLAGS}, status_code=400)

    RESULTS[job_id] = {
        "redirects_csv": package["redirects_csv"],
        "manual_csv": package["manual_csv"],
        "compare_csv": package["compare_csv"],
        "context": {**package["context"], "job_id": job_id},
    }
    return templates.TemplateResponse("result.html", {"request": request, **RESULTS[job_id]["context"]})


@app.get("/download/{job_id}/redirects.csv")
async def download_redirects(job_id: str) -> Response:
    result = RESULTS.get(job_id)
    if not result:
        return Response(content="Kayit bulunamadi", media_type="text/plain", status_code=404)
    return Response(
        content=result["redirects_csv"],
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="redirects_{job_id}.csv"'},
    )


@app.get("/download/{job_id}/manual.csv")
async def download_manual(job_id: str) -> Response:
    result = RESULTS.get(job_id)
    if not result:
        return Response(content="Kayit bulunamadi", media_type="text/plain", status_code=404)
    return Response(
        content=result["manual_csv"],
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="manual_review_{job_id}.csv"'},
    )


@app.get("/download/{job_id}/comparison.csv")
async def download_comparison(job_id: str) -> Response:
    result = RESULTS.get(job_id)
    if not result:
        return Response(content="Kayit bulunamadi", media_type="text/plain", status_code=404)
    return Response(
        content=result.get("compare_csv", "url,before_clicks,after_clicks\n"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="comparison_{job_id}.csv"'},
    )


def run_analysis_job(
    job_id: str,
    old_raw: bytes,
    new_raw: bytes,
    gsc_before_raw: bytes,
    gsc_after_raw: bytes,
    site_url: str,
    run_audit: bool,
    audit_limit: int,
    crawl_site: bool,
    crawl_limit: int,
) -> None:
    def progress(percent: int, message: str) -> None:
        check_cancel(job_id)
        update_job(job_id, status="running", progress=max(1, min(percent, 99)), message=message)

    try:
        package = perform_analysis(
            old_raw=old_raw,
            new_raw=new_raw,
            gsc_before_raw=gsc_before_raw,
            gsc_after_raw=gsc_after_raw,
            site_url=site_url,
            run_audit=run_audit,
            audit_limit=audit_limit,
            crawl_site=crawl_site,
            crawl_limit=crawl_limit,
            progress_cb=progress,
            cancel_cb=lambda: check_cancel(job_id),
        )
        RESULTS[job_id] = {
            "redirects_csv": package["redirects_csv"],
            "manual_csv": package["manual_csv"],
            "compare_csv": package["compare_csv"],
            "context": {**package["context"], "job_id": job_id},
        }
        update_job(job_id, status="done", progress=100, message="Rapor hazirlandi.", eta_seconds=0)
    except JobCancelled:
        update_job(job_id, status="cancelled", progress=100, message="Analiz iptal edildi.", eta_seconds=0)
    except AppError as exc:
        update_job(
            job_id,
            status="error",
            progress=100,
            message="Analiz tamamlanamadi.",
            error=exc.to_status_error(),
            log_id=exc.log_id,
            eta_seconds=0,
        )
    except Exception as exc:  # noqa: BLE001
        app_error = AppError("Beklenmeyen bir hata olustu.", [str(exc)])
        update_job(
            job_id,
            status="error",
            progress=100,
            message="Analiz tamamlanamadi.",
            error=app_error.to_status_error(),
            log_id=app_error.log_id,
            eta_seconds=0,
        )


def update_job(job_id: str, **kwargs: Any) -> None:
    with STATE_LOCK:
        current = JOBS.get(job_id, {})
        current.update(kwargs)
        progress = int(current.get("progress", 0) or 0)
        started = float(current.get("started_epoch", 0) or 0)
        if started > 0 and 0 < progress < 100 and current.get("status") == "running":
            elapsed = max(1.0, time.time() - started)
            eta = int((elapsed / progress) * (100 - progress))
            current["eta_seconds"] = max(1, eta)
        elif progress >= 100:
            current["eta_seconds"] = 0
        JOBS[job_id] = current


def check_cancel(job_id: str) -> None:
    with STATE_LOCK:
        job = JOBS.get(job_id, {})
        cancel_requested = bool(job.get("cancel_requested")) and FEATURE_FLAGS["CANCEL_JOB"]
    if cancel_requested:
        raise JobCancelled()


def perform_analysis(
    old_raw: bytes,
    new_raw: bytes,
    gsc_before_raw: bytes,
    gsc_after_raw: bytes,
    site_url: str,
    run_audit: bool,
    audit_limit: int,
    crawl_site: bool,
    crawl_limit: int,
    progress_cb: Callable[[int, str], None] | None = None,
    cancel_cb: Callable[[], None] | None = None,
) -> Dict[str, Any]:
    progress = progress_cb or (lambda _p, _m: None)
    cancel = cancel_cb or (lambda: None)
    errors: List[str] = []
    old_rows: List[Dict[str, str]] = []
    new_rows: List[Dict[str, str]] = []
    gsc_before_rows: List[Dict[str, str]] = []
    gsc_after_rows: List[Dict[str, str]] = []
    analysis_mode = "migration"
    score_threshold = 70

    progress(10, "Girdi dosyalari okunuyor.")
    has_old_file = bool(old_raw)
    has_new_file = bool(new_raw)

    if has_old_file and has_new_file:
        old_rows, old_errors = parse_csv_file(old_raw, "Eski URL dosyasi")
        new_rows, new_errors = parse_csv_file(new_raw, "Yeni URL dosyasi")
        errors.extend(old_errors)
        errors.extend(new_errors)
    elif crawl_site and site_url.strip():
        analysis_mode = "scan"
        progress(20, "Site URL'leri bulunuyor (sitemap + ic link taramasi).")
        cancel()
        cap = max(10, min(crawl_limit, 2000))
        discovered = discover_site_urls(site_url, limit=cap)
        if not discovered:
            errors.append("Site URL ile otomatik taramada URL bulunamadi. Site erisimi, robots veya sitemap kontrol edin.")
        new_rows = [{"url": url, "type": infer_type(url)} for url in discovered]
        old_rows = list(new_rows)
        run_audit = True
    else:
        errors.append("CSV gecis analizi icin eski ve yeni URL dosyalari gereklidir.")
        errors.append("Alternatif olarak 'Siteyi otomatik tara' secenegini acip Site URL girebilirsin.")

    if gsc_before_raw:
        gsc_before_rows, gsc_before_errors = parse_gsc_csv(gsc_before_raw)
        errors.extend(gsc_before_errors)
    if gsc_after_raw:
        gsc_after_rows, gsc_after_errors = parse_gsc_csv(gsc_after_raw)
        errors.extend(gsc_after_errors)

    if not old_rows:
        errors.append("Eski URL listesi bos veya gecersiz.")
    if not new_rows:
        errors.append("Yeni URL listesi bos veya gecersiz.")
    if errors:
        raise AppError("Analiz baslatilamadi.", errors)

    progress(45, "URL eslestirme ve onceliklendirme yapiliyor.")
    cancel()
    if analysis_mode == "migration":
        matches = match_urls(old_rows, new_rows)
    else:
        matches = [result_row(item["url"], item["url"], 100, "site_scan", item.get("type") or infer_type(item["url"])) for item in old_rows]

    summary = summarize_matches(matches, score_threshold=score_threshold)
    gsc_before_map = build_gsc_metric_map(gsc_before_rows)
    gsc_after_map = build_gsc_metric_map(gsc_after_rows)
    urgent_actions = rank_urgent_actions(matches, gsc_before_map) if analysis_mode == "migration" else []

    redirects_csv = build_redirects_csv(matches, score_threshold=score_threshold) if analysis_mode == "migration" else "Redirect from,Redirect to\r\n"
    manual_csv = build_manual_review_csv(matches, score_threshold=score_threshold) if analysis_mode == "migration" else "old_url,onerilen_new_url,score,reason\r\n"

    audit_results: List[Dict[str, str]] = []
    if run_audit:
        progress(70, "Teknik audit yapiliyor (404, noindex, canonical, robots, sitemap).")
        cap = max(1, min(audit_limit, 500))
        urls = [row["url"] for row in new_rows][:cap]
        audit_results = []
        batch_size = 40
        for idx in range(0, len(urls), batch_size):
            cancel()
            batch = urls[idx : idx + batch_size]
            audit_results.extend(run_quick_audit(batch))
            partial = 70 + int(((idx + len(batch)) / max(1, len(urls))) * 15)
            progress(min(88, partial), "Teknik audit devam ediyor...")
        if site_url.strip():
            cancel()
            audit_results.extend(check_robots_and_sitemap(site_url))
        audit_results = annotate_audit_items(audit_results)
    if analysis_mode == "scan":
        urgent_actions = build_audit_urgent_actions(audit_results)

    progress(90, "Rapor hazirlaniyor.")
    cancel()

    comparison_rows, unresolved_rows = build_comparison_rows(matches, gsc_before_map, gsc_after_map, analysis_mode)
    recovery_panel = build_recovery_panel(comparison_rows) if FEATURE_FLAGS["RECOVERY_PANEL"] else []
    compare_csv = build_comparison_csv(comparison_rows)

    critical_issues = sum(1 for item in audit_results if item["severity"] == "critical")
    context = {
        "summary": summary,
        "critical_issues": critical_issues,
        "matches": matches[:100],
        "urgent_actions": urgent_actions,
        "audit_results": audit_results[:200],
        "site_url": site_url,
        "run_audit": run_audit,
        "analysis_mode": analysis_mode,
        "comparison_rows": comparison_rows[:200],
        "unresolved_rows": unresolved_rows[:100],
        "recovery_panel": recovery_panel,
        "flags": FEATURE_FLAGS,
    }
    return {"context": context, "redirects_csv": redirects_csv, "manual_csv": manual_csv, "compare_csv": compare_csv}


def build_gsc_metric_map(gsc_rows: List[Dict[str, str]]) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for row in gsc_rows:
        key = normalize_url(row.get("url", ""))
        if not key:
            continue
        out[key] = {
            "clicks": safe_float(row.get("clicks"), 0.0),
            "impressions": safe_float(row.get("impressions"), 0.0),
            "position": safe_float(row.get("position"), 999.0),
        }
    return out


def safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:  # noqa: BLE001
        return default


def pick_metric(map_data: Dict[str, Dict[str, float]], primary_url: str, fallback_url: str = "") -> Dict[str, float]:
    primary_key = normalize_url(primary_url)
    fallback_key = normalize_url(fallback_url) if fallback_url else ""
    return map_data.get(primary_key) or (map_data.get(fallback_key) if fallback_key else None) or {"clicks": 0.0, "impressions": 0.0, "position": 999.0}


def build_comparison_rows(
    matches: List[Dict[str, str]],
    gsc_before_map: Dict[str, Dict[str, float]],
    gsc_after_map: Dict[str, Dict[str, float]],
    analysis_mode: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    if not gsc_before_map or not gsc_after_map:
        return rows, unresolved

    for item in matches:
        before = pick_metric(gsc_before_map, item["old_url"], item.get("new_url", ""))
        after = pick_metric(gsc_after_map, item.get("new_url", ""), item["old_url"])
        click_delta = after["clicks"] - before["clicks"]
        position_delta = before["position"] - after["position"]
        status = compare_status(before, after, item, analysis_mode)
        row = {
            "old_url": item["old_url"],
            "new_url": item.get("new_url", ""),
            "before_clicks": round(before["clicks"], 2),
            "after_clicks": round(after["clicks"], 2),
            "click_delta": round(click_delta, 2),
            "before_position": round(before["position"], 2),
            "after_position": round(after["position"], 2),
            "position_delta": round(position_delta, 2),
            "status": status,
            "reason": item.get("reason", ""),
        }
        rows.append(row)
        if status == "duzeltildi_ama_toparlanmadi":
            unresolved.append({**row, "lost_clicks": round(before["clicks"] - after["clicks"], 2)})

    rows.sort(key=lambda x: abs(x["click_delta"]), reverse=True)
    unresolved.sort(key=lambda x: x["lost_clicks"], reverse=True)
    return rows, unresolved


def compare_status(before: Dict[str, float], after: Dict[str, float], item: Dict[str, Any], analysis_mode: str) -> str:
    if analysis_mode == "migration" and item.get("score", 0) < 70:
        return "manual_gerekli"
    if before["clicks"] == 0 and after["clicks"] > 0:
        return "yeni_toparlaniyor"
    click_ok = after["clicks"] >= (before["clicks"] * 0.9)
    position_ok = after["position"] <= (before["position"] + 0.3)
    if click_ok or position_ok:
        return "toparlaniyor"
    return "duzeltildi_ama_toparlanmadi"


def build_recovery_panel(comparison_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not comparison_rows:
        return []
    total = len(comparison_rows)
    recovering = sum(1 for r in comparison_rows if r["status"] in {"toparlaniyor", "yeni_toparlaniyor"})
    unresolved = sum(1 for r in comparison_rows if r["status"] == "duzeltildi_ama_toparlanmadi")
    base_rate = int(round((recovering / max(1, total)) * 100))
    panels = []
    for day, bonus in [(7, 5), (14, 10), (30, 18)]:
        rate = min(100, base_rate + bonus)
        panels.append(
            {
                "day": day,
                "recovery_rate": rate,
                "recovering_count": recovering,
                "unresolved_count": unresolved,
            }
        )
    return panels


def build_comparison_csv(rows: List[Dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "old_url",
            "new_url",
            "before_clicks",
            "after_clicks",
            "click_delta",
            "before_position",
            "after_position",
            "position_delta",
            "status",
            "reason",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["old_url"],
                r["new_url"],
                r["before_clicks"],
                r["after_clicks"],
                r["click_delta"],
                r["before_position"],
                r["after_position"],
                r["position_delta"],
                r["status"],
                r["reason"],
            ]
        )
    return output.getvalue()


def build_audit_urgent_actions(audit_results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    scored = []
    for item in audit_results:
        severity = item.get("severity", "info")
        issues = item.get("issues", "")
        status_code = item.get("status_code", "0")
        sev_points = 100 if severity == "critical" else 60 if severity == "warning" else 20
        issue_points = 0
        if "404" in issues:
            issue_points += 30
        if "5xx" in issues:
            issue_points += 40
        if "noindex" in issues:
            issue_points += 25
        if "canonical_mismatch" in issues:
            issue_points += 15
        if "request_error" in issues:
            issue_points += 35
        total = sev_points + issue_points
        scored.append(
            {
                "old_url": item.get("url", ""),
                "new_url": item.get("final_url", ""),
                "score": status_code,
                "impact": total,
                "reason": issues or "ok",
                "cause": item.get("cause", ""),
                "fix": item.get("fix", ""),
            }
        )
    scored.sort(key=lambda x: x["impact"], reverse=True)
    return scored[:20]


def annotate_audit_items(audit_results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    annotated = []
    for item in audit_results:
        cause, fix = diagnose_issue(item)
        annotated.append({**item, "cause": cause, "fix": fix})
    return annotated


def diagnose_issue(item: Dict[str, str]) -> Tuple[str, str]:
    issues = (item.get("issues") or "").lower()
    status = str(item.get("status_code", "0"))
    url = item.get("url", "")

    if "request_error" in issues:
        return (
            "Sunucuya baglanti kurulamadi veya zaman asimi olustu.",
            "Domain/DNS/SSL erisimini kontrol et. Hosting aktif mi bak. Sonra tekrar tarama yap.",
        )
    if "404" in issues:
        if url.endswith("/sitemap.xml"):
            return (
                "Sitemap dosyasi bulunamiyor.",
                "Dogru domaini primary olarak ayarla. Ardindan /sitemap.xml aciliyor mu kontrol et ve Search Console'a gonder.",
            )
        return (
            "URL bulunamiyor (404).",
            "Sayfa tasindiysa 301 redirect tanimla. Silinmediyse sayfayi geri yayinla.",
        )
    if "5xx" in issues:
        return (
            "Sunucu hatasi (5xx) var.",
            "Hosting loglarini kontrol et. Uygulama/tema hatasini duzeltip URL'i tekrar test et.",
        )
    if "noindex" in issues:
        return (
            "Sayfa noindex etiketli oldugu icin Google'a kapanmis.",
            "Meta robots veya HTTP header'daki noindex'i kaldir. Sonra Search Console'da tekrar index iste.",
        )
    if "canonical_mismatch" in issues:
        return (
            "Canonical farkli bir URL'e isaret ediyor.",
            "Canonical etiketini bu sayfanin dogru final URL'ine guncelle.",
        )
    if status.startswith("4"):
        return (
            "Istemci hatasi (4xx) olustu.",
            "URL yazimini, yetki kurallarini ve yonlendirmeleri kontrol et.",
        )
    if status.startswith("5"):
        return (
            "Sunucu yanit veremedi (5xx).",
            "Sunucu logu ve uygulama hatalarini kontrol edip tekrar yayinla.",
        )
    return ("Sorun gorunmuyor.", "Aksiyon gerekmiyor.")
