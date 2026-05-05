#!/usr/bin/env python3
"""
Slack Reminder Cron Hardening Sample

Purpose:
    Demonstrates how to convert a fragile reminder cron into a deterministic,
    idempotent, retry-safe Python job with post-send verification.

This sample uses a mock Slack client by default.
No real Slack token, workspace, or channel is required.

Run:
    python slack_reminder_hardened.py

Optional:
    SIMULATE_SLACK_FAILURES=2 python slack_reminder_hardened.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


DB_PATH = Path(os.getenv("REMINDER_DB_PATH", "reminder_runs.sqlite3"))
LOG_PATH = Path(os.getenv("REMINDER_LOG_PATH", "reminder_runs.jsonl"))

REMINDER_CHANNEL = os.getenv("REMINDER_CHANNEL", "#operations")
REMINDER_TEXT = os.getenv(
    "REMINDER_TEXT",
    "Reminder: please review today’s open tasks and update any blockers."
)

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_SLEEP_SECONDS = float(os.getenv("RETRY_SLEEP_SECONDS", "0.25"))


@dataclass(frozen=True)
class ReminderJob:
    """Small deterministic job definition for one reminder run."""
    channel: str
    text: str
    scheduled_date: str

    @property
    def run_key(self) -> str:
        """
        Idempotency key.

        In production, this could include:
        - job name
        - target date/time bucket
        - channel ID
        - normalized message purpose
        """
        raw = f"daily-reminder|{self.scheduled_date}|{self.channel}|{self.text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class MockSlackClient:
    """
    Mock Slack client for sample/demo use.

    Set SIMULATE_SLACK_FAILURES=N to force the first N attempts to fail.
    """

    def __init__(self) -> None:
        self.failures_remaining = int(os.getenv("SIMULATE_SLACK_FAILURES", "0"))

    def post_message(self, channel: str, text: str) -> Dict[str, Any]:
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            return {
                "ok": False,
                "error": "simulated_transient_error",
                "ts": None,
                "channel": channel,
            }

        timestamp = f"{int(time.time())}.{int(time.time_ns() % 1_000_000):06d}"
        return {
            "ok": True,
            "error": None,
            "ts": timestamp,
            "channel": channel,
            "message": {"text": text},
        }

    def verify_message(self, channel: str, ts: str) -> bool:
        # In production, call conversations.history or conversations.replies
        # and confirm the message timestamp exists in the expected channel.
        return bool(channel and ts)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, **payload: Any) -> None:
    record = {"time": utc_now(), "event": event, **payload}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminder_runs (
            run_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            channel TEXT NOT NULL,
            message_text TEXT NOT NULL,
            slack_ts TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def get_existing_run(conn: sqlite3.Connection, run_key: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT run_key, status, channel, message_text, slack_ts, attempts, last_error
        FROM reminder_runs
        WHERE run_key = ?
        """,
        (run_key,),
    ).fetchone()

    if not row:
        return None

    return {
        "run_key": row[0],
        "status": row[1],
        "channel": row[2],
        "message_text": row[3],
        "slack_ts": row[4],
        "attempts": row[5],
        "last_error": row[6],
    }


def create_pending_run(conn: sqlite3.Connection, job: ReminderJob) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO reminder_runs
            (run_key, status, channel, message_text, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (job.run_key, "pending", job.channel, job.text, now, now),
    )
    conn.commit()


def update_run(
    conn: sqlite3.Connection,
    run_key: str,
    *,
    status: str,
    attempts: Optional[int] = None,
    slack_ts: Optional[str] = None,
    last_error: Optional[str] = None,
) -> None:
    current = get_existing_run(conn, run_key)
    if current is None:
        raise RuntimeError(f"Cannot update missing run_key={run_key}")

    new_attempts = current["attempts"] if attempts is None else attempts

    conn.execute(
        """
        UPDATE reminder_runs
        SET status = ?,
            attempts = ?,
            slack_ts = COALESCE(?, slack_ts),
            last_error = ?,
            updated_at = ?
        WHERE run_key = ?
        """,
        (status, new_attempts, slack_ts, last_error, utc_now(), run_key),
    )
    conn.commit()


def send_with_retry(
    conn: sqlite3.Connection,
    client: MockSlackClient,
    job: ReminderJob,
) -> Dict[str, Any]:
    """
    Sends the Slack message with retry and post-send verification.

    State machine:
        pending -> sending -> sent -> verified
        pending/sending -> failed only when retries are exhausted

    Important:
        A successful Slack post is never overwritten by a later generic exception.
        Verification is a separate state transition.
    """
    run = get_existing_run(conn, job.run_key)

    if run and run["status"] == "verified":
        log_event("skip_already_verified", run_key=job.run_key, slack_ts=run["slack_ts"])
        return {"status": "already_verified", "run_key": job.run_key, "slack_ts": run["slack_ts"]}

    if run is None:
        create_pending_run(conn, job)
        run = get_existing_run(conn, job.run_key)

    attempts = int(run["attempts"])

    while attempts < MAX_RETRIES:
        attempts += 1
        update_run(conn, job.run_key, status="sending", attempts=attempts, last_error=None)
        log_event("send_attempt", run_key=job.run_key, attempt=attempts, channel=job.channel)

        response = client.post_message(job.channel, job.text)

        if not response.get("ok"):
            error = str(response.get("error") or "unknown_slack_error")
            update_run(conn, job.run_key, status="pending", attempts=attempts, last_error=error)
            log_event("send_failed_transient", run_key=job.run_key, attempt=attempts, error=error)
            time.sleep(RETRY_SLEEP_SECONDS)
            continue

        slack_ts = str(response["ts"])
        update_run(conn, job.run_key, status="sent", attempts=attempts, slack_ts=slack_ts, last_error=None)
        log_event("send_succeeded", run_key=job.run_key, attempt=attempts, slack_ts=slack_ts)

        verified = client.verify_message(job.channel, slack_ts)
        if verified:
            update_run(conn, job.run_key, status="verified", attempts=attempts, slack_ts=slack_ts, last_error=None)
            log_event("post_send_verified", run_key=job.run_key, slack_ts=slack_ts)
            return {"status": "verified", "run_key": job.run_key, "slack_ts": slack_ts}

        update_run(
            conn,
            job.run_key,
            status="failed",
            attempts=attempts,
            slack_ts=slack_ts,
            last_error="post_send_verification_failed",
        )
        log_event("post_send_verification_failed", run_key=job.run_key, slack_ts=slack_ts)
        return {"status": "failed", "run_key": job.run_key, "error": "post_send_verification_failed"}

    update_run(
        conn,
        job.run_key,
        status="failed",
        attempts=attempts,
        last_error="retry_exhausted",
    )
    log_event("retry_exhausted", run_key=job.run_key, attempts=attempts)
    return {"status": "failed", "run_key": job.run_key, "error": "retry_exhausted"}


def main() -> int:
    scheduled_date = os.getenv("SCHEDULED_DATE") or datetime.now(timezone.utc).date().isoformat()

    job = ReminderJob(
        channel=REMINDER_CHANNEL,
        text=REMINDER_TEXT,
        scheduled_date=scheduled_date,
    )

    log_event("job_started", run_key=job.run_key, scheduled_date=scheduled_date)

    conn = connect_db()
    client = MockSlackClient()
    result = send_with_retry(conn, client, job)

    log_event("job_finished", **result)
    print(json.dumps(result, indent=2, sort_keys=True))

    return 0 if result["status"] in {"verified", "already_verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
