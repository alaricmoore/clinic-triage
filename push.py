#!/usr/bin/env python3
"""
clinic-triage / push.py
-----------------------
Stage 3. Files approved proposals from queue/ to sardinetracker's
POST /api/clinical/document.

    ./push.py                 walk the queue, approve one at a time
    ./push.py --dry-run       show exactly what would be sent; send nothing
    ./push.py --yes           file everything already marked approved, no prompts
    ./push.py --only <sha>    just this one

Nothing is sent without a 'y'. The server dedupes on the file's sha256, so a
retry after a dropped connection files nothing twice - which is what makes
--yes safe to re-run.

When a document is already filed and this proposal describes it differently,
push shows the difference field by field and offers to correct the filed copy.
Only fields the proposal actually has are offered: a proposal with no provider
means "could not determine one", so it will not blank a provider you typed in
by hand.

Config lives in config.json next to this file (see config.json.example).
Only stdlib.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE_DIR = os.path.join(HERE, "queue")
FILED_DIR = os.path.join(HERE, "filed")
CONFIG_PATH = os.path.join(HERE, "config.json")

SEND_FIELDS = ("date", "title", "doc_type", "specialty", "provider",
               "facility", "summary")

# If Cloudflare sits in front of the server, it blocks requests carrying
# urllib's default "Python-urllib/3.x" User-Agent with a 403 — which
# reads like an auth failure but happens before the request reaches Flask. Any
# non-default UA is accepted.
USER_AGENT = "clinic-triage/1.0"



def load_config(path=None):
    CONFIG_PATH = path or globals()["CONFIG_PATH"]
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"No config.json at {CONFIG_PATH}\n"
                 f"Copy config.json.example and fill in server, api_token, user_id.")
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    for key in ("server", "api_token", "user_id"):
        if not cfg.get(key):
            sys.exit(f"config.json is missing {key!r}")
    cfg["server"] = cfg["server"].rstrip("/")
    return cfg


# ============================================================
# multipart, by hand - stdlib has no encoder for it
# ============================================================

def encode_multipart(fields, file_field, filename, blob):
    """Build a multipart/form-data body. Returns (content_type, body bytes)."""
    boundary = "----clinic-triage-" + uuid.uuid4().hex
    out = bytearray()
    for key, value in fields.items():
        if value is None:
            continue                       # omit rather than send "None"
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += str(value).encode("utf-8") + b"\r\n"
    out += f"--{boundary}\r\n".encode()
    out += (f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n').encode()
    out += b"Content-Type: application/pdf\r\n\r\n"
    out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", bytes(out)


def post_document(cfg, prop, blob, on_duplicate=None):
    fields = {k: prop["fields"].get(k) for k in SEND_FIELDS}
    fields["user_id"] = cfg["user_id"]
    if on_duplicate:
        fields["on_duplicate"] = on_duplicate
    ctype, body = encode_multipart(
        fields, "pdf_file", prop["source_file"], blob)
    req = urllib.request.Request(
        cfg["server"] + "/api/clinical/document", data=body, method="POST",
        headers={"Content-Type": ctype,
                 "Authorization": "Bearer " + cfg["api_token"],
                 "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"error": e.reason}
    except urllib.error.URLError as e:
        return None, {"error": str(e.reason)}


# ============================================================
# review
# ============================================================

def show(prop):
    f = prop["fields"]
    print(f"\n  {prop['source_file']}   [{prop['status']}]")
    c = prop.get("classification")
    if c:
        note = ("   <- partial structure, read this one closely"
                if c.get("verdict") == "ambiguous" else "")
        print(f"  {c.get('verdict','?')}: {c.get('reason','')}{note}")
    print()
    for k in SEND_FIELDS:
        v = f.get(k)
        src = prop.get("field_sources", {}).get(k) or "not found"
        via = "model" if src.startswith("model") else "extracted"
        if v is None:
            print(f"    {k:<10} --")
            continue
        text = str(v)
        if k == "date" and "NOT corroborated" in str(src):
            via = "filename, UNCONFIRMED"
        first, rest = text[:64], text[64:]
        print(f"    {k:<10} {first:<64}  ({via})")
        while rest:
            print(f"    {'':<10} {rest[:64]}")
            rest = rest[64:]


def diff_against_filed(prop, stored):
    """Fields where this proposal differs from what is already filed.

    Only fields the proposal actually has count. A proposal with no provider
    means "I could not determine one", not "there is no provider" — treating it
    as the latter would blank a value entered by hand in the web interface.
    """
    out = []
    for k in SEND_FIELDS:
        new = (prop["fields"].get(k) or "").strip() if prop["fields"].get(k) else ""
        old = (stored.get(k) or "").strip() if stored.get(k) else ""
        if new and new != old:
            out.append((k, old, new))
    return out


def show_diff(rows):
    for k, old, new in rows:
        print(f"    {k}")
        print(f"      filed    {old or '--'}")
        print(f"      proposed {new}")


def edit(path):
    editor = os.environ.get("EDITOR", "nano")
    subprocess.call([editor, path])
    with open(path) as f:
        return json.load(f)


def ask(path, prop):
    """Returns 'file', 'skip', or 'quit'. May reload prop after an edit."""
    while True:
        try:
            a = input("\n  file it? [y]es / [n]o / [e]dit / [q]uit  ").strip().lower()
        except EOFError:
            return "quit", prop
        if a in ("y", "yes"):
            return "file", prop
        if a in ("n", "no", ""):
            return "skip", prop
        if a in ("q", "quit"):
            return "quit", prop
        if a in ("e", "edit"):
            try:
                prop = edit(path)
            except ValueError as e:
                print(f"  that left the file unparseable ({e}); not saved")
                continue
            show(prop)


def mark_filed(path, prop, response):
    os.makedirs(FILED_DIR, exist_ok=True)
    prop["status"] = "filed"
    prop["filed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prop["server_response"] = response
    dest = os.path.join(FILED_DIR, os.path.basename(path))
    with open(dest, "w") as f:
        json.dump(prop, f, indent=2)
    os.remove(path)
    return dest


def main():
    ap = argparse.ArgumentParser(
        description="File approved clinic-triage proposals to sardinetracker.")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be sent, send nothing")
    ap.add_argument("--yes", action="store_true",
                    help="no prompts; file every pending proposal")
    ap.add_argument("--only", help="only the proposal whose sha256 starts with this")
    ap.add_argument("--update-duplicates", action="store_true",
                    help="when a document is already filed and the proposal "
                         "differs, apply the change without asking")
    ap.add_argument("--config", help="use a different config.json (for testing)")
    a = ap.parse_args()

    cfg = load_config(a.config)
    if not os.path.isdir(QUEUE_DIR):
        sys.exit("No queue/ directory. Run triage.py first.")
    files = sorted(f for f in os.listdir(QUEUE_DIR) if f.endswith(".json"))
    if a.only:
        files = [f for f in files if f.startswith(a.only)]
    if not files:
        print("Nothing pending in the queue.")
        return

    print(f"clinic-triage push  ·  {len(files)} pending  ·  {cfg['server']}"
          f"  ·  user {cfg['user_id']}"
          + ("  ·  DRY RUN" if a.dry_run else ""))

    filed = skipped = failed = 0
    for name in files:
        path = os.path.join(QUEUE_DIR, name)
        with open(path) as fh:
            prop = json.load(fh)

        if prop["status"] in ("not_clinical", "needs_ocr", "too_short",
                              "excluded", "not_a_record"):
            print(f"\n  {prop['source_file']}: {prop['status']} — not filing.")
            skipped += 1
            continue

        src = prop.get("source_path")
        if not src or not os.path.exists(src):
            print(f"\n  {prop['source_file']}: source PDF is gone from "
                  f"{src!r} — cannot file.")
            failed += 1
            continue
        with open(src, "rb") as fh:
            blob = fh.read()

        # The PDF may have been replaced since triage ran; the description in
        # this proposal describes the old bytes, so re-triage rather than file
        # a summary that belongs to a different document.
        digest = hashlib.sha256(blob).hexdigest()
        if digest != prop["sha256"]:
            print(f"\n  {prop['source_file']}: file on disk no longer matches the "
                  f"proposal. Re-run triage.py --force for it.")
            failed += 1
            continue

        show(prop)

        if a.dry_run:
            print("\n  (dry run — not sent)")
            skipped += 1
            continue

        if a.yes:
            action = "file"
        else:
            action, prop = ask(path, prop)
        if action == "quit":
            print("\nStopped.")
            break
        if action == "skip":
            print("  left in the queue.")
            skipped += 1
            continue

        status, body = post_document(cfg, prop, blob)
        if not (status in (200, 201) and body.get("ok")):
            print(f"  FAILED  {status}  {body}")
            failed += 1
            continue

        if body.get("duplicate"):
            rows = diff_against_filed(prop, body.get("document") or {})
            if not rows:
                print(f"  already filed as #{body['id']}, nothing to change")
                mark_filed(path, prop, {"status": status, **body})
                filed += 1
                continue
            print(f"  already filed as #{body['id']}, and this differs:")
            show_diff(rows)
            if a.update_duplicates:
                do_update = True
            elif a.yes:
                do_update = False
                print("  left as filed (--yes does not overwrite; "
                      "use --update-duplicates)")
            else:
                try:
                    do_update = input("\n  update the filed copy? [y/N]  "
                                      ).strip().lower() in ("y", "yes")
                except EOFError:
                    do_update = False
            if not do_update:
                print("  left as filed.")
                skipped += 1
                continue
            status, body = post_document(cfg, prop, blob, on_duplicate="update")
            if not (status in (200, 201) and body.get("ok")):
                print(f"  FAILED  {status}  {body}")
                failed += 1
                continue
            print(f"  updated #{body['id']}: {', '.join(body.get('changed') or [])}")
        else:
            print(f"  filed as #{body['id']}  "
                  f"{cfg['server']}/clinical#documents")
        mark_filed(path, prop, {"status": status, **body})
        filed += 1

    print(f"\n{filed} filed, {skipped} skipped, {failed} failed.")


if __name__ == "__main__":
    main()
