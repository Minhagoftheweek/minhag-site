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

    python scripts/send_weekly_email.py weekly --test-email you@example.com
        The hands-off mode, run on a timer. Reads the episode tracking sheet.
        Any upcoming row with "Yes" in the "Ready for mailchimp?" column gets
        its email scheduled for that row's date at 1:00 PM New York time, with
        a test copy sent. "23 (Old)" in the Episode # column means From the
        Archives; a plain number means a new episode. Changing the episode
        swaps the email; removing the Yes takes it off the schedule.
        Then runs the same check as "check" below.

    python scripts/send_weekly_email.py plan
        Quick look (no Mailchimp, nothing installed) at whether "weekly" has
        anything to do. Prints heavy=true or heavy=false for the workflow.

    python scripts/send_weekly_email.py check --test-email you@example.com
        Run on Wednesday before the send. If the episode's transcript or details
        changed on the website since the email was scheduled, the scheduled
        email (and its PDF) is rebuilt with the latest version. If nothing
        changed, or anything goes wrong, the email already scheduled stays put.

The audience, "from" name and reply-to address are copied from the most recent
Minhag of the Week email that was sent, so this always goes to the same people.
"""
import argparse
import argparse as _argparse
import base64
import csv
import io
import hashlib
import re
import subprocess
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
STATE_PATH = os.path.join(ROOT, "logs", "weekly-email-state.json")
TITLE_PREFIX = "Minhag Auto"  # internal campaign name in Mailchimp, never shown to readers
SEND_WEEKDAY, SEND_HOUR = 2, 13  # Wednesday, 1:00 PM New York time

# The episode tracking sheet. Each year has its own tab, named for the year.
SHEET_ID = "1Zrh6pF5CA2KQTupnb3QLNKjuVD1JcwE3aFoqls8D9iQ"
SHEET_TAB_GIDS = {2026: 916376778}

_log_lines = []


def log(msg):
    print(msg, flush=True)
    _log_lines.append(msg)


def write_log():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    stamp = datetime.now(NY).strftime("%Y-%m-%d %I:%M %p ET")
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(f"Last run: {stamp}\n" + "\n".join(_log_lines) + "\n")


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def fingerprints(ep_id, html_text):
    """Short codes that change whenever the email body or the transcript changes."""
    with open(os.path.join(ROOT, "transcripts.json"), encoding="utf-8") as f:
        tx = (json.load(f).get(str(ep_id)) or {}).get("text") or ""
    h = lambda t: hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]
    return h(html_text), h(tx)


def publish_assets(message):
    """Commits the email images and PDF and pushes them to the site."""
    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)
    git("add", "images/email", "transcripts/pdf")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git("commit", "-m", message)
    for _ in range(5):
        if git("pull", "--rebase", check=False).returncode == 0 and git("push", check=False).returncode == 0:
            return True
        time.sleep(5)
    raise SystemExit("Could not publish the updated PDF to the site.")


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
    send_at = getattr(a, "send_at", None) or next_send_time()
    is_new = getattr(a, "is_new", None)
    has_pdf = os.path.exists(os.path.join(ROOT, build_email.pdf_rel_path(ep_id)))
    email = build_email.build(ep_id, send_date=send_at.date(), has_pdf=has_pdf, is_new=is_new)
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

    if a.mode == "schedule":
        sched = mc("GET", "/campaigns?status=schedule&count=100&fields=campaigns.id,campaigns.settings.title")
        for c in sched.get("campaigns", []):
            if (c.get("settings", {}).get("title") or "") == f"{TITLE_PREFIX}: Episode {ep_id} (schedule)":
                mc("POST", f"/campaigns/{c['id']}/actions/unschedule")
                mc("DELETE", f"/campaigns/{c['id']}")
                log(f"Replaced the email already scheduled for this episode ({c['id']}).")

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
        fp_html, fp_tx = fingerprints(ep_id, email["html"])
        state = load_state()
        state[cid] = {"episode": ep_id, "send_time": when_utc, "fp_html": fp_html, "fp_tx": fp_tx,
                      "is_new": is_new}
        save_state(state)
    else:
        log("Test only: nothing was sent to the list. The draft is saved in Mailchimp, unscheduled.")


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def sheet_rows(year):
    """Rows of the tracking sheet's tab for one year, as dicts with
    date, episode (Episode # text), topic and ready (True when the
    "Ready for mailchimp?" column says yes)."""
    gid = SHEET_TAB_GIDS.get(year)
    base = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    if gid is not None:
        text, cols = _fetch(f"{base}/export?format=csv&gid={gid}"), (0, 1, 3, 5)
    else:
        # A tab we have no id for yet: read it by name. This route can drop text
        # like "23 (Old)" from the number column, so the topic is matched instead.
        text, cols = _fetch(f"{base}/gviz/tq?tqx=out:csv&sheet={year}&tq=select%20A,B,D,F"), (0, 1, 2, 3)
    if "<html" in text[:300].lower():
        raise SystemExit("Could not read the tracking sheet (it may no longer be shared by link).")
    rows = []
    for row in csv.reader(io.StringIO(text)):
        get = lambda i: row[i].strip() if len(row) > i else ""
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", get(cols[0]))
        if not m:
            continue
        try:
            day = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date()
        except ValueError:
            continue
        rows.append({"date": day, "episode": get(cols[1]), "topic": get(cols[2]),
                     "ready": get(cols[3]).lower() in ("yes", "y")})
    return rows


def ready_rows(now):
    """Upcoming rows marked ready, each with the moment its email should send."""
    today = now.date()
    rows = sheet_rows(today.year)
    if len(rows) < 5:
        raise SystemExit("The tracking sheet came back nearly empty, so nothing was changed.")
    if today.month == 12:
        try:
            rows += sheet_rows(today.year + 1)
        except BaseException:
            pass
    out = []
    for r in rows:
        if not r["ready"] or not (0 <= (r["date"] - today).days <= 60):
            continue
        send_at = datetime(r["date"].year, r["date"].month, r["date"].day, SEND_HOUR, 0, tzinfo=NY)
        if send_at > now + timedelta(minutes=20):
            out.append(dict(r, send_at=send_at, is_new="old" not in r["episode"].lower()))
    return out


def _state_schedule(state, now):
    """What our own records say is scheduled: {date: episode} for future sends."""
    have = {}
    for v in state.values():
        try:
            when = datetime.fromisoformat(v["send_time"].replace("Z", "+00:00")).astimezone(NY)
        except Exception:
            continue
        if when > now:
            have[when.date()] = v.get("episode")
    return have


def cmd_plan(a):
    """Prints heavy=true when the sheet and our records disagree."""
    heavy = False
    try:
        now = datetime.now(NY)
        eps = build_email.load_site()[0]
        want = {}
        for r in ready_rows(now):
            ep, problem = resolve_episode(r, eps)
            if ep:
                want[r["date"]] = ep[0]
            else:
                print(f"Row {r['date']}: marked ready but {problem}.", file=sys.stderr)
        have = _state_schedule(load_state(), now)
        heavy = want != have
        print(f"Sheet wants {want}; scheduled {have}.", file=sys.stderr)
    except BaseException as err:
        print(f"Quick look failed, will try again next time: {err}", file=sys.stderr)
    print(f"heavy={'true' if heavy else 'false'}")


def _words(t):
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", (t or "").lower()).split() if len(w) >= 4}


def resolve_episode(row, eps):
    """Turns a sheet row into a website episode. Returns (episode, problem)."""
    num = re.search(r"\d+(?:\.\d+)?", row["episode"])
    ep = None
    if num:
        n = float(num.group(0))
        ep = next((e for e in eps if float(re.sub(r"^Ep\.\s*", "", e[1])) == n), None)
        if not ep:
            return None, f"episode {num.group(0)} is not on the website yet"
        # Guard against a typo in the number: the sheet's topic should resemble the site's title.
        if row["topic"] and not (_words(row["topic"]) & _words(ep[2])):
            return None, (f"the sheet says episode {num.group(0)} is \"{row['topic']}\" but on the website "
                          f"episode {num.group(0)} is \"{ep[2]}\"")
    elif row["topic"]:
        norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
        ep = next((e for e in eps if norm(e[2]) == norm(row["topic"])), None)
        if not ep:
            return None, f"could not match \"{row['topic']}\" to an episode on the website"
    else:
        return None, "no episode is entered"
    if not isinstance(ep[0], int):
        return None, f"\"{ep[2]}\" has no whole episode number, so it needs to be scheduled by hand"
    if not os.path.exists(os.path.join(ROOT, "images", f"episode-{ep[0]}-thumb.jpg")):
        return None, f"episode {ep[0]} has no thumbnail on the website yet"
    return ep, None


def cmd_auto(a):
    """Makes Mailchimp's schedule match the tracking sheet's "Ready" rows."""
    now = datetime.now(NY)
    eps = build_email.load_site()[0]
    rows = ready_rows(now)
    today_row_problem = None

    fields = "campaigns.id,campaigns.send_time,campaigns.settings.title"
    sched = {}
    for c in mc("GET", f"/campaigns?status=schedule&count=100&fields={fields}").get("campaigns", []):
        title = c.get("settings", {}).get("title") or ""
        m = re.search(r"Episode (\d+)", title)
        if title.startswith(TITLE_PREFIX) and m:
            when = datetime.fromisoformat(c["send_time"].replace("Z", "+00:00")).astimezone(NY)
            sched[when.date()] = (c["id"], int(m.group(1)), c["send_time"])

    state = load_state()
    keep_dates = set()
    for r in rows:
        day, label = r["date"], r["send_at"].strftime("%A, %b %d")
        keep_dates.add(day)
        ep, problem = resolve_episode(r, eps)
        if not ep:
            log(f"Auto: {label} is marked ready but {problem}. Left as is.")
            if day == now.date():
                today_row_problem = f"The {label} email is NOT scheduled: {problem}."
            continue
        ep_id = ep[0]
        if day in sched and sched[day][1] == ep_id:
            cid = sched[day][0]
            if cid not in state:
                state[cid] = {"episode": ep_id, "send_time": sched[day][2], "is_new": r["is_new"]}
                save_state(state)
            log(f"Auto: episode {ep_id} is already scheduled for {label}.")
            continue
        if day in sched:
            mc("POST", f"/campaigns/{sched[day][0]}/actions/unschedule")
            mc("DELETE", f"/campaigns/{sched[day][0]}")
            state.pop(sched[day][0], None)
            save_state(state)
            log(f"Auto: the sheet now says episode {ep_id} for {label}, so episode {sched[day][1]}'s email was removed.")
        kind = "new episode" if r["is_new"] else "from the archives"
        log(f"Auto: the sheet says episode {ep_id} (\"{ep[2]}\", {kind}) is ready for {label}. Scheduling it.")
        cmd_prepare(_argparse.Namespace(episode=ep_id))
        publish_assets(f"Weekly email assets for episode {ep_id}")
        cmd_send(_argparse.Namespace(episode=ep_id, mode="schedule", test_email=a.test_email,
                                     send_at=r["send_at"], is_new=r["is_new"]))
        state = load_state()

    # A scheduled email whose row no longer says Yes comes off the schedule (kept as a draft).
    for day, (cid, ep_id, _) in sched.items():
        if day in keep_dates or day < now.date() or (day - now.date()).days > 60:
            continue
        mc("POST", f"/campaigns/{cid}/actions/unschedule")
        state.pop(cid, None)
        log(f"Auto: {day.strftime('%A, %b %d')} is no longer marked ready, so episode {ep_id}'s email was "
            f"taken off the schedule. It is saved in Mailchimp as a draft.")

    # Forget sends that are in the past.
    for cid in [k for k, v in state.items()
                if datetime.fromisoformat(v["send_time"].replace("Z", "+00:00")) < datetime.now(timezone.utc) - timedelta(days=1)]:
        state.pop(cid)
    save_state(state)

    if today_row_problem:
        raise SystemExit(today_row_problem)


def cmd_weekly(a):
    failure = None
    try:
        cmd_auto(a)
    except SystemExit as err:
        failure = err
        if err.code not in (0, None):
            log(f"FAILED: {err.code}")
    except Exception as err:
        failure = SystemExit(f"{type(err).__name__}: {err}")
        log(f"FAILED: {failure.code}")
    cmd_check(a)
    if failure and failure.code not in (0, None):
        raise SystemExit(1)


def cmd_check(a):
    """Wednesday pre-send check: refresh the scheduled email if the site changed."""
    now = datetime.now(timezone.utc)
    fields = "campaigns.id,campaigns.send_time,campaigns.settings.title,campaigns.settings.subject_line"
    sched = mc("GET", f"/campaigns?status=schedule&count=100&fields={fields}")
    mine = [c for c in sched.get("campaigns", [])
            if (c.get("settings", {}).get("title") or "").startswith(TITLE_PREFIX)]
    if not mine:
        log("Check: no automated Minhag email is scheduled. Nothing to do.")
        return
    state = load_state()
    for c in mine:
        cid = c["id"]
        m = re.search(r"Episode (\d+)", c["settings"]["title"])
        send_time = datetime.fromisoformat(c["send_time"].replace("Z", "+00:00"))
        mins = (send_time - now).total_seconds() / 60
        if not m:
            continue
        ep_id = int(m.group(1))
        if mins > 5 * 60:
            log(f"Check: episode {ep_id} sends in {mins / 60:.0f} hours. Too early to check; skipping.")
            continue
        if mins < 20:
            log(f"Check: episode {ep_id} sends in {mins:.0f} minutes. Too close to change safely; leaving it.")
            continue

        send_date = send_time.astimezone(NY).date()
        has_pdf = os.path.exists(os.path.join(ROOT, build_email.pdf_rel_path(ep_id)))
        old = state.get(cid, {})
        is_new = old.get("is_new")
        email = build_email.build(ep_id, send_date=send_date, has_pdf=has_pdf, is_new=is_new)
        fp_html, fp_tx = fingerprints(ep_id, email["html"])
        tx_changed = old.get("fp_tx") != fp_tx
        html_changed = old.get("fp_html") != fp_html or c["settings"].get("subject_line") != email["subject"]
        if not tx_changed and not html_changed:
            log(f"Check: episode {ep_id} is unchanged since it was scheduled. Leaving it as is.")
            continue

        if tx_changed:
            try:
                build_email.make_images(ep_id)
                build_email.make_pdf(ep_id)
                publish_assets(f"Weekly email: refreshed PDF for episode {ep_id}")
                log(f"Check: transcript changed, so the PDF for episode {ep_id} was rebuilt.")
                if not has_pdf:
                    email = build_email.build(ep_id, send_date=send_date, has_pdf=True, is_new=is_new)
                    fp_html, _ = fingerprints(ep_id, email["html"])
                    html_changed = True
            except SystemExit:
                raise
            except Exception as err:
                log(f"Check: the PDF could not be rebuilt ({err}). The older PDF stays.")

        if html_changed:
            when = send_time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            mc("POST", f"/campaigns/{cid}/actions/unschedule")
            try:
                mc("PUT", f"/campaigns/{cid}/content", {"html": email["html"]})
                mc("PATCH", f"/campaigns/{cid}",
                   {"settings": {"subject_line": email["subject"], "preview_text": email["preheader"]}})
                log(f"Check: episode {ep_id} changed on the website, so the scheduled email was updated.")
            finally:
                # Whatever happened above, put it back on the schedule so it still sends.
                mc("POST", f"/campaigns/{cid}/actions/schedule", {"schedule_time": when})
                log(f"Check: episode {ep_id} is back on the schedule for "
                    f"{send_time.astimezone(NY).strftime('%A %I:%M %p')} New York time.")
            if a.test_email:
                try:
                    mc("POST", f"/campaigns/{cid}/actions/test",
                       {"test_emails": [e.strip() for e in a.test_email.split(",") if e.strip()],
                        "send_type": "html"})
                    log(f"Check: updated test copy sent to {a.test_email}.")
                except SystemExit as err:
                    log(f"Check: could not send the updated test copy ({err.code}). The email is still scheduled.")
        state[cid] = {"episode": ep_id, "send_time": c["send_time"], "fp_html": fp_html, "fp_tx": fp_tx,
                      "is_new": is_new}
        save_state(state)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("episode", type=int)
    s = sub.add_parser("send")
    s.add_argument("episode", type=int)
    s.add_argument("--mode", choices=["test", "schedule"], default="test")
    s.add_argument("--test-email", default="")
    k = sub.add_parser("check")
    k.add_argument("--test-email", default="")
    w = sub.add_parser("weekly")
    w.add_argument("--test-email", default="")
    sub.add_parser("plan")
    a = ap.parse_args()
    try:
        {"prepare": cmd_prepare, "send": cmd_send, "check": cmd_check, "weekly": cmd_weekly,
         "plan": cmd_plan}[a.cmd](a)
    except SystemExit as err:
        if err.code not in (0, None):
            log(f"FAILED: {err.code}")
        raise
    except Exception as err:
        log(f"FAILED: {type(err).__name__}: {err}")
        raise
    finally:
        if a.cmd in ("send", "check", "weekly"):
            write_log()


if __name__ == "__main__":
    main()
