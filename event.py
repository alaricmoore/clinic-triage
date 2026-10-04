#!/usr/bin/env python3
"""
event.py — record a clinical event that left no PDF behind.

A visit, lab draw or scan that produced no paperwork never passes through the
triage gates, so it never reaches the record. This asks for the same fields as
the event form on /clinical, shows what it will send, and posts it to
/api/clinical/event only after a y.

    ./event.py              ask, show, confirm, send
    ./event.py --dry-run    ask and show; send nothing

Providers are offered from the cached clinician roster (state/clinicians.json,
refreshed by `clinic-triage roster`), and picking one fills in the clinic.
Dates take YYYY-MM-DD, "today" or "yesterday". The server refuses a second
event with the same date, type and provider, so re-running after a dropped
connection is safe.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

from push import USER_AGENT, load_config

HERE = os.path.dirname(os.path.abspath(__file__))
ROSTER_PATH = os.path.join(HERE, "state", "clinicians.json")

# Must match CLINICAL_EVENT_TYPES on the server (routes/api.py).
EVENT_TYPES = ("encounter", "biopsy", "injection", "procedure", "lab draw",
               "imaging", "ER visit", "hospitalization", "other")


def ask(prompt, default=None):
    shown = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{shown}: ").strip()
    except EOFError:
        sys.exit("\ncancelled")
    return answer or default


def parse_date(text):
    text = (text or "").strip().lower()
    if text == "today":
        return date.today().isoformat()
    if text == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()
    return date.fromisoformat(text).isoformat()


def ask_date(prompt, default=None, required=True):
    while True:
        text = ask(prompt, default)
        if not text and not required:
            return None
        try:
            return parse_date(text)
        except ValueError:
            print("  use YYYY-MM-DD, today or yesterday")


def ask_choice(prompt, options, default=None):
    for i, opt in enumerate(options, 1):
        print(f"  {i:>2}  {opt}")
    while True:
        text = ask(prompt, default)
        if text in options:
            return text
        if text and text.isdigit() and 1 <= int(text) <= len(options):
            return options[int(text) - 1]
        print(f"  pick 1-{len(options)}")


def ask_provider(roster):
    """A roster number, a typed name, or blank. Returns (provider, clinic)."""
    for i, c in enumerate(roster, 1):
        extra = ", ".join(x for x in (c.get("specialty"), c.get("clinic_name")) if x)
        print(f"  {i:>2}  {c['name']}" + (f"  ({extra})" if extra else ""))
    text = ask("Provider (number, or type a name; blank for none)")
    if text and text.isdigit() and 1 <= int(text) <= len(roster):
        c = roster[int(text) - 1]
        return c["name"], c.get("clinic_name")
    return text, None


def post_event(cfg, event):
    req = urllib.request.Request(
        cfg["server"] + "/api/clinical/event", method="POST",
        data=json.dumps({"user_id": cfg["user_id"], **event}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["api_token"],
                 "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"error": e.reason}
    except urllib.error.URLError as e:
        return None, {"error": str(e.reason)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="show the event; send nothing")
    args = ap.parse_args()
    cfg = load_config()
    roster = []
    if os.path.exists(ROSTER_PATH):
        with open(ROSTER_PATH) as f:
            roster = json.load(f)

    print("New clinical event  (Ctrl-C to cancel)\n")
    event = {"date": ask_date("Date", "today")}
    print("\nType:")
    event["event_type"] = ask_choice("Type", EVENT_TYPES, "encounter")
    print("\nProvider:")
    event["provider"], clinic = ask_provider(roster)
    event["facility"] = ask("\nFacility", clinic)
    event["notes"] = ask("Notes (one line: why, what was found or ordered)")
    event["follow_up_date"] = ask_date("Follow-up date (blank for none)", required=False)
    event = {k: v for k, v in event.items() if v}

    print("\nWill send:")
    for k, v in event.items():
        print(f"  {k:<15} {v}")
    if args.dry_run:
        print("\n(dry run: nothing sent)")
        return
    if (ask("\nSend it? y/n", "n") or "").lower() != "y":
        print("not sent")
        return

    status, reply = post_event(cfg, event)
    if status == 201:
        print(f"recorded (event {reply['id']})")
    elif status == 200 and reply.get("duplicate"):
        print(f"already recorded (event {reply['id']}); nothing changed")
    elif status == 404:
        sys.exit("404: the server doesn't have /api/clinical/event yet. Deploy private-track first.")
    else:
        sys.exit(f"not recorded: {status} {reply}")


if __name__ == "__main__":
    main()
