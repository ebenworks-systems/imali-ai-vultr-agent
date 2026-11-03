"""The worker: runs queued month-end jobs and removes copies whose two hours are up.

It waits on Postgres LISTEN/NOTIFY, so a job starts as soon as the owner asks for it, and it
polls every few seconds in case a notification is missed. Run it with `python -m app.worker`.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import psycopg

from . import close_job, db, jobs, limits, services, workspace
from .config import settings

_stop = False


def _on_signal(*_a) -> None:
    global _stop
    _stop = True


def run_one() -> bool:
    """Run one queued job if there is one. Returns whether a job ran."""
    with db.tx() as c:
        job = jobs.claim_next(c)
    if job is None:
        return False
    svc = services.factory(str(job["workspace_id"]))
    if job["kind"] == "close_month":
        close_job.run_close(job["id"], svc)
    else:
        close_job.run_post(job["id"], svc)
    return True


def tidy() -> None:
    with db.tx() as c:
        workspace.remove_expired(c)
        limits.prune(c)
        # A job left running by a stopped worker is failed after ten minutes, so it is not stuck.
        c.execute(
            "UPDATE jobs SET status = 'failed', finished_at = now() WHERE status = 'running' "
            "AND started_at < now() - interval '10 minutes'"
        )


def _serve() -> None:
    listen = psycopg.connect(settings().database_url, autocommit=True)
    try:
        listen.execute("LISTEN month_end_jobs")
        print("worker ready", file=sys.stderr, flush=True)
        last_tidy = 0.0
        while not _stop:
            while run_one():
                if _stop:
                    break
            if time.monotonic() - last_tidy > 60:
                tidy()
                last_tidy = time.monotonic()
            for _n in listen.notifies(timeout=3.0, stop_after=1):
                break
    finally:
        listen.close()


def main() -> None:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    wait = 1.0
    while not _stop:
        try:
            db.migrate()
            _serve()
        except psycopg.OperationalError:
            # The database went away (a restart, say): wait and connect again rather than exit.
            print(f"database unavailable, retrying in {wait:.0f} s", file=sys.stderr, flush=True)
            db.close_pool()
            time.sleep(wait)
            wait = min(wait * 2, 30.0)
            continue
        wait = 1.0
    db.close_pool()


if __name__ == "__main__":
    if os.environ.get("WORKER_ONCE"):
        db.migrate()
        while run_one():
            pass
    else:
        main()
