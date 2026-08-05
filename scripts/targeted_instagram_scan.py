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
"""
import csv
import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

HASHTAG = "#scaminhagoftheweek"


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
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        pass
    return None


def main():
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    account_id = os.environ.get("INSTAGRAM_BUSINESS_ACCOUNT_ID")
    since_date = os.environ.get("SCAN_SINCE_DATE")
    until_date = os.environ.get("SCAN_UNTIL_DATE")

    if not all([access_token, account_id, since_date, until_date]):
        print("Missing one of: INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_BUSINESS_ACCOUNT_ID, SCAN_SINCE_DATE, SCAN_UNTIL_DATE", file=sys.stderr)
        sys.exit(1)

    since_ts = int(datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    until_ts = int(datetime.strptime(until_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())

    print(f"Scanning {since_date} to {until_date}...")

    all_items = []
    page_url = (f"https://graph.instagram.com/{account_id}/media"
                f"?fields=id,caption,media_type,timestamp&limit=50"
                f"&since={since_ts}&until={until_ts}"
                f"&access_token={urllib.parse.quote(access_token)}")
    pages = 0
    while page_url and pages < 100:
        try:
            data = http_get_json(page_url)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            print(f"Network error after {pages} page(s): {e}", file=sys.stderr)
            break
        items = data.get("data", [])
        all_items.extend(items)
        pages += 1
        page_url = data.get("paging", {}).get("next")

    print(f"Scanned {len(all_items)} total posts across {pages} page(s) in this date range.")

    matches = [m for m in all_items
               if m.get("media_type") in ("VIDEO", "REELS")
               and HASHTAG in (m.get("caption") or "").lower()]

    print(f"Found {len(matches)} tagged video/reel posts. Fetching view counts...")

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

    print(f"Wrote {len(rows)} rows to data/instagram-targeted-scan.csv")


if __name__ == "__main__":
    main()
