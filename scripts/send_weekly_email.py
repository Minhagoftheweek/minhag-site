#!/usr/bin/env python3
"""Creates the weekly Minhag of the Week email in Mailchimp.

Run by .github/workflows/weekly-email.yml. Two steps:

    python scripts/send_weekly_email.py prepare 23
        Builds the email's images and PDF so the workflow can publish them.

    python scripts/send_weekly_email.py send 23 --mode test --test-email you@example.com
    python scripts/send_weekly_email.py send 23 --mode schedule --test-email you@example.com
        test:     makes a draft in Mailchimp and emails a test copy. Nothing goes to the list.
        schedule: makes the campaign, schedules it for the coming Wednesday at
                  1:00 PM New York time, and also emails a test copy.

The audience, "from" name and reply-to address are copied from the most recent
Minhag of the Week email that was sent, so this always goes to the same people.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_email  # noqa: E402

ROOT = build_email.ROOT
NY = ZoneInfo("America/New_York")
LOG_PATH = os.path.join(ROOT, "logs", "weekly-email.log")
TITLE_PREFIX = "Minhag Auto"  # internal campaign name in Mailchimp, never shown to readers
SEND_WEEKDAY, SEND_HOUR = 2, 13  # Wednesday, 1:00 PM New York time

_log_lines = []


def log(msg):
    print(msg, flush=True)
    _log_lines.append(msg)


def write_log():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    stamp = datetime.now(NY).strftime("%Y-%m-%d %I:%M %p ET")
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(f"Last run: {stamp}\n" + "\n".join(_log_lines) + "\n")


def mc(method, path, body=None):
    key = os.environ.get("MAILCHIMP_API_KEY", "").strip()
    if "-" not in key:
        raise SystemExit("MAILCHIMP_API_KEY is missing or is not a Mailchimp key.")
    dc = key.rsplit("-", 1)[1]
    req = urllib.request.Request(
        f"https://{dc}.api.mailchimp.com/3.0{path}",
        data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"x:{key}".encode()).decode())
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:1500]
        raise SystemExit(f"Mailchimp said no ({err.code}) to {method} {path.split('?')[0]}: {detail}")


def last_minhag_campaign():
    """The most recent sent Minhag of the Week email, used as the model for
    who receives this one and who it is from."""
    fields = ("campaigns.id,campaigns.send_time,campaigns.long_archive_url,campaigns.archive_url,"
              "campaigns.settings,campaigns.recipients")
    data = mc("GET", f"/campaigns?status=sent&sort_field=send_time&sort_dir=DESC&count=60&fields={fields}")
    for c in data.get("campaigns", []):
        s = c.get("settings", {})
        hay = " ".join([c.get("long_archive_url") or "", s.get("title") or "", s.get("subject_line") or ""]).lower()
        if "minhag" in hay and not (s.get("title") or "").startswith(TITLE_PREFIX):
            return c
    for c in data.get("campaigns", []):
        if "minhag" in json.dumps(c).lower():
            return c
    raise SystemExit("Could not find a past Minhag of the Week email in Mailchimp to copy the audience from.")


def recipients_from(model):
    r = model.get("recipients", {})
    out = {"list_id": r["list_id"]}
    so = r.get("segment_opts") or {}
    if so.get("saved_segment_id"):
        out["segment_opts"] = {"saved_segment_id": so["saved_segment_id"]}
    elif so.get("conditions"):
        out["segment_opts"] = {"match": so.get("match", "any"), "conditions": so["conditions"]}
    return out


def next_send_time(now=None):
    now = now or datetime.now(NY)
    days = (SEND_WEEKDAY - now.weekday()) % 7
    when = (now + timedelta(days=days)).replace(hour=SEND_HOUR, minute=0, second=0, microsecond=0)
    if when <= now + timedelta(minutes=30):
        when += timedelta(days=7)
    return when


def wait_until_live(urls, minutes=8):
    """Email apps load images from the website, so they must be published first."""
    deadline = time.time() + minutes * 60
    pending = list(urls)
    while pending and time.time() < deadline:
        still = []
        for u in pending:
            try:
                req = urllib.request.Request(f"{u}?cb={int(time.time())}", method="GET",
                                             headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    if r.status != 200 or int(r.headers.get("Content-Length") or 1) == 0:
                        still.append(u)
            except Exception:
                still.append(u)
        pending = still
        if pending:
            time.sleep(15)
    if pending:
        raise SystemExit("These files are not live on the website yet, so nothing was sent: " + ", ".join(pending))


def cmd_prepare(a):
    build_email.make_images(a.episode)
    try:
        build_email.make_pdf(a.episode)
        log(f"PDF ready: {build_email.pdf_rel_path(a.episode)}")
    except Exception as err:
        log(f"PDF could not be made, so the email will go without the PDF button: {err}")


def cmd_send(a):
    ep_id = a.episode
    send_at = next_send_time()
    has_pdf = os.path.exists(os.path.join(ROOT, build_email.pdf_rel_path(ep_id)))
    email = build_email.build(ep_id, send_date=send_at.date(), has_pdf=has_pdf)
    log(f"Episode {ep_id}: {email['title']} ({email['label']})")
    log(f"Subject: {email['subject']}")

    site = build_email.SITE
    assets = [f"{site}/images/email/logo.png", f"{site}/images/email/episode-{ep_id}-play.jpg"]
    if has_pdf:
        assets.append(f"{site}/{build_email.pdf_rel_path(ep_id)}")
    wait_until_live(assets)
    log("Images and PDF are live on the website.")

    model = last_minhag_campaign()
    ms = model.get("settings", {})
    recipients = recipients_from(model)
    log(f"Copying audience and sender from the last Minhag email: \"{ms.get('subject_line')}\" "
        f"sent {model.get('send_time')}")
    log(f"From: {ms.get('from_name')} <{ms.get('reply_to')}> | Audience: {model['recipients'].get('list_name')} "
        f"| Segment: {model['recipients'].get('segment_text') or 'entire audience'} "
        f"| Recipients last time: {model['recipients'].get('recipient_count')}")

    title = f"{TITLE_PREFIX}: Episode {ep_id} ({a.mode})"
    # Clear out earlier unsent drafts this tool made for the same episode.
    drafts = mc("GET", "/campaigns?status=save&count=100&fields=campaigns.id,campaigns.settings.title")
    for c in drafts.get("campaigns", []):
        if (c.get("settings", {}).get("title") or "").startswith(f"{TITLE_PREFIX}: Episode {ep_id} "):
            mc("DELETE", f"/campaigns/{c['id']}")
            log(f"Removed an older draft ({c['id']}).")

    camp = mc("POST", "/campaigns", {
        "type": "regular",
        "recipients": recipients,
        "settings": {
            "subject_line": email["subject"], "preview_text": email["preheader"], "title": title,
            "from_name": ms.get("from_name"), "reply_to": ms.get("reply_to"),
            "to_name": ms.get("to_name") or "",
        },
    })
    cid = camp["id"]
    mc("PUT", f"/campaigns/{cid}/content", {"html": email["html"]})
    log(f"Campaign created in Mailchimp: {title} (id {cid})")

    check = mc("GET", f"/campaigns/{cid}/send-checklist")
    problems = [i.get("heading", "") + ": " + i.get("details", "")
                for i in check.get("items", []) if i.get("type") == "error"]
    if problems:
        log("Mailchimp checklist problems: " + " | ".join(problems))

    if a.test_email:
        mc("POST", f"/campaigns/{cid}/actions/test",
           {"test_emails": [e.strip() for e in a.test_email.split(",") if e.strip()], "send_type": "html"})
        log(f"Test copy sent to {a.test_email}.")

    if a.mode == "schedule":
        if problems:
            raise SystemExit("Not scheduled because Mailchimp's checklist has problems (see above).")
        when_utc = send_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        mc("POST", f"/campaigns/{cid}/actions/schedule", {"schedule_time": when_utc})
        log(f"SCHEDULED for {send_at.strftime('%A, %b %d, %Y at %I:%M %p')} New York time.")
    else:
        log("Test only: nothing was sent to the list. The draft is saved in Mailchimp, unscheduled.")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("episode", type=int)
    s = sub.add_parser("send")
    s.add_argument("episode", type=int)
    s.add_argument("--mode", choices=["test", "schedule"], default="test")
    s.add_argument("--test-email", default="")
    a = ap.parse_args()
    try:
        (cmd_prepare if a.cmd == "prepare" else cmd_send)(a)
    except SystemExit as err:
        if err.code not in (0, None):
            log(f"FAILED: {err.code}")
        raise
    except Exception as err:
        log(f"FAILED: {type(err).__name__}: {err}")
        raise
    finally:
        if a.cmd == "send":
            write_log()


if __name__ == "__main__":
    main()
