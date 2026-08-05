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
    """
    'views' is the current, correct metric as of Meta's April 2025 overhaul,
    which consolidated the old 'plays'/'video_views'/'impressions' metrics
    into one unified number that matches what's shown in the Instagram app
    itself. Those old metric names are deprecated and were, in testing,
    silently falling through to 'reach' — a fundamentally different number
    (unique accounts reached, NOT total view count) that produced
    plausible-looking but wrong results. 'reach' is intentionally NOT used
    as a fallback here anymore for that reason: an honest "unavailable" is
    better than a confident wrong number.
    """
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


def save_instagram_cache(cache):
    os.makedirs("data", exist_ok=True)
    with open("data/instagram-cache.json", "w") as f:
        json.dump(cache, f, indent=2)


def get_instagram_total_views(access_token, account_id):
    """
    Keeps a cache (data/instagram-cache.json) of every #SCAMinhagOfTheWeek
    video/reel found so far and its view count. Two separate things happen
    each run, so brand-new episodes are caught quickly AND the full account
    history eventually gets scanned completely, without ever doing a slow
    full re-walk more than once:

    1. FRONT CHECK (every run, fast): looks at the newest posts and stops
       as soon as it reaches one already indexed. Catches new episodes
       within minutes of being posted.

    2. BACKFILL (every run, until done once): continues walking older and
       older posts using a saved resume-point ("backfill_cursor"), a
       bounded chunk at a time (10 pages / ~500 posts per run), until it
       reaches the very end of the account's history — at which point
       "backfill_complete" is set and this step does nothing on every
       future run. This is what makes sure genuinely old episodes (posted
       further back than a single run could reach) all eventually get
       counted, a little at a time, without one run ever taking too long
       or risking Instagram's rate limit.

    The running total is always the sum of everything found so far in the
    cache, so it only grows over time — it's never wrong-but-shrinking,
    just possibly still-growing until the one-time backfill finishes.

    Robustness: any single network hiccup (timeout, connection reset, a bad
    page) stops that stage early rather than crashing the whole script, and
    whatever was already found gets saved immediately — no lost progress.
    """
    cache_path = "data/instagram-cache.json"
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    cache.setdefault("media", {})
    cache.setdefault("newest_seen_id", None)
    cache.setdefault("backfill_cursor", None)
    cache.setdefault("backfill_complete", False)
    if cache.get("schema_version") != 2:
        # Upgrading from the old schema (view-count-only, no caption/date,
        # and posts that failed their insights lookup were silently never
        # recorded at all). Re-walk the full history once more so those
        # posts get a second chance to be captured — already-cached entries
        # are left alone, so this doesn't re-spend API calls on them.
        cache["backfill_cursor"] = None
        cache["backfill_complete"] = False
        cache["schema_version"] = 2
        save_instagram_cache(cache)
    if cache.get("schema_version") != 3:
        # The 'plays'/'video_views' metrics used until now are deprecated
        # (April 2025) and were silently falling back to 'reach', a
        # different and much-lower number than real view counts. Every
        # previously-recorded view count is therefore suspect — clear just
        # the view numbers (keep captions/dates, no need to re-fetch those)
        # and re-walk the full history once more to re-pull every view
        # count fresh using the correct 'views' metric.
        for entry in cache["media"].values():
            entry["views"] = 0
            entry["views_unavailable"] = True
            entry.pop("_needs_view_refetch", None)
        cache["backfill_cursor"] = None
        cache["backfill_complete"] = False
        cache["newest_seen_id"] = None
        cache["schema_version"] = 3
        save_instagram_cache(cache)

    known_ids = set(cache["media"].keys())
    base_media_url = (f"https://graph.instagram.com/{account_id}/media"
                       f"?fields=id,caption,media_type,timestamp&limit=50"
                       f"&access_token={urllib.parse.quote(access_token)}")

    diag_by_phase = {"front": {}, "backfill": {}}  # phase -> media_type -> count
    insights_failed_count = 0

    def process_items(items, phase):
        """Filters for matching video/reel posts. New posts get fully
        recorded (caption/date/view-count). Posts already in the cache but
        flagged views_unavailable get a fresh retry at just the view-count
        lookup (this is how a metric-name fix or a transient failure gets
        corrected on a later run, without re-doing the caption/date work).
        Every match is recorded even if the view-count lookup fails, so a
        post is never silently dropped and forgotten just because Instagram
        wouldn't return a number for it that particular time."""
        nonlocal insights_failed_count
        found = 0
        for m in items:
            has_tag = HASHTAG in (m.get("caption") or "").lower()
            if not has_tag:
                continue
            mtype = m.get("media_type", "UNKNOWN")
            diag_by_phase[phase][mtype] = diag_by_phase[phase].get(mtype, 0) + 1
            if mtype not in ("VIDEO", "REELS"):
                continue
            existing = cache["media"].get(m["id"])
            if existing is not None and not existing.get("views_unavailable"):
                continue  # already have a real view count for this one
            views = fetch_media_view_count(m["id"], access_token)
            if views is None:
                insights_failed_count += 1
                print(f"Instagram: no view count available for media {m['id']} (type {mtype})", file=sys.stderr)
            cache["media"][m["id"]] = {
                "views": views if views is not None else 0,
                "views_unavailable": views is None,
                "caption": (m.get("caption") or "")[:300],
                "timestamp": m.get("timestamp"),
                "media_type": mtype,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            found += 1
            save_instagram_cache(cache)
        return found

    # ── 0. One-time enrichment: add caption/date to older cache entries that
    #      predate this fields update, without re-fetching their view counts ──
    for media_id, entry in cache["media"].items():
        if "caption" not in entry:
            try:
                detail_url = (f"https://graph.instagram.com/{media_id}"
                               f"?fields=caption,timestamp,media_type"
                               f"&access_token={urllib.parse.quote(access_token)}")
                detail = http_get_json(detail_url)
                entry["caption"] = (detail.get("caption") or "")[:300]
                entry["timestamp"] = detail.get("timestamp")
                entry["media_type"] = detail.get("media_type")
                entry.setdefault("views_unavailable", False)
                save_instagram_cache(cache)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                print(f"Instagram: couldn't enrich older entry {media_id}: {e}", file=sys.stderr)

    # ── 1. Front check: newest posts, stop at the first one we already know ──
    front_new_found = 0
    front_pages = 0
    url = base_media_url
    newest_id_this_run = None
    while url and front_pages < 3:
        try:
            data = http_get_json(url)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            print(f"Instagram front check stopped early: {e}", file=sys.stderr)
            break
        items = data.get("data", [])
        if items and newest_id_this_run is None:
            newest_id_this_run = items[0]["id"]
        front_pages += 1
        reached_known = cache["newest_seen_id"] and any(m["id"] == cache["newest_seen_id"] for m in items)
        front_new_found += process_items(items, "front")
        if reached_known or not cache["newest_seen_id"]:
            break
        url = data.get("paging", {}).get("next")
    if newest_id_this_run:
        cache["newest_seen_id"] = newest_id_this_run
        save_instagram_cache(cache)

    # ── 2. Backfill: continue deeper into history, a bounded chunk at a time ──
    backfill_new_found = 0
    backfill_pages = 0
    stopped_early_reason = None
    cache.setdefault("total_posts_scanned_ever", 0)
    last_post_id_this_backfill = None
    if not cache["backfill_complete"]:
        url = cache["backfill_cursor"] or base_media_url
        while url and backfill_pages < 70:
            try:
                data = http_get_json(url)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                stopped_early_reason = f"network error during backfill after {backfill_pages} page(s): {e}"
                print(f"Instagram: {stopped_early_reason}", file=sys.stderr)
                break
            items = data.get("data", [])
            if items:
                last_post_id_this_backfill = items[-1]["id"]
            backfill_new_found += process_items(items, "backfill")
            cache["total_posts_scanned_ever"] += len(items)
            backfill_pages += 1
            url = data.get("paging", {}).get("next")
            cache["backfill_cursor"] = url
            save_instagram_cache(cache)
        if not url and stopped_early_reason is None:
            cache["backfill_complete"] = True
            save_instagram_cache(cache)

    total_views = sum(v["views"] for v in cache["media"].values())
    views_unavailable_count = sum(1 for v in cache["media"].values() if v.get("views_unavailable"))
    diagnostics = {
        "backfill_complete": cache["backfill_complete"],
        "backfill_pages_this_run": backfill_pages,
        "stopped_early_reason": stopped_early_reason,
        "front_check_hashtag_matches_by_type": diag_by_phase["front"],
        "backfill_hashtag_matches_by_type": diag_by_phase["backfill"],
        "total_posts_scanned_ever": cache["total_posts_scanned_ever"],
        "last_post_id_reached_this_run": last_post_id_this_backfill,
        "insights_failed_this_run": insights_failed_count,
        "views_unavailable_total": views_unavailable_count,
    }
    return total_views, len(cache["media"]), front_new_found + backfill_new_found, diagnostics


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
            ig_views, ig_counted, ig_matched, ig_diag = get_instagram_total_views(ig_token, ig_account_id)
            if ig_diag["backfill_complete"]:
                backfill_status = "full history scan complete"
            else:
                backfill_status = (f"still backfilling — {ig_diag['total_posts_scanned_ever']} posts scanned "
                                    f"so far out of ~4,817 total on the account")
            ig_note = (f"{ig_counted} tagged videos indexed total ({ig_matched} newly found this run); "
                       f"{backfill_status}; "
                       f"NEW matches found during backfill this run (by type): {ig_diag['backfill_hashtag_matches_by_type']}; "
                       f"matches seen during front-check (mostly re-seeing already-known posts, by type): {ig_diag['front_check_hashtag_matches_by_type']}; "
                       f"posts with no view-count available (still counted in the list, contribute 0 views): {ig_diag['views_unavailable_total']}"
                       f"{' — ' + ig_diag['stopped_early_reason'] if ig_diag['stopped_early_reason'] else ''}")
        except Exception as e:
            print(f"Instagram step failed this run ({e}) — Instagram contributes 0, YouTube/SproutVideo unaffected", file=sys.stderr)
            ig_note = "error this run — contributed 0, check logs"

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

