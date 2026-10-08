#!/usr/bin/env python3
"""Builds the weekly Minhag of the Week email for one episode.

Everything in the email comes from the same data the website uses
(index.html + transcripts.json), so the email always matches the episode's
page on the site.

Usage:
    python scripts/build_email.py 23 --out /tmp/email.html
    python scripts/build_email.py 23 --out /tmp/email.html --asset-base http://localhost:8765

It also writes two image files into images/email/ (a play-button version of
the episode thumbnail, and an email-sized logo). Those have to be live on the
site before the email is sent, because email apps load images from the web.
"""
import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = "https://minhagoftheweek.com"
NY = ZoneInfo("America/New_York")
SUBSCRIBE_URL = "https://youtube.us3.list-manage.com/subscribe?u=af8f644070b5e43b83849c47c&id=ad6b593467"

NAMES = {
    "Mosseri": "Joseph Mosseri",
    "Arking": "Morris Arking",
    "Harari": "Joey Harari",
    "All 3": "Joseph Mosseri, Morris Arking & Joey Harari",
    "Harari ft Arking": "Joey Harari & Morris Arking",
}

# Site colors (same values as :root in index.html)
NAVY, NAVY_DARK, PURPLE, PURPLE_LIGHT, PURPLE_DARK = "#184878", "#0f2d49", "#8b54b2", "#f0e6fa", "#6b3d8a"
CREAM, BORDER, TEXT, MUTED = "#f5f7fa", "#d0dae8", "#1a2a3a", "#5a7090"
SANS = "Arial,Helvetica,sans-serif"
SERIF = "Georgia,'Times New Roman',serif"

# How much transcript to show before the "keep reading" link.
EXCERPT_WORDS = 170
# An episode released within this many days of the send date counts as new.
NEW_WINDOW_DAYS = 10


# ── Reading the site's data ────────────────────────────────────────────────
def _js_array(src, marker):
    """Returns the JSON text of the array literal that follows `marker`."""
    i = src.index(marker) + len(marker)
    depth, j, in_str, esc = 0, i, False, False
    while True:
        c = src[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1


def _js_unescape(s):
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    return s.replace("\\'", "'").replace('\\"', '"').replace("\\&", "&")


def load_site():
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
        src = f.read()
    eps = json.loads(_js_array(src, "const EPS="))

    cats = {m.group(1): _js_unescape(m.group(2))
            for m in re.finditer(r"\{id:'([a-z]+)',label:'((?:[^'\\]|\\.)*)'", src)}

    insights = {}
    block = src[src.index("const INSIGHTS={"):]
    block = block[:block.index("\n};")]
    key = None
    for m in re.finditer(
        r"(?m)^\s*(\d+):\[|\{label:'((?:[^'\\]|\\.)*)',type:'(\w+)',(?:url:'([^']*)'|epId:(\d+))\}", block):
        if m.group(1):
            key = int(m.group(1))
        elif key is not None:
            insights.setdefault(key, []).append({
                "label": _js_unescape(m.group(2)), "type": m.group(3),
                "url": m.group(4), "epId": int(m.group(5)) if m.group(5) else None})

    schedule = {}
    sm = re.search(r"const SCHEDULE=\{(.*?)\n\};", src, re.S)
    if sm:
        for m in re.finditer(r"(?m)^\s*(\d+):\s*'([^']+)'", sm.group(1)):
            schedule[int(m.group(1))] = datetime.fromisoformat(m.group(2))

    with open(os.path.join(ROOT, "transcripts.json"), encoding="utf-8") as f:
        transcripts = json.load(f)
    return eps, cats, insights, schedule, transcripts


def slugify(s):
    return re.sub(r"^-+|-+$", "", re.sub(r"[^A-Za-z0-9]+", "-", (s or "").strip()))


def slug_map(eps):
    """Same rule the site uses to turn an episode title into its address."""
    used, out = set(), {}
    for ep in eps:
        base = slugify(ep[2]) or f"episode-{ep[0]}"
        slug = base
        if slug.lower() in used:
            slug = f"{base}-{ep[0]}"
        used.add(slug.lower())
        out[ep[0]] = slug
    return out


def num_display(ep):
    return re.sub(r"Episode 0+(\d)", r"Episode \1", ep[1].replace("Ep.", "Episode"))


def full_name(w):
    if not w:
        return ""
    if "," in w:
        return " & ".join(NAMES.get(p.strip(), p.strip()) for p in w.split(","))
    return NAMES.get(w, w)


def parse_ep_date(s):
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s.replace("Sept ", "Sep "), fmt).date()
        except ValueError:
            pass
    return None


def excerpt(text, max_words=EXCERPT_WORDS):
    """First few paragraphs of the transcript, cut at a sentence end.

    Returns (paragraphs, truncated, resume) where resume is the paragraph the
    reader should land on when they click through, counted the same way the
    website splits the transcript (1 = first paragraph).
    """
    raw = text.split("\n\n")
    out, count, resume = [], 0, None
    for idx, p in enumerate(raw, start=1):
        p = p.strip()
        if not p:
            continue
        words = p.split()
        if count + len(words) <= max_words:
            out.append(p)
            count += len(words)
            continue
        resume = idx
        if count < max_words * 0.6:
            room = max_words - count
            cut = " ".join(words[:room])
            ends = [m.end() for m in re.finditer(r"[.!?][\"')\u201d]?(?=\s|$)", cut)]
            if ends and ends[-1] > len(cut) * 0.4:
                out.append(cut[:ends[-1]])
            else:
                out.append(cut.rstrip(",;:") + "\u2026")
        break
    return out, resume is not None, resume or 1


# ── Images ────────────────────────────────────────────────────────────────
def make_images(ep_id):
    """Writes images/email/episode-N-play.jpg and images/email/logo.png."""
    from PIL import Image, ImageDraw

    out_dir = os.path.join(ROOT, "images", "email")
    os.makedirs(out_dir, exist_ok=True)

    logo_out = os.path.join(out_dir, "logo.png")
    if not os.path.exists(logo_out):
        logo = Image.open(os.path.join(ROOT, "images", "logo.png")).convert("RGBA")
        w = 480
        logo.resize((w, round(logo.height * w / logo.width)), Image.LANCZOS).save(logo_out, optimize=True)

    src = os.path.join(ROOT, "images", f"episode-{ep_id}-thumb.jpg")
    if not os.path.exists(src):
        raise SystemExit(f"No thumbnail found for episode {ep_id} at images/episode-{ep_id}-thumb.jpg")
    im = Image.open(src).convert("RGB")
    W = 1200
    im = im.resize((W, round(im.height * W / im.width)), Image.LANCZOS)

    # Draw the play button at 4x size and shrink it, so the edges come out smooth.
    S = 4
    layer = Image.new("RGBA", (im.width * S, im.height * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy, r = layer.width // 2, layer.height // 2, 78 * S
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(15, 45, 73, 205), outline=(255, 255, 255, 240), width=5 * S)
    t = 30 * S
    d.polygon([(cx - t * 0.72, cy - t), (cx - t * 0.72, cy + t), (cx + t * 1.05, cy)], fill=(255, 255, 255, 255))
    layer = layer.resize(im.size, Image.LANCZOS)
    shade = Image.new("RGBA", im.size, (15, 45, 73, 38))
    out = Image.alpha_composite(Image.alpha_composite(im.convert("RGBA"), shade), layer).convert("RGB")
    out.save(os.path.join(out_dir, f"episode-{ep_id}-play.jpg"), quality=86, optimize=True)


def pdf_rel_path(ep_id):
    return f"transcripts/pdf/minhag-of-the-week-episode-{ep_id}.pdf"


def make_pdf(ep_id):
    """Saves the transcript PDF by running the website's own PDF button in a
    hidden browser, so the file is identical to what visitors download."""
    import functools
    import http.server
    import threading
    from playwright.sync_api import sync_playwright

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=ROOT)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = os.path.join(ROOT, pdf_rel_path(ep_id))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(accept_downloads=True)
            page.goto(f"http://127.0.0.1:{srv.server_address[1]}/#ep-{ep_id}", wait_until="domcontentloaded")
            page.wait_for_function(
                "id => typeof TRANSCRIPTS!=='undefined' && TRANSCRIPTS[String(id)] && TRANSCRIPTS[String(id)].text",
                arg=ep_id, timeout=30000)
            with page.expect_download(timeout=60000) as dl:
                page.evaluate("id => printPDF(id)", ep_id)
            dl.value.save_as(out)
            browser.close()
    finally:
        srv.shutdown()
    if os.path.getsize(out) < 5000:
        raise SystemExit("The transcript PDF came out empty.")
    return out


# ── The email ─────────────────────────────────────────────────────────────
def e(s):
    return html.escape(s or "", quote=True)


def button(label, url, primary=True):
    if primary:
        style = (f"background:{PURPLE};color:#ffffff;border:1px solid {PURPLE};font-size:16px;"
                 "padding:14px 34px;")
    else:
        style = (f"background:{PURPLE_LIGHT};color:{PURPLE_DARK};border:1px solid #cdb3e3;font-size:13px;"
                 "padding:9px 18px;")
    return (f'<a href="{e(url)}" target="_blank" style="display:inline-block;{style}'
            f'font-family:{SANS};font-weight:bold;text-decoration:none;border-radius:6px;'
            f'line-height:1.2;">{e(label)}</a>')


def build(ep_id, send_date=None, asset_base=SITE, has_pdf=False):
    eps, cats, insights, schedule, transcripts = load_site()
    ep = next((x for x in eps if x[0] == ep_id), None)
    if not ep:
        raise SystemExit(f"Episode {ep_id} is not on the website.")
    slugs = slug_map(eps)
    now = datetime.now(NY)
    hidden = {i for i, when in schedule.items() if when > now}

    send_date = send_date or now.date()
    released = parse_ep_date(ep[5])
    is_new = bool(released and abs((send_date - released).days) <= NEW_WINDOW_DAYS)
    label = "New Episode" if is_new else "From the Archives"

    title, presenter, dedication = ep[2], full_name(ep[3]), ep[4]
    ep_url = f"{SITE}/{slugs[ep_id]}"
    num = num_display(ep)

    tx = (transcripts.get(str(ep_id)) or {}).get("text") or (ep[10] if len(ep) > 10 else "")
    paras, truncated, resume = excerpt(tx) if tx else ([], False, 1)
    # Opens the episode page already scrolled to where the email left off.
    read_url = f"{SITE}/?ep={ep_id}&read={resume}"

    # Archive emails: the title only. New episodes: "Episode 308: Title".
    subject = title if not is_new else f"{num}: {title}"
    preheader = f"{num} with {presenter}. Watch it now, or read the transcript."

    # Archive emails mark the label with an asterisk and tie it to the air date.
    top_label = "New Episode" if is_new else "*From the Archives"
    aired = ep[5] if is_new else f"*First aired {ep[5]}"

    ded_html = ""
    if dedication:
        ded_html = f"""
          <tr><td style="padding:14px 36px 0 36px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
              <td style="background:{PURPLE_LIGHT};padding:11px 16px;text-align:center;font-family:{SERIF};font-style:italic;font-size:15px;line-height:1.5;color:{PURPLE_DARK};">{e(dedication)}</td>
            </tr></table>
          </td></tr>"""

    tx_html = ""
    pdf_btn = button("Download PDF", f"{SITE}/{pdf_rel_path(ep_id)}", primary=False) if has_pdf else ""
    if paras:
        body = "".join(
            f'<p style="margin:0 0 16px 0;font-family:{SERIF};font-size:17px;line-height:1.7;color:{TEXT};">{e(p)}</p>'
            for p in paras)
        more = ""
        if truncated:
            more = (f'<p style="margin:4px 0 0 0;font-family:{SANS};font-size:15px;font-weight:bold;">'
                    f'<a href="{e(read_url)}" target="_blank" style="color:{PURPLE};text-decoration:underline;">'
                    f'Click here to keep reading</a></p>')
        tx_html = f"""
          <tr><td style="padding:30px 36px 0 36px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {BORDER};border-top:3px solid {PURPLE};border-radius:8px;">
              <tr><td style="padding:14px 22px 12px 22px;border-bottom:1px solid {BORDER};">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
                  <td valign="middle" style="font-family:{SANS};font-size:15px;font-weight:bold;color:{NAVY};">Episode Transcript</td>
                  <td valign="middle" align="right">{pdf_btn}</td>
                </tr></table>
              </td></tr>
              <tr><td style="padding:20px 22px 22px 22px;">{body}{more}</td></tr>
            </table>
          </td></tr>"""

    ins_html = ""
    items = insights.get(ep_id) or []
    if items:
        rows = ""
        for it in items:
            if it["type"] == "siteLink" and it["epId"] in slugs:
                url, cta = f"{SITE}/{slugs[it['epId']]}", "Watch"
            elif it["url"]:
                url = it["url"] if it["url"].startswith("http") else SITE + it["url"]
                cta = {"pdf": "Read the Article", "audio": "Listen"}.get(it["type"], "Open")
            else:
                continue
            rows += f"""<tr>
                <td style="padding:8px 0;font-family:{SANS};font-size:14px;line-height:1.5;color:{TEXT};">{e(it['label'])}</td>
                <td align="right" style="padding:8px 0 8px 14px;white-space:nowrap;">{button(cta, url, primary=False)}</td>
              </tr>"""
        if rows:
            ins_html = f"""
          <tr><td style="padding:22px 36px 0 36px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {BORDER};border-top:3px solid {PURPLE};border-radius:8px;">
              <tr><td style="padding:16px 22px 4px 22px;font-family:{SANS};font-size:15px;font-weight:bold;color:{NAVY};">Further Insights</td></tr>
              <tr><td style="padding:2px 22px 14px 22px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table></td></tr>
            </table>
          </td></tr>"""

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{e(subject)}</title>
<style>
  body{{margin:0;padding:0;background:{CREAM};}}
  img{{-ms-interpolation-mode:bicubic;}}
  @media only screen and (max-width:620px){{
    .wrap{{width:100% !important;}}
    .px{{padding-left:20px !important;padding-right:20px !important;}}
    .title{{font-size:25px !important;}}
    .hero-name{{font-size:29px !important;}}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:{CREAM};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;font-size:1px;line-height:1px;color:{CREAM};">{e(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{CREAM};">
<tr><td align="center" style="padding:24px 10px;">

  <table role="presentation" class="wrap" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background:#ffffff;border:1px solid {BORDER};border-radius:10px;overflow:hidden;">

    <tr><td align="center" style="padding:18px 20px 16px 20px;border-bottom:3px solid {PURPLE};background:#ffffff;">
      <a href="{SITE}" target="_blank"><img src="{asset_base}/images/email/logo.png" width="170" alt="Sephardic Community Alliance" style="display:block;width:170px;height:auto;border:0;"></a>
    </td></tr>

    <tr><td align="center" bgcolor="{NAVY_DARK}" style="padding:30px 24px 28px 24px;background:{NAVY_DARK};background-image:linear-gradient(135deg,{NAVY_DARK} 0%,#2a2a6a 55%,#3b2a6e 100%);">
      <div class="hero-name" style="font-family:{SERIF};font-style:italic;font-weight:bold;font-size:34px;line-height:1.15;color:#c9a3e6;">Minhag of the Week</div>
      <div style="font-family:{SERIF};font-style:italic;font-size:14px;letter-spacing:.5px;color:#d9c7ec;padding-top:8px;">Preserving Our Rich Heritage, One Minhag at a Time</div>
    </td></tr>

    <tr><td>
      <table role="presentation" class="pad" width="100%" cellpadding="0" cellspacing="0" border="0">

        <tr><td align="center" style="padding:30px 36px 0 36px;text-align:center;">
          <div style="font-family:{SANS};font-size:12px;font-weight:bold;letter-spacing:1.5px;text-transform:uppercase;color:{PURPLE};">{e(top_label)}</div>
          <div style="padding-top:6px;font-family:{SANS};font-size:15px;color:{MUTED};">{e(num)}</div>
          <a href="{e(ep_url)}" target="_blank" class="title" style="display:block;padding-top:10px;font-family:{SANS};font-size:28px;font-weight:bold;line-height:1.2;color:{NAVY};text-decoration:none;">{e(title)}</a>
          <div style="padding-top:12px;font-family:{SANS};font-size:16px;line-height:1.5;color:{TEXT};">By {e(presenter)}</div>
          <div style="padding-top:2px;font-family:{SANS};font-size:13px;line-height:1.5;color:{MUTED};">{e(aired)}</div>
        </td></tr>
        {ded_html}
        <tr><td style="padding:20px 36px 0 36px;">
          <a href="{e(ep_url)}" target="_blank"><img src="{asset_base}/images/email/episode-{ep_id}-play.jpg" width="528" alt="Watch: {e(title)}" style="display:block;width:100%;max-width:528px;height:auto;border:0;border-radius:8px;"></a>
        </td></tr>

        {tx_html}
        {ins_html}
        <tr><td style="padding:34px 36px 0 36px;font-size:0;line-height:0;">&nbsp;</td></tr>
      </table>
    </td></tr>

    <tr><td align="center" bgcolor="{NAVY_DARK}" style="background:{NAVY_DARK};padding:28px 30px 26px 30px;">
      <div style="font-family:{SANS};font-size:14px;line-height:2.1;">
        <a href="{SITE}" target="_blank" style="color:#ffffff;font-weight:bold;text-decoration:underline;">Watch Previous Episodes</a><br>
        <a href="{SITE}/question" target="_blank" style="color:#ffffff;font-weight:bold;text-decoration:underline;">Suggest a Minhag Topic for Future Episodes</a><br>
        <a href="{SITE}/sponsorship" target="_blank" style="color:#ffffff;font-weight:bold;text-decoration:underline;">Sponsor a Future Episode</a><br>
        <a href="{SUBSCRIBE_URL}" target="_blank" style="color:#ffffff;font-weight:bold;text-decoration:underline;">Subscribe to Minhag of the Week Emails</a>
      </div>
      <div style="padding-top:16px;font-family:{SANS};font-size:13px;line-height:1.6;color:#b9c6d8;">Minhag of the Week is a project of the Sephardic Community Alliance.</div>
      <div style="padding-top:16px;font-family:{SANS};font-size:11px;line-height:1.6;color:#8fa1b8;">*|LIST:ADDRESSLINE|*<br>
        <a href="*|UNSUB|*" style="color:#8fa1b8;text-decoration:underline;">Unsubscribe</a> &nbsp;&middot;&nbsp; <a href="*|UPDATE_PROFILE|*" style="color:#8fa1b8;text-decoration:underline;">Update preferences</a> &nbsp;&middot;&nbsp; <a href="*|ARCHIVE|*" style="color:#8fa1b8;text-decoration:underline;">View in browser</a></div>
    </td></tr>

  </table>

</td></tr>
</table>
</body>
</html>
"""
    # Narrower side margins on phones (only on the outer cells, not nested ones).
    page = re.sub(r'<td( align="center")? style="padding:(\d+px) 36px 0 36px;',
                  r'<td\1 class="px" style="padding:\2 36px 0 36px;', page)
    return {"html": page, "subject": subject, "preheader": preheader, "label": label,
            "title": title, "episode_url": ep_url, "is_new": is_new}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("episode", type=int)
    ap.add_argument("--out", required=True)
    ap.add_argument("--send-date", help="YYYY-MM-DD; decides New Episode vs From the Archives")
    ap.add_argument("--asset-base", default=SITE)
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--no-pdf", action="store_true")
    a = ap.parse_args()
    if not a.no_images:
        make_images(a.episode)
    sd = datetime.strptime(a.send_date, "%Y-%m-%d").date() if a.send_date else None
    has_pdf = False
    if not a.no_pdf:
        try:
            make_pdf(a.episode)
            has_pdf = True
        except Exception as err:  # the email still goes out, just without the PDF button
            print(f"PDF step skipped: {err}", file=sys.stderr)
    r = build(a.episode, send_date=sd, asset_base=a.asset_base.rstrip("/"), has_pdf=has_pdf)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(r["html"])
    print(json.dumps({k: v for k, v in r.items() if k != "html"}, indent=2))
    print(f"{len(r['html'].encode('utf-8'))} bytes")


if __name__ == "__main__":
    main()
