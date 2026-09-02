#!/usr/bin/env python3
"""
Publish a new Minhag of the Week episode end-to-end.

Triggered by a GitHub repository_dispatch event (sent from the Google Sheet's
Apps Script) with a payload of:
  {
    "episode_num": "303",
    "topic": "Aleppo Customs of the month of Elul",
    "presenter": "Mosseri",
    "dedication": "Dedicated in loving memory of Ruth and Ralph S. Gindi A\"H",
    "release_date": "2026-08-26"   # the Wednesday this episode is slated for
  }

Reads that payload from the GITHUB_EVENT_PATH JSON file (standard for
repository_dispatch events), does all the work, commits, and pushes.

Secrets needed (as repo Action secrets):
  ANTHROPIC_API_KEY
  SPROUTVIDEO_API_KEY
GITHUB_TOKEN is provided automatically by Actions with contents:write.
"""

import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

REPO_ROOT = os.environ.get("GITHUB_WORKSPACE", ".")
INDEX_HTML = os.path.join(REPO_ROOT, "index.html")
VERSION_JSON = os.path.join(REPO_ROOT, "version.json")
DEBUG_LOG = os.path.join(REPO_ROOT, "logs", "publish-debug.log")

# Read lazily (inside main(), guarded) rather than at import time, so a
# missing/misnamed secret produces a diagnosed, committed failure instead of
# a bare KeyError that only exists in GitHub's log storage.
SPROUT_KEY = os.environ.get("SPROUTVIDEO_API_KEY")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

# The category taxonomy actually used across index.html's EPS tag arrays.
# Kept here so the categorizer prompt stays anchored to real values.
KNOWN_CATEGORIES = {
    "holidays": ["Rosh Hashanah", "Yom Kippur", "Sukkot & Shemini Asseret & Simhat Torah",
                 "Hanukkah", "Tu Bishbat", "Purim", "Pesah", "Omer & Lag LaOmer",
                 "Shabuot", "17 Tamuz through Tish'a BeAb", "Elul", "Rosh Hodesh",
                 "Shobabim", "Fasts", "13 Sivan"],
    "prayer": ["Shahrit", "Minha & Arbit", "Amidah", "Qaddish", "Birkat Kohanim",
               "Sefer Torah & Torah Reading", "Talet & Tefillin",
               "Tefilla BeSibbur Series"],
    "shabbat": ["Candle Lighting", "Qabbalat Shabbat", "Qiddush", "Habdalah",
                "Shabbat Table & Food", "Misc"],
    "lifecycle": ["Berit Milah", "Naming a Baby", "Bar Missvah", "Pidyon HaBen",
                  "Engagements & Weddings", "Mourning"],
    "kashrut": [],
    "sebbit": [],
    "parasha": [],
}


def http_json(url, headers=None, data=None, method="GET"):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def find_sprout_video(episode_num):
    """Page through SproutVideo videos looking for a title containing the episode number."""
    url = "https://api.sproutvideo.com/v1/videos?per_page=100"
    seen = set()
    while url and url not in seen:
        seen.add(url)
        data = http_json(url, headers={"SproutVideo-Api-Key": SPROUT_KEY})
        for v in data.get("videos", []):
            title = v["title"]
            if re.search(rf"\b{re.escape(str(episode_num))}\b", title):
                return v
        url = data.get("next_page")
    return None


def make_thumbnail(video_id, security_token, video_480_url, duration):
    """Download the video, extract a real mid-video frame, upload as custom poster frame."""
    local_mp4 = "/tmp/_ep_video.mp4"
    local_jpg = "/tmp/_ep_frame.jpg"

    req = urllib.request.Request(video_480_url, headers={"SproutVideo-Api-Key": SPROUT_KEY})
    with urllib.request.urlopen(req) as resp, open(local_mp4, "wb") as f:
        f.write(resp.read())

    midpoint = max(1, int(duration / 2))
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(midpoint), "-i", local_mp4,
         "-frames:v", "1", "-q:v", "2", local_jpg],
        check=True, capture_output=True,
    )

    # Upload as custom poster frame (multipart/form-data)
    boundary = "----minhagpublish"
    with open(local_jpg, "rb") as f:
        img_bytes = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="custom_poster_frame"; filename="frame.jpg"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + img_bytes + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"https://api.sproutvideo.com/v1/videos/{video_id}",
        data=body, method="PUT",
        headers={
            "SproutVideo-Api-Key": SPROUT_KEY,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())

    poster_frames = result["assets"]["poster_frames"]
    # The just-uploaded custom frame is the last one in the list.
    return poster_frames[-1]


def categorize(topic, presenter, dedication):
    """Ask Claude to pick tags from the known taxonomy based on the topic text."""
    taxonomy_desc = "\n".join(
        f"- {cat}: {', '.join(vals) if vals else '(no sub-values, just tag presence)'}"
        for cat, vals in KNOWN_CATEGORIES.items()
    )
    prompt = f"""You are tagging an episode of a Sephardic Jewish customs video series for a website's category filter.

Episode topic: "{topic}"
Presenter: {presenter}

Existing category taxonomy (category: possible sub-values):
{taxonomy_desc}

Pick 0-3 categories that genuinely apply to this topic, and for each category that has sub-values, pick the matching sub-value(s) if any apply (use "|" to join multiple sub-values in one category, use "✓" if the category applies but has no natural sub-value match).

Respond with ONLY valid JSON, no other text, in this exact shape:
{{"categories": ["holidays", "prayer"], "subs": "holidays:Elul,prayer:Shahrit"}}

If a category has no sub-value match, omit it from "subs" entirely (don't force a "✓" unless truly generic).
If nothing applies well, respond {{"categories": [], "subs": ""}}."""

    resp = http_json(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        data={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        },
        method="POST",
    )
    text = resp["content"][0]["text"].strip()
    # Strip markdown fences if the model added them anyway
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    parsed = json.loads(text)
    return parsed["categories"], parsed["subs"]


def slugify_title(topic):
    """Match the site's existing preview-folder naming: spaces -> dashes, strip unsafe chars.
    NOTE: mirrored in the Google Apps Script (slugifyTitle) so the sheet can write
    the direct-access URL back immediately, without waiting for this script to run."""
    cleaned = re.sub(r"[^\w\s-]", "", topic)
    return re.sub(r"\s+", "-", cleaned.strip())


def make_preview_page(episode_num, topic, presenter, thumb_url):
    """Static per-episode folder with og:/twitter: tags + redirect, for link previews (iMessage/WhatsApp/etc)."""
    slug = slugify_title(topic)
    title = f'SCA Minhag of the Week {episode_num}: &ldquo;{topic}&rdquo;'
    desc = f"Presented by {presenter}. Watch this week&rsquo;s minhag from the Sephardic Community Alliance."
    url = f"https://minhagoftheweek.com/{slug}"
    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="video.other">
<meta property="og:site_name" content="SCA Minhag of the Week">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{thumb_url}">
<meta property="og:image:width" content="1920">
<meta property="og:image:height" content="1080">
<meta property="og:image:type" content="image/jpeg">
<meta property="og:url" content="{url}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{desc}">
<meta name="twitter:image" content="{thumb_url}">
<meta http-equiv="refresh" content="0; url=/#ep-{episode_num}">
<script>location.replace('/#ep-{episode_num}');</script>
</head><body>
<p>Redirecting to <a href="/#ep-{episode_num}">Episode {episode_num}</a>&hellip;</p>
</body></html>
"""
    folder = os.path.join(REPO_ROOT, slug)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    return slug


def next_wednesday_1230_et(after_date=None):
    """Compute the next Wednesday at 12:30pm America/New_York, as ISO 8601 with offset."""
    tz = ZoneInfo("America/New_York")
    now = datetime.now(tz) if after_date is None else after_date
    days_ahead = (2 - now.weekday()) % 7  # Wednesday = 2
    target = now + timedelta(days=days_ahead)
    target = target.replace(hour=12, minute=30, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=7)
    return target.isoformat()


def write_debug_log(message):
    """Write a failure report straight into the repo and push it, so the
    next failure is diagnosable via a normal git pull — no GitHub Actions
    log access (which requires blob-storage domains that may be blocked)
    needed. This runs in its own git add/commit/push scope, separate from
    any partial site changes, so a failed publish never leaves index.html
    half-edited in a pushed commit."""
    os.makedirs(os.path.dirname(DEBUG_LOG), exist_ok=True)
    timestamp = datetime.now(ZoneInfo("America/New_York")).isoformat()
    with open(DEBUG_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*70}\n[{timestamp}]\n{message}\n")
    try:
        # Discard any partial edits to tracked files (e.g. a half-written
        # index.html from a failure partway through main()) before adding
        # only the debug log — a failed run must never push a broken site.
        subprocess.run(
            ["git", "checkout", "--", ".", ":!logs/publish-debug.log"],
            cwd=REPO_ROOT,
        )
        subprocess.run(["git", "clean", "-fd", "--exclude=logs"], cwd=REPO_ROOT)
        subprocess.run(["git", "config", "user.name", "minhag-publish-bot"], check=True, cwd=REPO_ROOT)
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True, cwd=REPO_ROOT)
        subprocess.run(["git", "add", "logs/publish-debug.log"], check=True, cwd=REPO_ROOT)
        subprocess.run(["git", "commit", "-m", f"Publish failure log ({timestamp})"], check=True, cwd=REPO_ROOT)
        subprocess.run(["git", "push"], check=True, cwd=REPO_ROOT)
    except Exception as log_push_error:
        # If even the log push fails, at least this shows up in the Actions
        # console output itself.
        print(f"Also failed to push debug log: {log_push_error}", file=sys.stderr)


def main():
    missing_secrets = [
        name for name, val in [
            ("SPROUTVIDEO_API_KEY", SPROUT_KEY),
            ("ANTHROPIC_API_KEY", ANTHROPIC_KEY),
        ] if not val
    ]
    if missing_secrets:
        raise RuntimeError(
            "Missing required repo secret(s): " + ", ".join(missing_secrets) +
            ". Set these under Settings > Secrets and variables > Actions."
        )

    event_path = os.environ["GITHUB_EVENT_PATH"]
    with open(event_path) as f:
        event = json.load(f)
    payload = event["client_payload"]

    episode_num = str(payload["episode_num"])
    topic = payload["topic"]
    presenter = payload["presenter"]
    dedication = payload["dedication"]

    print(f"Publishing episode {episode_num}: {topic}")

    video = find_sprout_video(episode_num)
    if not video:
        raise RuntimeError(f"No SproutVideo upload found matching episode {episode_num}")

    embed_url = f"https://videos.sproutvideo.com/embed/{video['id']}/{video['security_token']}"
    thumb_url = make_thumbnail(
        video["id"], video["security_token"],
        video["assets"]["videos"]["480p"], video["duration"],
    )

    categories, subs = categorize(topic, presenter, dedication)
    cats_js = json.dumps(categories)

    slug = make_preview_page(episode_num, topic, presenter, thumb_url)

    release_dt = datetime.now(ZoneInfo("America/New_York"))
    display_date = release_dt.strftime("%b %-d, %Y")
    schedule_iso = next_wednesday_1230_et()

    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    dedication_escaped = dedication.replace('"', '\\"')
    entry = (
        f'[{episode_num}, "Ep. {episode_num}", "{topic}", "{presenter}", '
        f'"{dedication_escaped}", "{display_date}", {cats_js}, "{subs}", '
        f'"SPROUT:{embed_url}"]'
    )
    html = html.replace("const EPS=[[", f"const EPS=[{entry}, [", 1)
    html = html.replace('const THUMBS={"', f'const THUMBS={{"{episode_num}": "{thumb_url}", "', 1)

    schedule_entry = f"  {episode_num}: '{schedule_iso}',\n"
    html = re.sub(
        r"(const SCHEDULE=\{\n)",
        r"\1" + schedule_entry,
        html,
        count=1,
    )

    build_version = str(int(time.time()))
    html = re.sub(
        r'<meta name="build-version" content="\d+">',
        f'<meta name="build-version" content="{build_version}">',
        html,
    )

    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    with open(VERSION_JSON, "w", encoding="utf-8") as f:
        f.write(json.dumps({"v": build_version}))

    subprocess.run(["git", "config", "user.name", "minhag-publish-bot"], check=True, cwd=REPO_ROOT)
    subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True, cwd=REPO_ROOT)
    subprocess.run(["git", "add", "index.html", "version.json", slug], check=True, cwd=REPO_ROOT)
    subprocess.run(
        ["git", "commit", "-m", f"Publish Episode {episode_num}: {topic} (scheduled {schedule_iso})"],
        check=True, cwd=REPO_ROOT,
    )
    subprocess.run(["git", "push"], check=True, cwd=REPO_ROOT)

    print(f"Done. Episode {episode_num} scheduled to go live {schedule_iso}")
    print(f"  Categories: {categories} ({subs})")
    print(f"  Thumbnail: {thumb_url}")
    print(f"  Preview page: /{slug}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        write_debug_log(
            "Publish failed with an unhandled exception:\n\n" + tb
        )
        # Still exit non-zero so the Actions run shows failed, as before —
        # the difference is the reason is now committed to the repo, not
        # only visible in GitHub's log storage.
        print(tb, file=sys.stderr)
        sys.exit(1)
