"""Re-picks the thumbnail for an episode already on the site.

    python scripts/repick_thumbnail.py 308            # auto: sharp frame, eyes open
    python scripts/repick_thumbnail.py 308 --at 95    # exact second of the video

Updates the SproutVideo poster, images/episode-N-thumb.jpg (link previews and
the email), the THUMBS entry in index.html, bumps the version, and pushes.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import publish_episode as pe  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--at", type=float, default=None)
    a = ap.parse_args()
    if not pe.SPROUT_KEY:
        raise SystemExit("Missing SPROUTVIDEO_API_KEY.")

    v = pe.find_sprout_video(a.episode)
    if not v:
        raise SystemExit(f"No SproutVideo upload found for episode {a.episode}")
    thumb_url, _, og_path = pe.make_thumbnail(
        a.episode, v["id"], v["security_token"], v["assets"]["videos"]["480p"], v["duration"], a.at)

    with open(pe.INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    html, n = re.subn(rf'"{re.escape(a.episode)}": "[^"]*"', f'"{a.episode}": "{thumb_url}"', html, count=1)
    if n != 1:
        raise SystemExit(f"Episode {a.episode} has no THUMBS entry in index.html")
    ver = str(int(time.time()))
    html = re.sub(r'<meta name="build-version" content="\d+">', f'<meta name="build-version" content="{ver}">', html)
    with open(pe.INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    with open(pe.VERSION_JSON, "w", encoding="utf-8") as f:
        f.write(json.dumps({"v": ver}))

    git = lambda *x: subprocess.run(["git", *x], check=True, cwd=pe.REPO_ROOT)
    git("config", "user.name", "minhag-publish-bot")
    git("config", "user.email", "actions@github.com")
    git("add", "index.html", "version.json", og_path)
    git("commit", "-m", f"New thumbnail for episode {a.episode}")
    pe.push_with_retry()
    print(f"Done. New thumbnail for episode {a.episode}: {thumb_url}")


if __name__ == "__main__":
    main()
