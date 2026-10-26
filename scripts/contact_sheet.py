"""Makes a grid of frames from across an episode's video, each labeled with its
second, so a thumbnail can be picked by eye.

    python scripts/contact_sheet.py 308   ->  logs/contact-sheet-308.jpg
"""
import os
import subprocess
import sys
import tempfile
import urllib.request

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import publish_episode as pe  # noqa: E402

COLS, ROWS, W = 6, 10, 320


def main():
    ep = sys.argv[1]
    v = pe.find_sprout_video(ep)
    if not v:
        raise SystemExit(f"No SproutVideo upload found for episode {ep}")
    dur = v["duration"]
    with tempfile.TemporaryDirectory() as d:
        mp4 = os.path.join(d, "v.mp4")
        req = urllib.request.Request(v["assets"]["videos"]["480p"], headers={"SproutVideo-Api-Key": pe.SPROUT_KEY})
        with urllib.request.urlopen(req) as r, open(mp4, "wb") as f:
            f.write(r.read())
        n = COLS * ROWS
        frames = []
        for i in range(n):
            t = dur * (i + 0.5) / n
            p = os.path.join(d, f"{i}.jpg")
            subprocess.run(["ffmpeg", "-y", "-ss", f"{t:.1f}", "-i", mp4, "-frames:v", "1",
                            "-vf", f"scale={W}:-2", p], check=True, capture_output=True)
            frames.append((t, Image.open(p).convert("RGB")))
        h = frames[0][1].height
        sheet = Image.new("RGB", (COLS * W, ROWS * h), "white")
        draw = ImageDraw.Draw(sheet)
        for i, (t, im) in enumerate(frames):
            x, y = (i % COLS) * W, (i // COLS) * h
            sheet.paste(im, (x, y))
            draw.rectangle([x, y, x + 70, y + 22], fill="black")
            draw.text((x + 6, y + 5), f"{t:.0f}s", fill="yellow")
    out = os.path.join(pe.REPO_ROOT, "logs", f"contact-sheet-{ep}.jpg")
    sheet.save(out, quality=80)
    print(out)


if __name__ == "__main__":
    main()
