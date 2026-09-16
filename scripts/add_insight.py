#!/usr/bin/env python3
"""
Add a "Further Insights" link to an existing episode's INSIGHTS entry in
index.html, and ONLY that — never touches the EPS array, THUMBS map, or
SCHEDULE map.

Why this script exists:
On 2026-09-09, a manual/ad-hoc edit meant to add a Further Insights link to
episode 306 also (accidentally) rewrote and truncated the EPS array,
silently dropping episode 306's own listing entry for a week. The cause was
editing the giant single-line EPS array by hand/by re-pasting it, which is
exactly the kind of edit a human or an LLM can get subtly wrong with no
visible symptom until someone actually looks for the missing episode.

This script never re-writes the EPS array at all. It:
  1. Records a fingerprint of the EPS array (entry count + episode numbers,
     in order) before touching the file.
  2. Makes a single, narrow, anchor-based string insertion into the
     INSIGHTS object only.
  3. Re-parses EPS afterward and hard-fails (raises, no commit) if the
     fingerprint changed in any way.
  4. Only after that safety check passes does it bump build-version,
     write version.json, and commit/push.

Usage:
  python3 scripts/add_insight.py --episode 210 \
      --label "Further insights on this topic:" \
      --type siteLink --ep-id 149

  python3 scripts/add_insight.py --episode 210 \
      --label "In-depth article written by Joseph Mosseri on this topic:" \
      --type pdf --url /insights/sca-minhag-of-the-week-episode-210.pdf

  python3 scripts/add_insight.py --episode 210 \
      --label "Watch related video:" \
      --type embed --video-url https://videos.sproutvideo.com/embed/xxx/yyy
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

REPO_ROOT = os.environ.get("GITHUB_WORKSPACE", ".")
INDEX_HTML = os.path.join(REPO_ROOT, "index.html")
VERSION_JSON = os.path.join(REPO_ROOT, "version.json")


def extract_balanced(text, start_idx):
    """Given the index of an opening '[' or '{', return (content, end_idx)
    where end_idx is one past the matching close, respecting quoted
    strings and escapes so brackets inside string literals don't confuse
    the depth count."""
    open_ch = text[start_idx]
    close_ch = {"[": "]", "{": "}"}[open_ch]
    depth = 0
    in_str = False
    quote = None
    escape = False
    i = start_idx
    while i < len(text):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == quote:
                in_str = False
        else:
            if c in ("'", '"'):
                in_str = True
                quote = c
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return text[start_idx:i + 1], i + 1
        i += 1
    raise ValueError(f"Unbalanced {open_ch!r} starting at {start_idx}")


def eps_fingerprint(html):
    """Return (count, [episode_numbers_in_order]) for the current EPS
    array, without ever needing to touch or rewrite it."""
    idx = html.index("const EPS=")
    bracket_start = html.index("[", idx)
    array_text, _ = extract_balanced(html, bracket_start)
    # Each top-level entry starts with '[<number>,' — pull just the leading
    # numbers of each top-level element, respecting nesting depth.
    numbers = []
    depth = 0
    in_str = False
    quote = None
    escape = False
    i = 0
    n = len(array_text)
    while i < n:
        c = array_text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == quote:
                in_str = False
            i += 1
            continue
        if c in ("'", '"'):
            in_str = True
            quote = c
            i += 1
            continue
        if c == "[":
            depth += 1
            if depth == 2:
                # Start of a top-level entry; read its leading number.
                m = re.match(r"\[(\d+)", array_text[i:])
                if m:
                    numbers.append(m.group(1))
            i += 1
            continue
        if c == "]":
            depth -= 1
            i += 1
            continue
        i += 1
    return len(numbers), numbers


def build_insight_js(label, entry_type, url=None, ep_id=None, video_url=None):
    label_escaped = label.replace("\\", "\\\\").replace("'", "\\'")
    parts = [f"label:'{label_escaped}'", f"type:'{entry_type}'"]
    if entry_type == "siteLink":
        if ep_id is None:
            raise ValueError("siteLink insights require --ep-id")
        parts.append(f"epId:{int(ep_id)}")
    elif entry_type == "pdf":
        if not url:
            raise ValueError("pdf insights require --url")
        parts.append(f"url:'{url}'")
    elif entry_type in ("embed", "video"):
        if not video_url:
            raise ValueError(f"{entry_type} insights require --video-url")
        parts.append(f"url:'{video_url}'")
    else:
        raise ValueError(f"Unknown insight type: {entry_type}")
    return "[{" + ",".join(parts) + "}]"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--episode", required=True, type=int)
    p.add_argument("--label", required=True)
    p.add_argument("--type", required=True, choices=["siteLink", "pdf", "embed", "video"])
    p.add_argument("--ep-id", type=int, default=None)
    p.add_argument("--url", default=None)
    p.add_argument("--video-url", default=None)
    p.add_argument("--no-push", action="store_true", help="Commit locally but skip git push (for testing)")
    args = p.parse_args()

    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    before_count, before_numbers = eps_fingerprint(html)
    print(f"EPS before edit: {before_count} entries (min={before_numbers[-1]}, max={before_numbers[0]})")

    # --- Locate the INSIGHTS object and insert narrowly, right after its
    # opening brace. This never touches any existing INSIGHTS entry, and
    # is textually nowhere near EPS/THUMBS/SCHEDULE. ---
    insights_marker = "const INSIGHTS={"
    if insights_marker not in html:
        raise RuntimeError("Could not find 'const INSIGHTS={' in index.html")

    insight_js = build_insight_js(
        args.label, args.type, url=args.url, ep_id=args.ep_id, video_url=args.video_url
    )
    new_entry_line = f"\n  {args.episode}:{insight_js},"

    insert_pos = html.index(insights_marker) + len(insights_marker)
    new_html = html[:insert_pos] + new_entry_line + html[insert_pos:]

    # --- Safety check: EPS must be byte-for-byte unchanged in shape. ---
    after_count, after_numbers = eps_fingerprint(new_html)
    if (after_count, after_numbers) != (before_count, before_numbers):
        raise RuntimeError(
            "SAFETY ABORT: EPS array fingerprint changed after an INSIGHTS-only "
            f"edit (before: {before_count} entries, after: {after_count} entries). "
            "This should be impossible with this script's insertion method — "
            "not writing the file. Investigate before retrying."
        )
    print("Safety check passed: EPS array unchanged.")

    # Sanity-check the episode we're annotating actually exists in EPS.
    if str(args.episode) not in after_numbers:
        print(
            f"WARNING: episode {args.episode} not found in the EPS array — "
            "the insight link will be added, but nothing on the site links "
            "to it unless the episode is also published.",
            file=sys.stderr,
        )

    build_version = str(int(time.time()))
    new_html = re.sub(
        r'<meta name="build-version" content="\d+">',
        f'<meta name="build-version" content="{build_version}">',
        new_html,
    )

    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(new_html)
    with open(VERSION_JSON, "w", encoding="utf-8") as f:
        f.write(json.dumps({"v": build_version}))

    subprocess.run(["git", "config", "user.name", "minhag-publish-bot"], check=True, cwd=REPO_ROOT)
    subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True, cwd=REPO_ROOT)
    subprocess.run(["git", "add", "index.html", "version.json"], check=True, cwd=REPO_ROOT)
    subprocess.run(
        ["git", "commit", "-m", f"Add Further Insights link to episode {args.episode} (via add_insight.py)"],
        check=True, cwd=REPO_ROOT,
    )
    if not args.no_push:
        subprocess.run(["git", "push"], check=True, cwd=REPO_ROOT)

    print(f"Done. Insight added to episode {args.episode}.")


if __name__ == "__main__":
    main()
