#!/usr/bin/env python3
"""
Full export: every post ever made on @SCA_updates, with view counts where
available (VIDEO/REELS only — Instagram's 'views' insight isn't defined for
IMAGE/CAROUSEL_ALBUM posts, those get "N/A"). Resumable across runs via
data/full-export-state.json, since fetching insights for thousands of posts
will hit rate limits / time budgets in a single run.

Writes/updates on every run:
  data/full-export-state.json  — full post list + views fetched so far + pagination cursor
  data/full-export.csv         — current snapshot, regenerated from state each run
  data/full-export-log.txt     — run log

Required environment variables:
  INSTAGRAM_ACCESS_TOKEN
  INSTAGRAM_BUSINESS_ACCOUNT_ID

Optional:
  TIME_BUDGET_SECONDS (default 240) — stop fetching insights after this long
  and commit progress, so the job finishes well within Actions limits.
"""
import csv
import json
import os
import sys
import time
import traceback
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

STATE_PATH = "data/full-export-state.json"
CSV_PATH = "data/full-export.csv"
LOG_PATH = "data/full-export-log.txt"

LOG_LINES = []


def log(msg):
    print(msg)
    LOG_LINES.append(str(msg))


def http_get_json(url, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(3)
                continue
            raise
    raise last_err


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"items": {}, "pagination_done": False, "next_page_url": None, "views_done": False}


def save_state(state):
    os.makedirs("data", exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def write_csv(state):
    items = state["items"]
    rows = list(items.values())
    rows.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    os.makedirs("data", exist_ok=True)
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "date", "media_type", "views", "hashtag_match", "permalink", "caption"])
        w.writeheader()
        for r in rows:
            w.writerow({
                "id": r["id"],
                "date": (r.get("timestamp") or "")[:10],
                "media_type": r.get("media_type"),
                "views": r.get("views", "NOT_FETCHED"),
                "hashtag_match": r.get("hashtag_match"),
                "permalink": r.get("permalink"),
                "caption": (r.get("caption") or "").replace("\n", " ")[:500],
            })


def fetch_media_views(media_id, access_token):
    insights_url = (f"https://graph.instagram.com/{media_id}/insights"
                     f"?metric=views"
                     f"&access_token={urllib.parse.quote(access_token)}")
    try:
        idata = http_get_json(insights_url, retries=2)
        values = idata.get("data", [])
        if values:
            return int(values[0].get("values", [{}])[0].get("value", 0))
        return 0
    except urllib.error.HTTPError:
        return "N/A"
    except Exception as e:
        return f"ERROR:{e}"


HASHTAG = "#scaminhagoftheweek"


def run():
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    account_id = os.environ.get("INSTAGRAM_BUSINESS_ACCOUNT_ID")
    time_budget = int(os.environ.get("TIME_BUDGET_SECONDS", "240"))
    start = time.time()

    if not access_token or not account_id:
        log("Missing INSTAGRAM_ACCESS_TOKEN or INSTAGRAM_BUSINESS_ACCOUNT_ID")
        sys.exit(1)

    state = load_state()
    log(f"Loaded state: {len(state['items'])} items known, pagination_done={state['pagination_done']}, "
        f"views_done={state.get('views_done')}")

    # Phase 1: paginate through the full post list (only if not already done)
    if not state["pagination_done"]:
        page_url = state.get("next_page_url") or (
            f"https://graph.instagram.com/{account_id}/media"
            f"?fields=id,caption,media_type,timestamp,permalink&limit=50"
            f"&access_token={urllib.parse.quote(access_token)}")
        pages = 0
        while page_url and (time.time() - start) < time_budget:
            try:
                data = http_get_json(page_url)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                log(f"Pagination error, will resume next run: {e}")
                break
            if "error" in data:
                log(f"API error payload: {json.dumps(data['error'])[:500]}")
                break
            items = data.get("data", [])
            for m in items:
                cap = (m.get("caption") or "").lower()
                state["items"][m["id"]] = {
                    "id": m["id"],
                    "caption": m.get("caption"),
                    "media_type": m.get("media_type"),
                    "timestamp": m.get("timestamp"),
                    "permalink": m.get("permalink"),
                    "hashtag_match": HASHTAG in cap,
                    "views": state["items"].get(m["id"], {}).get("views", "NOT_FETCHED"),
                }
            pages += 1
            next_url = data.get("paging", {}).get("next")
            state["next_page_url"] = next_url
            page_url = next_url
            if pages % 10 == 0:
                log(f"  paginated {pages} pages this run, {len(state['items'])} total items known")
        if not page_url:
            state["pagination_done"] = True
            log(f"Pagination COMPLETE. Total items: {len(state['items'])}")
        else:
            log(f"Pagination not yet complete this run ({len(state['items'])} items so far), will resume next run.")
        save_state(state)
        write_csv(state)

    if not state["pagination_done"]:
        log("Stopping this run after pagination phase (time budget). Re-run to continue.")
        return

    # Phase 2: fetch views for VIDEO/REELS items that don't have them yet
    to_fetch = [m for m in state["items"].values()
                if m.get("media_type") in ("VIDEO", "REELS") and m.get("views") == "NOT_FETCHED"]
    # mark non-video types as N/A immediately (no API call needed)
    changed = False
    for m in state["items"].values():
        if m.get("media_type") not in ("VIDEO", "REELS") and m.get("views") == "NOT_FETCHED":
            m["views"] = "N/A (not video)"
            changed = True

    log(f"{len(to_fetch)} video/reel items still need view counts.")
    fetched_this_run = 0
    for m in to_fetch:
        if (time.time() - start) >= time_budget:
            log(f"Time budget reached after fetching {fetched_this_run} view counts this run.")
            break
        m["views"] = fetch_media_views(m["id"], access_token)
        fetched_this_run += 1
        changed = True
        if fetched_this_run % 50 == 0:
            log(f"  fetched {fetched_this_run} view counts this run...")
            save_state(state)
            write_csv(state)

    remaining = sum(1 for m in state["items"].values()
                     if m.get("media_type") in ("VIDEO", "REELS") and m.get("views") == "NOT_FETCHED")
    state["views_done"] = (remaining == 0)
    log(f"Fetched {fetched_this_run} view counts this run. Remaining: {remaining}. views_done={state['views_done']}")

    save_state(state)
    write_csv(state)
    log(f"Wrote {len(state['items'])} rows to {CSV_PATH}")


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
        with open(LOG_PATH, "w") as f:
            f.write(f"Run at {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"Exit code: {exit_code}\n\n")
            f.write("\n".join(LOG_LINES))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
