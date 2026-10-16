"""Keeps the Instagram token alive forever.

Instagram tokens expire after 60 days unless refreshed. This runs before every
view-count update: it picks a working token (the auto-refreshed one stored
encrypted in data/ig-token.enc, or the INSTAGRAM_ACCESS_TOKEN secret as a
fallback), refreshes it about once a week, saves the refreshed token encrypted
(key = an existing repo secret, never committed), and hands it to the next step.
If no token works, it exits 1 so the workflow opens a GitHub issue (email alert).
"""
import json, os, subprocess, sys, time, urllib.request, urllib.parse

ENC = "data/ig-token.enc"
STATE = "data/ig-token-state.json"
KEY = os.environ.get("TOKEN_KEY", "")
REFRESH_EVERY = 7 * 86400

def openssl(args, data):
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-a", "-A", *args, "-pass", "env:TOKEN_KEY"],
                       input=data.encode(), capture_output=True)
    return r.stdout.decode().strip() if r.returncode == 0 else None

def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)

def works(tok):
    try:
        return "id" in get("https://graph.instagram.com/me?fields=id&access_token=" + urllib.parse.quote(tok))
    except Exception:
        return False

def main():
    candidates = []
    if KEY and os.path.exists(ENC):
        dec = openssl(["-d"], open(ENC).read().strip())
        if dec:
            candidates.append(("stored", dec))
    if os.environ.get("INSTAGRAM_ACCESS_TOKEN"):
        candidates.append(("secret", os.environ["INSTAGRAM_ACCESS_TOKEN"]))
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}
    tok = src = None
    for name, t in candidates:
        if works(t):
            tok, src = t, name
            break
    if not tok:
        print("No working Instagram token (stored or secret).", file=sys.stderr)
        state["status"] = "EXPIRED - generate a new token and update the INSTAGRAM_ACCESS_TOKEN secret"
        state["checked_at"] = int(time.time())
        json.dump(state, open(STATE, "w"), indent=1)
        sys.exit(1)
    if src == "secret" or time.time() - state.get("refreshed_at", 0) > REFRESH_EVERY:
        try:
            r = get("https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token="
                    + urllib.parse.quote(tok))
            if r.get("access_token"):
                tok = r["access_token"]
                state["refreshed_at"] = int(time.time())
                state["expires_at"] = int(time.time()) + int(r.get("expires_in", 0))
                if KEY:
                    enc = openssl([], tok)
                    if enc:
                        open(ENC, "w").write(enc + "\n")
                print("Instagram token refreshed.")
        except Exception as e:
            print(f"Refresh failed (token still valid, will retry next run): {e}", file=sys.stderr)
    state["status"] = "OK"
    state["source"] = src
    state["checked_at"] = int(time.time())
    json.dump(state, open(STATE, "w"), indent=1)
    print(f"::add-mask::{tok}")
    with open(os.environ["GITHUB_ENV"], "a") as f:
        f.write(f"INSTAGRAM_ACCESS_TOKEN={tok}\n")

main()
