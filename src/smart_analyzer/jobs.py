"""定时任务定义与执行历史（落盘到 session/jobs）。"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from smart_analyzer.config import Settings, get_settings
from smart_analyzer.pipeline import run_day, yesterday
from smart_analyzer.pull import purge_expired_logs

CST = timezone(timedelta(hours=8))
_MAX_RUNS = 200
_FRAC_RE = re.compile(
    r"(?P<phase>pull|scan)\s+(?P<cur>\d+)\s*/\s*(?P<total>\d+)",
    re.I,
)


@dataclass
class ScheduleSpec:
    id: str
    name: str
    hour: int = 3
    minute: int = 0
    sample_rate: int = 100
    from_cache: bool = False
    enabled: bool = True
    created_at: str = ""

    def cron_label(self) -> str:
        return f"每天 {self.hour:02d}:{self.minute:02d}"


@dataclass
class RunRecord:
    id: str
    schedule_id: str
    schedule_name: str
    day: str
    status: str  # running | done | error
    started_at: str
    finished_at: str = ""
    session_count: int | None = None
    report_path: str = ""
    error: str = ""
    trigger: str = "cron"  # cron | manual
    progress: str = ""
    progress_pct: float | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    progress_phase: str = ""


class JobManager:
    """进程内 BackgroundScheduler + JSON 持久化。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._lock = threading.Lock()
        self._jobs_dir = Path(self._settings.session_root) / "jobs"
        self._schedules_path = self._jobs_dir / "schedules.json"
        self._runs_path = self._jobs_dir / "runs.jsonl"
        self._schedules: dict[str, ScheduleSpec] = {}
        self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self._started = False
        # 运行中任务的实时进度（内存），list_runs 时合并进结果
        self._live_progress: dict[str, dict[str, Any]] = {}
        self._last_progress_flush: dict[str, float] = {}

    @property
    def scheduler(self) -> BackgroundScheduler:
        return self._scheduler

    def start(self) -> None:
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        self._load_schedules()
        if not self._schedules:
            self._seed_default()
        for spec in self._schedules.values():
            if spec.enabled:
                self._register(spec)
        self._scheduler.add_job(
            self._purge_expired_logs,
            CronTrigger(hour=4, minute=10, timezone="Asia/Shanghai"),
            id="purge-expired-logs",
            replace_existing=True,
            name="清理过期日志",
        )
        if not self._started:
            self._scheduler.start()
            self._started = True
        self._purge_expired_logs()

    def shutdown(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False

    def list_schedules(self) -> list[dict[str, Any]]:
        with self._lock:
            items = []
            for spec in sorted(
                self._schedules.values(), key=lambda s: s.created_at
            ):
                job = self._scheduler.get_job(spec.id)
                next_run = None
                if job and job.next_run_time is not None:
                    next_run = job.next_run_time.isoformat()
                items.append(
                    {
                        **asdict(spec),
                        "cron_label": spec.cron_label(),
                        "next_run_time": next_run,
                    }
                )
            return items

    def create_schedule(
        self,
        *,
        name: str,
        hour: int,
        minute: int,
        sample_rate: int = 100,
        from_cache: bool = False,
        enabled: bool = True,
    ) -> dict[str, Any]:
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("hour/minute out of range")
        if sample_rate not in (5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
            raise ValueError("invalid sample_rate")
        spec = ScheduleSpec(
            id=uuid.uuid4().hex[:12],
            name=name.strip() or "日分析",
            hour=hour,
            minute=minute,
            sample_rate=sample_rate,
            from_cache=from_cache,
            enabled=enabled,
            created_at=datetime.now(CST).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._schedules[spec.id] = spec
            self._save_schedules()
            if enabled:
                self._register(spec)
            job = self._scheduler.get_job(spec.id)
            next_run = (
                job.next_run_time.isoformat()
                if job and job.next_run_time is not None
                else None
            )
        return {
            **asdict(spec),
            "cron_label": spec.cron_label(),
            "next_run_time": next_run,
        }

    def delete_schedule(self, schedule_id: str) -> None:
        with self._lock:
            if schedule_id not in self._schedules:
                raise KeyError(schedule_id)
            del self._schedules[schedule_id]
            self._save_schedules()
            try:
                self._scheduler.remove_job(schedule_id)
            except Exception:
                pass

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        with self._lock:
            spec = self._schedules.get(schedule_id)
            if spec is None:
                raise KeyError(schedule_id)
            spec.enabled = enabled
            self._save_schedules()
            try:
                self._scheduler.remove_job(schedule_id)
            except Exception:
                pass
            if enabled:
                self._register(spec)
            job = self._scheduler.get_job(spec.id)
            next_run = (
                job.next_run_time.isoformat()
                if job and job.next_run_time is not None
                else None
            )
            return {**asdict(spec), "cron_label": spec.cron_label(), "next_run_time": next_run}

    def run_now(
        self,
        *,
        day: date | None = None,
        sample_rate: int | None = None,
        from_cache: bool = False,
        schedule_id: str = "",
        schedule_name: str = "手动执行",
        trigger: str = "manual",
    ) -> RunRecord:
        target = day or yesterday()
        rate = (
            sample_rate
            if sample_rate is not None
            else self._settings.normalized_sample_rate()
        )
        record = RunRecord(
            id=uuid.uuid4().hex[:12],
            schedule_id=schedule_id or "-",
            schedule_name=schedule_name,
            day=target.isoformat(),
            status="running",
            started_at=datetime.now(CST).isoformat(timespec="seconds"),
            trigger=trigger,
            progress="启动中",
            progress_phase="start",
            progress_pct=0.0,
        )
        self._append_run(record)
        self._set_live_progress(record)

        def on_progress(msg: str) -> None:
            phase, cur, total, pct = _parse_progress(msg)
            record.progress = msg
            record.progress_phase = phase
            record.progress_current = cur
            record.progress_total = total
            record.progress_pct = pct
            self._set_live_progress(record)
            self._maybe_flush_progress(record)

        try:
            summary = run_day(
                target,
                settings=self._settings,
                from_cache=from_cache,
                sample_rate=rate,
                on_progress=on_progress,
            )
            record.status = "done"
            record.session_count = int(summary.get("session_count") or 0)
            record.report_path = str(summary.get("report_path") or "")
            record.progress = "完成"
            record.progress_phase = "done"
            record.progress_pct = 100.0
        except Exception as exc:
            record.status = "error"
            record.error = f"{exc}\n{traceback.format_exc()[-1500:]}"
            record.progress = "失败"
            record.progress_phase = "error"
        record.finished_at = datetime.now(CST).isoformat(timespec="seconds")
        self._live_progress.pop(record.id, None)
        self._last_progress_flush.pop(record.id, None)
        self._update_run(record)
        return record

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        path = self._runs_path
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        rows.reverse()
        out = rows[:limit]
        with self._lock:
            live = dict(self._live_progress)
        for row in out:
            patch = live.get(str(row.get("id") or ""))
            if patch:
                row.update(patch)
        return out

    def _set_live_progress(self, record: RunRecord) -> None:
        with self._lock:
            self._live_progress[record.id] = {
                "status": record.status,
                "progress": record.progress,
                "progress_phase": record.progress_phase,
                "progress_pct": record.progress_pct,
                "progress_current": record.progress_current,
                "progress_total": record.progress_total,
            }

    def _maybe_flush_progress(self, record: RunRecord, min_interval: float = 1.0) -> None:
        """把进度落到 runs.jsonl，限流避免频繁写盘。"""
        now = time.monotonic()
        last = self._last_progress_flush.get(record.id, 0.0)
        if now - last < min_interval:
            return
        self._last_progress_flush[record.id] = now
        self._update_run(record)

    def _purge_expired_logs(self) -> None:
        purge_expired_logs(settings=self._settings)

    def _seed_default(self) -> None:
        spec = ScheduleSpec(
            id="daily-0300",
            name="每天凌晨分析昨天",
            hour=3,
            minute=0,
            sample_rate=self._settings.normalized_sample_rate(),
            from_cache=False,
            enabled=True,
            created_at=datetime.now(CST).isoformat(timespec="seconds"),
        )
        self._schedules[spec.id] = spec
        self._save_schedules()

    def _register(self, spec: ScheduleSpec) -> None:
        trigger = CronTrigger(
            hour=spec.hour, minute=spec.minute, timezone="Asia/Shanghai"
        )

        def _job(sid: str = spec.id) -> None:
            current = self._schedules.get(sid)
            if current is None or not current.enabled:
                return
            self.run_now(
                day=yesterday(),
                sample_rate=current.sample_rate,
                from_cache=current.from_cache,
                schedule_id=current.id,
                schedule_name=current.name,
                trigger="cron",
            )

        self._scheduler.add_job(
            _job,
            trigger=trigger,
            id=spec.id,
            replace_existing=True,
            name=spec.name,
        )

    def _load_schedules(self) -> None:
        if not self._schedules_path.exists():
            return
        try:
            raw = json.loads(self._schedules_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, dict) or "id" not in item:
                continue
            self._schedules[item["id"]] = ScheduleSpec(
                id=str(item["id"]),
                name=str(item.get("name") or "任务"),
                hour=int(item.get("hour", 3)),
                minute=int(item.get("minute", 0)),
                sample_rate=int(item.get("sample_rate", 100)),
                from_cache=bool(item.get("from_cache", False)),
                enabled=bool(item.get("enabled", True)),
                created_at=str(item.get("created_at") or ""),
            )

    def _save_schedules(self) -> None:
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        payload = [asdict(s) for s in self._schedules.values()]
        self._schedules_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _append_run(self, record: RunRecord) -> None:
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self._runs_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            self._trim_runs_unlocked()

    def _update_run(self, record: RunRecord) -> None:
        with self._lock:
            if not self._runs_path.exists():
                self._append_run(record)
                return
            lines = self._runs_path.read_text(encoding="utf-8").splitlines()
            updated: list[str] = []
            found = False
            for line in lines:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    updated.append(line)
                    continue
                if row.get("id") == record.id:
                    updated.append(json.dumps(asdict(record), ensure_ascii=False))
                    found = True
                else:
                    updated.append(line)
            if not found:
                updated.append(json.dumps(asdict(record), ensure_ascii=False))
            self._runs_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
            self._trim_runs_unlocked()

    def _trim_runs_unlocked(self) -> None:
        if not self._runs_path.exists():
            return
        lines = [
            ln for ln in self._runs_path.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        if len(lines) <= _MAX_RUNS:
            return
        self._runs_path.write_text(
            "\n".join(lines[-_MAX_RUNS:]) + "\n", encoding="utf-8"
        )


_manager: JobManager | None = None


def _parse_progress(
    msg: str,
) -> tuple[str, int | None, int | None, float | None]:
    """从进度文案解析 phase / current / total / percent。"""
    text = (msg or "").strip()
    m = _FRAC_RE.search(text)
    if m:
        cur = int(m.group("cur"))
        total = max(1, int(m.group("total")))
        phase = m.group("phase").lower()
        # pull 占 0–70%，scan 占 70–95%
        if phase == "pull":
            pct = round(70.0 * cur / total, 1)
        else:
            pct = round(70.0 + 25.0 * cur / total, 1)
        return phase, cur, total, min(99.0, pct)
    lower = text.lower()
    if lower.startswith("discover") or "discovered=" in lower:
        return "discover", None, None, 5.0
    if lower.startswith("from-cache"):
        return "cache", None, None, 20.0
    if lower.startswith("run_day"):
        return "start", None, None, 1.0
    if lower.startswith("wrote") or "report" in lower:
        return "report", None, None, 98.0
    return "running", None, None, None


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
