"""Bộ lập lịch sync chạy nền bên trong Dashboard server.

Gọi `run.sh sync` dưới dạng subprocess để dùng chung cơ chế khóa (chống chạy chồng
với cron/lệnh tay) và ghi log vào data/logs/sync.log.
"""
import asyncio
import os
import signal
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
RUN_SH = PROJECT_DIR / "run.sh"
LOG_FILE = PROJECT_DIR / "data" / "logs" / "sync.log"

EXIT_MEANING = {0: "ok", 2: "login_required", 3: "source_error", 75: "busy"}


class SyncScheduler:
    def __init__(self, interval_minutes: int):
        self.interval = max(0, interval_minutes)
        self.phase: str | None = None
        self.running = False
        self.last_started: datetime | None = None
        self.last_finished: datetime | None = None
        self.last_status: str | None = None
        self.next_run: datetime | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._target: tuple[str | None, str | None] = (None, None)

    def _next_tick_minutes(self) -> int:
        if self.interval <= 0:
            return 0
        tick = self.interval
        try:
            from app.config import load_projects

            for proj in load_projects():
                p_int = int(proj.get("scan_interval_minutes") or self.interval)
                if p_int >= 1:
                    tick = min(tick, p_int)
        except Exception:
            pass
        return max(1, tick)

    async def run_once(
        self,
        *,
        startup: bool = False,
        scheduled: bool = False,
        project_key=None,
        cluster_id=None,
    ) -> str:
        if self.running:
            return "already_running"
        self.running = True
        self.last_started = datetime.now()
        self.next_run = None
        self.phase = "starting" if startup else "running_cycle"
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as log:
                args = []
                if scheduled and not startup and not project_key and not cluster_id:
                    args.append("--scheduled")
                if project_key:
                    args.extend(["--project", project_key])
                if cluster_id:
                    args.extend(["--cluster", cluster_id])
                proc = await asyncio.create_subprocess_exec(
                    "/bin/bash", str(RUN_SH), "startup-cycle" if startup else "cycle", *args,
                    cwd=str(PROJECT_DIR), stdout=asyncio.subprocess.PIPE if startup else log,
                    stderr=log, start_new_session=True,
                )
                try:
                    if startup:
                        async for line in proc.stdout:
                            phase = line.decode().strip()
                            if phase in {"checking_session", "awaiting_login", "running_cycle"}:
                                self.phase = phase
                    rc = await proc.wait()
                except asyncio.CancelledError:
                    # The shell, browser check and cycle belong to this server.
                    if proc.returncode is None:
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                        except (ProcessLookupError, PermissionError):
                            pass
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=5)
                        except asyncio.TimeoutError:
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except (ProcessLookupError, PermissionError):
                                pass
                            await proc.wait()
                    raise
            self.last_status = EXIT_MEANING.get(rc, f"error (exit {rc})")
        except Exception as exc:  # surface real failure, never fake success
            self.last_status = f"error ({exc})"
        finally:
            self.running = False
            self.phase = None
            self.last_finished = datetime.now()
        return self.last_status

    def trigger_now(self, project_key=None, cluster_id=None) -> bool:
        if self.running or self._wake.is_set():
            return False
        self._target = (project_key, cluster_id)
        self._wake.set()
        return True

    async def _loop(self, initial_delay_minutes: int, startup: bool) -> None:
        if startup:
            await self.run_once(startup=True)
            if self.interval == 0:
                return
        delay = timedelta(minutes=initial_delay_minutes)
        while True:
            self.next_run = datetime.now() + delay
            timed_out = False
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay.total_seconds())
            except asyncio.TimeoutError:
                timed_out = True
            self._wake.clear()
            project_key, cluster_id = self._target
            self._target = (None, None)
            if project_key:
                await self.run_once(project_key=project_key, cluster_id=cluster_id)
            elif timed_out:
                await self.run_once(scheduled=True)
            else:
                await self.run_once()
            delay = timedelta(minutes=self._next_tick_minutes())

    def start(self, initial_delay_minutes: int | None = None, *, startup: bool = False) -> None:
        if self._task is None:
            first = self.interval if initial_delay_minutes is None else initial_delay_minutes
            self._task = asyncio.create_task(self._loop(first, startup))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def status(self) -> dict:
        fmt = lambda d: d.isoformat(timespec="seconds") if d else None
        return {
            "enabled": self.interval > 0 and self._task is not None,
            "phase": self.phase,
            "interval_minutes": self.interval,
            "running": self.running,
            "last_started": fmt(self.last_started),
            "last_finished": fmt(self.last_finished),
            "last_status": self.last_status,
            "next_run": fmt(self.next_run),
        }


def scheduler_from_env() -> SyncScheduler | None:
    """CRASHPILOT_SYNC_INTERVAL_MIN=0 tắt scheduler; mặc định 60 phút."""
    interval = int(os.environ.get("CRASHPILOT_SYNC_INTERVAL_MIN", "60"))
    return SyncScheduler(interval) if interval > 0 or os.environ.get("CRASHPILOT_STARTUP_CYCLE") == "1" else None
