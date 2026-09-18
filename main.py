from __future__ import annotations

import base64
import secrets
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from smart_analyzer.config import ROOT_DIR, get_settings
from smart_analyzer.files import list_dir, read_file, resolve_under_session
from smart_analyzer.jobs import get_job_manager
from smart_analyzer.pipeline import yesterday

STATIC_DIR = ROOT_DIR / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    mgr = get_job_manager()
    mgr.start()
    try:
        yield
    finally:
        mgr.shutdown()


app = FastAPI(title="smart-analyzer", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    """全站 HTTP Basic Auth，默认 hollycrm:hollycrm。"""
    cfg = get_settings()
    expected_user = cfg.basic_auth_user.encode("utf-8")
    expected_pass = cfg.basic_auth_password.encode("utf-8")
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
            username, _, password = decoded.partition(":")
            user_ok = secrets.compare_digest(username.encode("utf-8"), expected_user)
            pass_ok = secrets.compare_digest(password.encode("utf-8"), expected_pass)
            if user_ok and pass_ok:
                return await call_next(request)
        except Exception:
            pass
    return Response(
        content="Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="smart-analyzer"'},
        media_type="text/plain",
    )


class CreateScheduleBody(BaseModel):
    name: str = "日分析"
    hour: int = Field(default=3, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    sample_rate: int = 100
    from_cache: bool = False
    enabled: bool = True


class PatchScheduleBody(BaseModel):
    enabled: bool


class ManualRunBody(BaseModel):
    day: str | None = None
    sample_rate: int | None = None
    from_cache: bool = False
    name: str | None = None


@app.get("/")
def index_page():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return {"service": "smart-analyzer", "status": "ok", "ui": "missing"}
    return FileResponse(index)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/schedules")
def api_list_schedules() -> list[dict[str, Any]]:
    return get_job_manager().list_schedules()


@app.post("/api/schedules")
def api_create_schedule(body: CreateScheduleBody) -> dict[str, Any]:
    try:
        return get_job_manager().create_schedule(
            name=body.name,
            hour=body.hour,
            minute=body.minute,
            sample_rate=body.sample_rate,
            from_cache=body.from_cache,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/schedules/{schedule_id}")
def api_patch_schedule(schedule_id: str, body: PatchScheduleBody) -> dict[str, Any]:
    try:
        return get_job_manager().set_enabled(schedule_id, body.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="schedule not found") from exc


@app.delete("/api/schedules/{schedule_id}")
def api_delete_schedule(schedule_id: str) -> dict[str, str]:
    try:
        get_job_manager().delete_schedule(schedule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="schedule not found") from exc
    return {"status": "deleted", "id": schedule_id}


@app.post("/api/schedules/{schedule_id}/run")
def api_run_schedule(schedule_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    mgr = get_job_manager()
    schedules = {s["id"]: s for s in mgr.list_schedules()}
    spec = schedules.get(schedule_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="schedule not found")

    def _job() -> None:
        mgr.run_now(
            day=yesterday(),
            sample_rate=int(spec["sample_rate"]),
            from_cache=bool(spec["from_cache"]),
            schedule_id=schedule_id,
            schedule_name=str(spec["name"]),
            trigger="manual",
        )

    background_tasks.add_task(_job)
    return {"status": "started", "schedule_id": schedule_id}


@app.get("/api/runs")
def api_list_runs(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    return get_job_manager().list_runs(limit=limit)


@app.post("/api/runs")
def api_manual_run(
    body: ManualRunBody, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    mgr = get_job_manager()
    target = yesterday() if not body.day else date.fromisoformat(body.day)

    def _job() -> None:
        mgr.run_now(
            day=target,
            sample_rate=body.sample_rate,
            from_cache=body.from_cache,
            schedule_name=body.name
            or ("重新分析" if body.from_cache else "手动执行"),
            trigger="manual",
        )

    background_tasks.add_task(_job)
    return {"status": "started", "date": target.isoformat()}


@app.get("/api/files")
def api_list_files(path: str = Query(default="")) -> dict[str, Any]:
    try:
        return list_dir(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="path not found") from exc
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail="not a directory") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="forbidden") from exc


@app.get("/api/files/content")
def api_file_content(
    path: str = Query(...),
    format: str = Query(default="json", pattern="^(json|text)$"),
):
    try:
        data = read_file(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="file not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="forbidden") from exc

    if format != "text":
        return data
    if not data.get("text"):
        raise HTTPException(
            status_code=415,
            detail=data.get("message") or "not a text file",
        )
    return PlainTextResponse(
        data.get("content") or "",
        media_type=data.get("mime") or "text/plain; charset=utf-8",
    )


@app.get("/api/files/download")
def api_file_download(path: str = Query(...)):
    try:
        target = resolve_under_session(path)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="forbidden") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(
        path=target,
        filename=target.name,
        media_type="application/octet-stream",
        content_disposition_type="attachment",
    )


@app.post("/jobs/run")
def trigger_job(
    background_tasks: BackgroundTasks,
    day: str | None = Query(default=None),
    from_cache: bool = Query(default=False),
    sample_rate: int | None = Query(default=None, ge=5, le=100),
    wait: bool = Query(default=False),
) -> dict:
    """兼容旧接口：手动触发某日分析。"""
    target = yesterday() if not day else date.fromisoformat(day)
    mgr = get_job_manager()

    if wait:
        record = mgr.run_now(
            day=target,
            sample_rate=sample_rate,
            from_cache=from_cache,
            schedule_name="手动执行",
            trigger="manual",
        )
        return {
            "status": record.status,
            "date": record.day,
            "session_count": record.session_count,
            "report_path": record.report_path,
            "error": record.error,
        }

    def _job() -> None:
        mgr.run_now(
            day=target,
            sample_rate=sample_rate,
            from_cache=from_cache,
            schedule_name="手动执行",
            trigger="manual",
        )

    background_tasks.add_task(_job)
    return {"status": "started", "date": target.isoformat()}


@app.get("/reports/{day}")
def get_report(
    day: str,
    format: str = Query(default="html", pattern="^(html|json|text)$"),
):
    cfg = get_settings()
    try:
        target = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid date") from exc

    report_dir = Path(cfg.reports_root) / target.isoformat()
    if format == "json":
        path = report_dir / "summary.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="summary not found")
        return PlainTextResponse(
            path.read_text(encoding="utf-8"), media_type="application/json"
        )

    html_path = report_dir / "report.html"
    if format != "text" and html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    txt_path = report_dir / "report.txt"
    if txt_path.exists():
        return PlainTextResponse(txt_path.read_text(encoding="utf-8"))

    raise HTTPException(status_code=404, detail="report not found")


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
