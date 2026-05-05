# Slack Reminder Cron Hardening Sample

## Purpose

This is a small sanitized MVP sample for a job involving Python automation, cron hardening, Slack delivery, and AI-assisted development verification.

It demonstrates how I would convert a fragile reminder cron into a deterministic Python script with:

- idempotency
- retry handling
- post-send verification
- structured state transitions
- JSONL logs
- SQLite run tracking
- no real credentials or private client data

## Why this sample fits the requested work

The target failure pattern was:

> delivery plumbing allows contradictory states — message posts successfully but the run is logged as error.

This sample avoids that by treating delivery as a small state machine:

`pending -> sending -> sent -> verified`

A successful Slack post is not overwritten by a later generic exception. The Slack timestamp is stored, and post-send verification is treated as its own step.

## Files

- `slack_reminder_hardened.py` — deterministic cron-style reminder job
- `README.md` — explanation and verification notes

## Run

```bash
python slack_reminder_hardened.py
```

Expected output:

```json
{
  "run_key": "...",
  "slack_ts": "...",
  "status": "verified"
}
```

## Simulate transient Slack failures

```bash
SIMULATE_SLACK_FAILURES=2 python slack_reminder_hardened.py
```

The first two attempts fail, then the third succeeds if `MAX_RETRIES` is at least 3.

## Test idempotency

Run the same command twice on the same day:

```bash
python slack_reminder_hardened.py
python slack_reminder_hardened.py
```

The second run should return:

```json
{
  "status": "already_verified"
}
```

This prevents duplicate reminder posts.

## Logs/artifacts

The script creates:

- `reminder_runs.sqlite3`
- `reminder_runs.jsonl`

These are local run artifacts and can be deleted between tests.

## AI-assisted build approach

The way I would use Claude Code/Cursor on a real version:

1. Ask it to inspect the existing cron and list all dependencies, inputs, outputs, and side effects.
2. Ask it to isolate the business rule from the delivery mechanism.
3. Ask it to refactor the reminder into deterministic Python with an idempotency key.
4. Ask it to add retry handling around Slack delivery.
5. Ask it to add post-send verification using Slack message timestamp.
6. Ask it to produce a test plan covering success, duplicate run, transient failure, retry exhaustion, and contradictory logging states.
7. Manually inspect the final code and run targeted tests before shipping.

## Production notes

For a real Slack integration, replace `MockSlackClient` with Slack SDK calls:

- `chat_postMessage`
- `conversations_history` or `conversations_replies`
- explicit handling for rate limits and Slack API error codes

Secrets should be loaded from environment variables or a secret manager, never committed.
