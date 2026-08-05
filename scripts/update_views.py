#!/usr/bin/env python3
"""
Pulls total view counts from YouTube (a specific playlist) and SproutVideo
(the whole library), sums them into one grand total, and writes it to
data/view-count.json for the site to read.

Instagram is not wired up yet — INSTAGRAM_VIEWS is a placeholder that gets
added into the total once that integration exists, so the site doesn't
need to change again when it's added.

Required environment variables (set as GitHub Actions secrets):
  YOUTUBE_API_KEY
  YOUTUBE_PLAYLIST_ID
  SPROUTVIDEO_API_KEY
Optional:
  INSTAGRAM_VIEWS   (a static override int, until real Instagram pulling exists)
"""
import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone


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


def main():
    yt_key = os.environ.get("YOUTUBE_API_KEY")
    yt_playlist = os.environ.get("YOUTUBE_PLAYLIST_ID")
    sv_key = os.environ.get("SPROUTVIDEO_API_KEY")
    instagram_views = int(os.environ.get("INSTAGRAM_VIEWS", "0"))

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

    total = yt_views + sv_plays + instagram_views

    out = {
        "total_views": total,
        "breakdown": {
            "youtube": {"views": yt_views, "video_count": yt_video_count},
            "sproutvideo": {"plays": sv_plays, "video_count": sv_video_count},
            "instagram": {"views": instagram_views, "note": "placeholder until Instagram Graph API is wired up"},
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    os.makedirs("data", exist_ok=True)
    with open("data/view-count.json", "w") as f:
        json.dump(out, f, indent=2)

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
