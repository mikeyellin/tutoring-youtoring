#!/usr/bin/env python3
"""Tutoring booking pipeline daemon.

Flow:
  1. Poll Gmail for Formspree submission emails
  2. Parse student info (name, email, subject, session type, preferred time)
  3. Send confirmation email with session details + payment instructions
  4. Create Todoist task immediately (UNPAID)
  5. Mark email as processed

Usage:
    python booking_daemon.py              # Run continuously (60s poll interval)
    python booking_daemon.py --dry-run    # Test without sending emails or creating tasks
    python booking_daemon.py --once       # Single poll cycle then exit
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Add Gmail client to path
sys.path.insert(0, "/home/jacob/ReThinker/Refactor/Paradigm/implementations/Deploy/impl")
from gmail_client import GmailClient  # noqa: E402

# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #

STATE_FILE = Path("/home/jacob/tutoring-site/booking_state.json")
POLL_INTERVAL = 60  # seconds

CONFIRMATION_EMAIL = """\
Hi {name},

Got your booking request — here are the details:

  Subject:    {subject}
  Session:    {session_type}
  Time:       {preferred_datetime}

To lock in your spot, please send payment before the session:

  A secure payment link is included below. Please complete payment before your session.

Once payment goes through I'll send you the Google Meet link and confirm your time.

Talk soon,
Jacob
"""

# ------------------------------------------------------------------ #
# State helpers
# ------------------------------------------------------------------ #

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_formspree_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ------------------------------------------------------------------ #
# Email parsing
# ------------------------------------------------------------------ #

_FIELD_PATTERNS = {
    "name":               r"(?:^|\n)\s*name\s*[:\-]\s*(.+?)(?:\n|$)",
    "email":              r"(?:^|\n)\s*email\s*[:\-]\s*(.+?)(?:\n|$)",
    "subject":            r"(?:^|\n)\s*subject\s*[:\-]\s*(.+?)(?:\n|$)",
    "session_type":       r"(?:^|\n)\s*session[_\s]?type\s*[:\-]\s*(.+?)(?:\n|$)",
    "preferred_datetime": r"(?:^|\n)\s*preferred[_\s]?datetime\s*[:\-]\s*(.+?)(?:\n|$)",
    "message":            r"(?:^|\n)\s*message\s*[:\-]\s*(.+?)(?:\n|$)",
}


def parse_formspree_body(body: str) -> dict:
    fields = {}
    for key, pattern in _FIELD_PATTERNS.items():
        m = re.search(pattern, body, re.IGNORECASE)
        if m:
            fields[key] = m.group(1).strip()
    return fields


# ------------------------------------------------------------------ #
# Pipeline
# ------------------------------------------------------------------ #

def poll_formspree(client: GmailClient, state: dict, dry_run: bool) -> None:
    emails = client.search_emails("from:formspree.io newer_than:30d", max_results=30)

    for msg in emails:
        msg_id = msg["id"]
        if msg_id in state["processed_formspree_ids"]:
            continue

        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] New Formspree submission: {msg['subject']}")

        body = client.get_full_body(msg_id)
        fields = parse_formspree_body(body)

        student_email = fields.get("email", "").strip().lower()
        student_name = fields.get("name", "Student")

        if not student_email:
            print(f"  WARNING: could not parse student email — body snippet: {body[:200]!r}")
            state["processed_formspree_ids"].append(msg_id)
            continue

        subject = fields.get("subject", "(not specified)")
        session_type = fields.get("session_type", "(not specified)")
        preferred_datetime = fields.get("preferred_datetime", "TBD")

        print(f"  Student:  {student_name} <{student_email}>")
        print(f"  Subject:  {subject}")
        print(f"  Session:  {session_type}")
        print(f"  Time:     {preferred_datetime}")

        # 1. Send confirmation email
        email_body = CONFIRMATION_EMAIL.format(
            name=student_name,
            subject=subject,
            session_type=session_type,
            preferred_datetime=preferred_datetime,
        )

        if dry_run:
            print(f"  [DRY RUN] Would send confirmation to {student_email}")
            print(f"  --- email preview ---\n{email_body}  ---")
        else:
            client.send_email(
                to=student_email,
                subject=f"Tutoring session confirmed — {subject}",
                body=email_body,
            )
            print(f"  Sent confirmation to {student_email}")

        # 2. Create Todoist task immediately (UNPAID)
        task_text = f"Tutoring: {student_name} - {subject} - {preferred_datetime} - UNPAID"

        if dry_run:
            print(f"  [DRY RUN] Would create task: {task_text}")
        else:
            result = subprocess.run(
                ["todo", "add", task_text,
                 "--project", "Deploy",
                 "--label", "urgent",
                 "--priority", "2"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                print(f"  Created Todoist task: {task_text}")
            else:
                print(f"  ERROR creating task: {result.stderr.strip()}", file=sys.stderr)

        state["processed_formspree_ids"].append(msg_id)


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #

def main() -> None:
    parser = argparse.ArgumentParser(description="Tutoring booking pipeline daemon")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Connect to Gmail and parse, but do not send emails or create tasks",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one poll cycle and exit",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("=" * 60)
        print("  DRY RUN — no emails sent, no Todoist tasks created")
        print("=" * 60)

    print("Connecting to Gmail...")
    client = GmailClient()
    print("Connected.\n")

    state = load_state()
    print(f"State: {len(state['processed_formspree_ids'])} processed bookings.\n")

    if args.dry_run or args.once:
        poll_formspree(client, state, dry_run=args.dry_run)
        if not args.dry_run:
            save_state(state)
            print("\nState saved.")
        else:
            print("\n(State NOT saved — dry run)")
        return

    print(f"Daemon started. Polling every {POLL_INTERVAL}s. Ctrl+C to stop.\n")
    while True:
        try:
            poll_formspree(client, state, dry_run=False)
            save_state(state)
        except KeyboardInterrupt:
            print("\nShutting down.")
            break
        except Exception as exc:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] ERROR: {exc}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
