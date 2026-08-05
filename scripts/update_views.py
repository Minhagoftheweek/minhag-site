#!/usr/bin/env python3
"""
Pulls total view counts from YouTube (a specific playlist), SproutVideo
(the whole library), and Instagram (videos tagged #SCAMinhagOfTheWeek on
@SCA_updates), sums them into one grand total, and writes it to
data/view-count.json for the site to read.

Required environment variables (set as GitHub Actions secrets):
  YOUTUBE_API_KEY
  YOUTUBE_PLAYLIST_ID
  SPROUTVIDEO_API_KEY
  INSTAGRAM_ACCESS_TOKEN
  INSTAGRAM_BUSINESS_ACCOUNT_ID

Note on the Instagram token: it is not a classic long-lived Facebook token —
attempts to run it through the standard ig_exchange_token flow failed, but
the token itself works fine directly against the Instagram Graph API. Its
real expiry behavior hasn't been independently confirmed. If it ever stops
working, the workflow logs a clear warning and Instagram's contribution
simply drops to 0 for that run (YouTube + SproutVideo still update normally)
rather than the whole job failing silently.
"""
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


def sv_get_json(url, api_key):
    req = urllib.request.Request(url, headers={"SproutVideo-Api-Key": api_key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get_youtube_total_views(api_key, playlist_id):
    video_ids = []
    page_token = ""
    while True:
        params = {
            "part": "contentDetails",
            "maxResults": "50",
            "playlistId": playlist_id,
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        url = "https://www.googleapis.com/youtube/v3/playlistItems?" + urllib.parse.urlencode(params)
        data = http_get_json(url)
        for item in data.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId")
            if vid:
                video_ids.append(vid)
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    total_views = 0
    # videos.list allows up to 50 ids per request
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        params = {
            "part": "statistics",
            "id": ",".join(chunk),
            "key": api_key,
        }
        url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params)
        data = http_get_json(url)
        for item in data.get("items", []):
            views = item.get("statistics", {}).get("viewCount")
            if views is not None:
                total_views += int(views)

    return total_views, len(video_ids)


def get_sproutvideo_total_plays(api_key):
    total_plays = 0
    total_videos = 0
    page = 1
    while True:
        url = f"https://api.sproutvideo.com/v1/videos?count=100&page={page}"
        data = sv_get_json(url, api_key)
        videos = data.get("videos", [])
        if not videos:
            break
        for v in videos:
            total_plays += v.get("plays", 0)
            total_videos += 1
        if total_videos >= data.get("total", 0):
            break
        page += 1
    return total_plays, total_videos


def fetch_media_view_count(media_id, access_token):
    for metric in ("plays", "video_views", "reach"):
        insights_url = (f"https://graph.instagram.com/{media_id}/insights"
                         f"?metric={metric}"
                         f"&access_token={urllib.parse.quote(access_token)}")
        try:
            idata = http_get_json(insights_url)
            values = idata.get("data", [])
            if values:
                return int(values[0].get("values", [{}])[0].get("value", 0))
        except urllib.error.HTTPError:
            continue
    return None


def get_instagram_total_views(access_token, account_id):
    """
    Keeps a cache (data/instagram-cache.json) of every #SCAMinhagOfTheWeek
    video/reel found so far and its view count, so routine runs don't have
    to re-walk the account's entire post history (which is slow and risks
    Instagram's 200-calls/hour limit given this runs every 5 minutes).

    First run ever (empty/missing cache): walks up to ~3,000 recent posts
    (60 pages of 50) to backfill the cache — this one run may take a while.
    Every run after that: only walks forward from the newest post until it
    reaches a post it has already seen, so it's typically just 1-2 quick
    page fetches. The running total is the sum of everything in the cache,
    so already-found videos keep counting even on runs that find nothing new.
    """
    cache_path = "data/instagram-cache.json"
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {"media": {}}

    known_ids = set(cache["media"].keys())
    is_first_run = len(known_ids) == 0
    max_pages = 60 if is_first_run else 5

    media_items = []
    url = (f"https://graph.instagram.com/{account_id}/media"
           f"?fields=id,caption,media_type&limit=50"
           f"&access_token={urllib.parse.quote(access_token)}")
    pages = 0
    while url and pages < max_pages:
        data = http_get_json(url)
        items = data.get("data", [])
        media_items.extend(items)
        pages += 1
        if not is_first_run and any(m["id"] in known_ids for m in items):
            break  # caught up to posts we've already indexed
        url = data.get("paging", {}).get("next")

    matching_new = [
        m for m in media_items
        if m["id"] not in known_ids
        and m.get("media_type") in ("VIDEO", "REELS")
        and HASHTAG in (m.get("caption") or "").lower()
    ]

    for m in matching_new:
        views = fetch_media_view_count(m["id"], access_token)
        if views is not None:
            cache["media"][m["id"]] = {
                "views": views,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            print(f"Instagram: could not get view count for media {m['id']}, skipped", file=sys.stderr)

    os.makedirs("data", exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=2)

    total_views = sum(v["views"] for v in cache["media"].values())
    return total_views, len(cache["media"]), len(matching_new)


def main():
    yt_key = os.environ.get("YOUTUBE_API_KEY")
    yt_playlist = os.environ.get("YOUTUBE_PLAYLIST_ID")
    sv_key = os.environ.get("SPROUTVIDEO_API_KEY")
    ig_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    ig_account_id = os.environ.get("INSTAGRAM_BUSINESS_ACCOUNT_ID")

    if not yt_key or not yt_playlist:
        print("Missing YOUTUBE_API_KEY or YOUTUBE_PLAYLIST_ID", file=sys.stderr)
        sys.exit(1)
    if not sv_key:
        print("Missing SPROUTVIDEO_API_KEY", file=sys.stderr)
        sys.exit(1)

    try:
        yt_views, yt_video_count = get_youtube_total_views(yt_key, yt_playlist)
    except urllib.error.HTTPError as e:
        print(f"YouTube API error: {e.code} {e.read().decode()}", file=sys.stderr)
        sys.exit(1)

    try:
        sv_plays, sv_video_count = get_sproutvideo_total_plays(sv_key)
    except urllib.error.HTTPError as e:
        print(f"SproutVideo API error: {e.code} {e.read().decode()}", file=sys.stderr)
        sys.exit(1)

    ig_views, ig_counted, ig_matched = 0, 0, 0
    ig_note = "not configured"
    if ig_token and ig_account_id:
        try:
            ig_views, ig_counted, ig_matched = get_instagram_total_views(ig_token, ig_account_id)
            ig_note = f"{ig_counted} total tagged videos indexed ({ig_matched} newly found this run)"
        except urllib.error.HTTPError as e:
            print(f"Instagram API error: {e.code} {e.read().decode()} — Instagram contributes 0 this run", file=sys.stderr)
            ig_note = "token/API error this run — contributed 0, check logs"

    total = yt_views + sv_plays + ig_views

    out = {
        "total_views": total,
        "breakdown": {
            "youtube": {"views": yt_views, "video_count": yt_video_count},
            "sproutvideo": {"plays": sv_plays, "video_count": sv_video_count},
            "instagram": {"views": ig_views, "note": ig_note},
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    os.makedirs("data", exist_ok=True)
    with open("data/view-count.json", "w") as f:
        json.dump(out, f, indent=2)

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

