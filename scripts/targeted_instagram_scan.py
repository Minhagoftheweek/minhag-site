#!/usr/bin/env python3
"""
One-off targeted scan: finds every #SCAMinhagOfTheWeek video/reel posted on
@SCA_updates within an explicit date range, and writes the results to
data/instagram-targeted-scan.csv. Does NOT touch data/view-count.json or
data/instagram-cache.json — completely separate from the automated total-
views system, safe to run any time without affecting it.

Required environment variables:
  INSTAGRAM_ACCESS_TOKEN
  INSTAGRAM_BUSINESS_ACCOUNT_ID
  SCAN_SINCE_DATE   (YYYY-MM-DD, inclusive)
  SCAN_UNTIL_DATE   (YYYY-MM-DD, exclusive)

Always writes data/instagram-targeted-scan-log.txt with full run output,
including any traceback, so results are inspectable via the GitHub Contents
API even when Actions log storage isn't reachable.
"""
import csv
import json
import os
import sys
import traceback
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

HASHTAG = "#scaminhagoftheweek"

LOG_LINES = []


def log(msg):
    print(msg)
    LOG_LINES.append(str(msg))


def http_get_json(url):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_media_view_count(media_id, access_token):
    insights_url = (f"https://graph.instagram.com/{media_id}/insights"
                     f"?metric=views"
                     f"&access_token={urllib.parse.quote(access_token)}")
    try:
        idata = http_get_json(insights_url)
        values = idata.get("data", [])
        if values:
            return int(values[0].get("values", [{}])[0].get("value", 0))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        log(f"  view count fetch failed for {media_id}: {e}")
    return None


def run():
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    account_id = os.environ.get("INSTAGRAM_BUSINESS_ACCOUNT_ID")
    since_date = os.environ.get("SCAN_SINCE_DATE")
    until_date = os.environ.get("SCAN_UNTIL_DATE")
    lookup_url = os.environ.get("LOOKUP_POST_URL")

    log(f"access_token present: {bool(access_token)} (len={len(access_token) if access_token else 0})")
    log(f"account_id: {account_id!r}")

    if lookup_url:
        log(f"=== ONE-OFF LOOKUP MODE (permalink match) for {lookup_url} ===")
        target = lookup_url.rstrip("/")
        all_items = []
        page_url = (f"https://graph.instagram.com/{account_id}/media"
                    f"?fields=id,caption,media_type,timestamp,permalink&limit=50"
                    f"&access_token={urllib.parse.quote(access_token)}")
        pages = 0
        while page_url and pages < 200:
            try:
                data = http_get_json(page_url)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                log(f"HTTPError after {pages} page(s): {e.code} {e.reason} — body: {body[:1000]}")
                break
            except (urllib.error.URLError, TimeoutError) as e:
                log(f"Network error after {pages} page(s): {e}")
                break
            if "error" in data:
                log(f"API returned error payload after {pages} page(s): {json.dumps(data['error'])[:1000]}")
                break
            items = data.get("data", [])
            all_items.extend(items)
            pages += 1
            if pages % 10 == 0:
                log(f"  ...{pages} pages, {len(all_items)} items so far")
            page_url = data.get("paging", {}).get("next")
        log(f"Scanned {len(all_items)} total posts across {pages} page(s) (full history, no date filter).")
        if all_items:
            dated = [(m.get("timestamp") or "") for m in all_items if m.get("timestamp")]
            dated.sort()
            log(f"Earliest post timestamp in pull: {dated[0] if dated else 'N/A'}")
            log(f"Latest post timestamp in pull: {dated[-1] if dated else 'N/A'}")
            log("10 oldest posts in the pull:")
            oldest = sorted(all_items, key=lambda m: m.get("timestamp") or "")[:10]
            for m in oldest:
                log(f"  {m.get('timestamp')} id={m['id']} type={m.get('media_type')} permalink={m.get('permalink')}")

        match = None
        for m in all_items:
            pl = (m.get("permalink") or "").rstrip("/")
            if pl == target:
                match = m
                break
        if match:
            log(f"MATCH FOUND: {json.dumps(match, indent=2)}")
        else:
            log("NO MATCH by exact permalink. Showing any permalinks containing similar shortcode fragment:")
            frag = target.rstrip("/").split("/")[-1]
            near = [m for m in all_items if frag in (m.get("permalink") or "")]
            for m in near[:5]:
                log(f"  near: {json.dumps(m)}")
            log(f"Total items with a 'tv' or 'reel' style permalink: "
                f"{sum(1 for m in all_items if '/tv/' in (m.get('permalink') or '') or '/reel/' in (m.get('permalink') or ''))}")
        return

    log(f"since_date: {since_date!r}  until_date: {until_date!r}")

    if not all([access_token, account_id, since_date, until_date]):
        log("Missing one of: INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_BUSINESS_ACCOUNT_ID, SCAN_SINCE_DATE, SCAN_UNTIL_DATE")
        sys.exit(1)

    since_ts = int(datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    until_ts = int(datetime.strptime(until_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())

    log(f"Scanning {since_date} ({since_ts}) to {until_date} ({until_ts})...")

    all_items = []
    page_url = (f"https://graph.instagram.com/{account_id}/media"
                f"?fields=id,caption,media_type,timestamp&limit=50"
                f"&since={since_ts}&until={until_ts}"
                f"&access_token={urllib.parse.quote(access_token)}")
    pages = 0
    while page_url and pages < 100:
        try:
            data = http_get_json(page_url)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            log(f"HTTPError after {pages} page(s): {e.code} {e.reason} — body: {body[:1000]}")
            break
        except (urllib.error.URLError, TimeoutError) as e:
            log(f"Network error after {pages} page(s): {e}")
            break
        if "error" in data:
            log(f"API returned error payload after {pages} page(s): {json.dumps(data['error'])[:1000]}")
            break
        items = data.get("data", [])
        log(f"  page {pages + 1}: got {len(items)} items")
        all_items.extend(items)
        pages += 1
        page_url = data.get("paging", {}).get("next")

    log(f"Scanned {len(all_items)} total posts across {pages} page(s) in this date range.")

    # Diagnostics: media_type breakdown
    from collections import Counter
    type_counts = Counter(m.get("media_type", "UNKNOWN") for m in all_items)
    log(f"media_type breakdown: {dict(type_counts)}")

    # Diagnostics: near-misses — caption mentions "minhag" but doesn't match our exact hashtag
    near_misses = [m for m in all_items
                   if "minhag" in (m.get("caption") or "").lower()
                   and HASHTAG not in (m.get("caption") or "").lower()]
    log(f"Near-miss count (caption mentions 'minhag' but hashtag not matched): {len(near_misses)}")
    for m in near_misses[:15]:
        cap = (m.get("caption") or "").replace("\n", " ")[:200]
        log(f"  NEAR-MISS id={m['id']} type={m.get('media_type')} date={(m.get('timestamp') or '')[:10]} caption={cap!r}")

    # Diagnostics: exact hashtag match regardless of media_type
    tag_matches_any_type = [m for m in all_items if HASHTAG in (m.get("caption") or "").lower()]
    log(f"Posts with exact hashtag match (any media_type): {len(tag_matches_any_type)}")
    for m in tag_matches_any_type[:30]:
        log(f"  TAG-MATCH id={m['id']} type={m.get('media_type')} date={(m.get('timestamp') or '')[:10]}")

    # Deep diagnostic: dump raw captions (repr, showing hidden chars) for posts
    # near the two dates we know for certain are tagged (from manual screenshot
    # confirmation) but that aren't showing up as TAG-MATCH above.
    target_dates = {"2020-12-22", "2020-12-23", "2020-12-24", "2019-05-29", "2019-05-30", "2019-05-31"}
    log("=== RAW DUMP for posts near known-tagged dates (Episode 83 / Rituals of Shabuot) ===")
    near_date_items = [m for m in all_items if (m.get("timestamp") or "")[:10] in target_dates]
    log(f"Found {len(near_date_items)} post(s) with timestamp on those dates.")
    for m in near_date_items:
        cap = m.get("caption") or ""
        log(f"  id={m['id']} type={m.get('media_type')} date={(m.get('timestamp') or '')[:10]}")
        log(f"    caption repr: {cap!r}")

    matches = [m for m in all_items
               if m.get("media_type") in ("VIDEO", "REELS")
               and HASHTAG in (m.get("caption") or "").lower()]

    log(f"Found {len(matches)} tagged video/reel posts. Fetching view counts...")

    rows = []
    for m in matches:
        views = fetch_media_view_count(m["id"], access_token)
        rows.append({
            "date": (m.get("timestamp") or "")[:10],
            "views": views if views is not None else "UNAVAILABLE",
            "caption": (m.get("caption") or "").replace("\n", " ")[:300],
            "media_id": m["id"],
        })

    rows.sort(key=lambda r: r["date"], reverse=True)

    os.makedirs("data", exist_ok=True)
    with open("data/instagram-targeted-scan.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "views", "caption", "media_id"])
        w.writeheader()
        w.writerows(rows)

    log(f"Wrote {len(rows)} rows to data/instagram-targeted-scan.csv")


def main():
    exit_code = 0
    try:
        run()
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
    except Exception:
        log("UNCAUGHT EXCEPTION:")
        log(traceback.format_exc())
        exit_code = 1
    finally:
        os.makedirs("data", exist_ok=True)
        with open("data/instagram-targeted-scan-log.txt", "w") as f:
            f.write(f"Run at {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"Exit code: {exit_code}\n\n")
            f.write("\n".join(LOG_LINES))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
