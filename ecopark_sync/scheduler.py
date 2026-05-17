import json
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import env


class SyncScheduler:
    def __init__(self, interval_seconds=None, run_on_start=None, start_delay_seconds=None):
        self.interval_seconds = interval_seconds or int(env("SYNC_INTERVAL_SECONDS", str(24 * 60 * 60)))
        self.run_on_start = run_on_start
        if self.run_on_start is None:
            self.run_on_start = env("SYNC_RUN_ON_START", "true").lower() in {"1", "true", "yes", "y"}
        self.start_delay_seconds = start_delay_seconds
        if self.start_delay_seconds is None:
            self.start_delay_seconds = int(env("SYNC_START_DELAY_SECONDS", "0"))

        if self.interval_seconds < 60:
            raise RuntimeError("SYNC_INTERVAL_SECONDS must be at least 60")
        if self.start_delay_seconds < 0:
            raise RuntimeError("SYNC_START_DELAY_SECONDS must be greater than or equal to 0")

        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start_background(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run_forever, name="ecopark-sync-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def run_forever(self):
        if self.start_delay_seconds:
            self._log(f"first scheduler tick in {self.start_delay_seconds}s")
            if self._stop_event.wait(self.start_delay_seconds):
                return

        first_run = True
        while not self._stop_event.is_set():
            if first_run and not self.run_on_start:
                self._log(f"next sync in {self.interval_seconds}s")
                if self._stop_event.wait(self.interval_seconds):
                    return

            result = self.run_once_safely()
            if result is not None:
                self._log(json.dumps(result, ensure_ascii=False))

            first_run = False
            self._log(f"next sync in {self.interval_seconds}s")
            if self._stop_event.wait(self.interval_seconds):
                return

    def run_once_safely(self):
        if not self._lock.acquire(blocking=False):
            self._log("sync skipped: previous run is still active")
            return None

        try:
            return run_once_recording_errors()
        except Exception as exc:
            self._log(f"sync failed: {exc}")
            return None
        finally:
            self._lock.release()

    def _log(self, message):
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def run_once():
    from .client import fetch_snapshot
    from .syncer import sync_snapshot

    snapshot = fetch_snapshot()
    result = sync_snapshot(snapshot)
    if env("SYNC_EXPORT_AFTER_SYNC", "false").lower() in {"1", "true", "yes", "y"}:
        maybe_export_google_sheets(result)
    return result


def maybe_export_google_sheets(result=None):
    if env("GOOGLE_SHEETS_EXPORT_ENABLED", "false").lower() not in {"1", "true", "yes", "y"}:
        return None

    from .sheets import export_to_google_sheets

    try:
        export_result = export_to_google_sheets()
    except Exception as exc:
        if result is not None:
            result["google_sheets_error"] = str(exc)
        return None

    if result is not None:
        result["google_sheets"] = export_result
    return export_result


def run_once_recording_errors():
    try:
        return run_once()
    except Exception as exc:
        error_text = str(exc)
        try:
            from .syncer import record_failed_run

            run_id = record_failed_run(error_text)
            raise RuntimeError(f"{error_text} (recorded as sync run #{run_id})") from exc
        except RuntimeError:
            raise
        except Exception as log_exc:
            raise RuntimeError(f"{error_text} (could not record sync error: {log_exc})") from exc


def run_forever():
    make_scheduler().run_forever()


def make_scheduler():
    if env("SCHEDULE_MODE", "daily").lower() == "interval":
        return SyncScheduler()
    return DailyScheduler()


class DailyScheduler:
    def __init__(self):
        self.timezone = ZoneInfo(env("SCHEDULE_TIMEZONE", "Asia/Novosibirsk"))
        self.sync_time = parse_daily_time(env("SYNC_DAILY_AT", "13:00"))
        self.export_time = parse_daily_time(env("GOOGLE_SHEETS_DAILY_AT", "13:10"))
        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start_background(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run_forever, name="ecopark-daily-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def run_forever(self):
        jobs = [
            ("sync", self.sync_time, self.run_sync_safely),
            ("google_sheets", self.export_time, self.run_export_safely),
        ]
        self._log(
            "daily scheduler: "
            + ", ".join(f"{name} at {format_daily_time(job_time)}" for name, job_time, _handler in jobs)
        )

        while not self._stop_event.is_set():
            now = datetime.now(self.timezone)
            name, run_at, handler = min(
                (
                    (name, next_run_at(now, job_time), handler)
                    for name, job_time, handler in jobs
                ),
                key=lambda item: item[1],
            )
            wait_seconds = max(0, (run_at - now).total_seconds())
            self._log(f"next {name} at {run_at.isoformat(timespec='minutes')}")
            if self._stop_event.wait(wait_seconds):
                return
            handler()

    def run_sync_safely(self):
        if not self._lock.acquire(blocking=False):
            self._log("sync skipped: previous job is still active")
            return
        try:
            result = run_once_recording_errors()
            self._log(json.dumps({"sync": result}, ensure_ascii=False))
        except Exception as exc:
            self._log(f"sync failed: {exc}")
        finally:
            self._lock.release()

    def run_export_safely(self):
        if not self._lock.acquire(blocking=False):
            self._log("google sheets export skipped: previous job is still active")
            return
        try:
            result = maybe_export_google_sheets()
            self._log(json.dumps({"google_sheets": result}, ensure_ascii=False))
        except Exception as exc:
            self._log(f"google sheets export failed: {exc}")
        finally:
            self._lock.release()

    def _log(self, message):
        print(f"[{datetime.now(self.timezone).isoformat(timespec='seconds')}] {message}", flush=True)


def parse_daily_time(value):
    try:
        hour, minute = str(value).strip().split(":", 1)
        return int(hour), int(minute)
    except Exception as exc:
        raise RuntimeError(f"Invalid daily time '{value}', expected HH:MM") from exc


def next_run_at(now, job_time):
    hour, minute = job_time
    run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run_at <= now:
        run_at += timedelta(days=1)
    return run_at


def format_daily_time(job_time):
    hour, minute = job_time
    return f"{hour:02d}:{minute:02d}"
