#!/usr/bin/env python3
"""
Structural validator for index.html's embedded data (EPS, THUMBS, SCHEDULE,
INSIGHTS). Two jobs:

  1. Sanity-check a single file: every EPS episode number is unique, every
     EPS episode has a THUMBS entry, every INSIGHTS siteLink epId points at
     a real episode, build-version is present and numeric.

  2. If given a --baseline (a previous version of index.html, e.g. the
     prior commit), compare the two: EPS/THUMBS/INSIGHTS keys may only be
     ADDED between baseline and current, never removed. This is the check
     that would have caught the 2026-09-09 incident (an edit that dropped
     an existing EPS entry) regardless of how the edit was made — by a
     script, by hand, or via the GitHub API directly (which bypasses git
     hooks entirely, which is exactly how a bad edit landed undetected
     this time).

Exit code 0 = valid, 1 = invalid (prints reasons to stderr).

Usage:
  python3 scripts/validate_index.py index.html
  python3 scripts/validate_index.py index.html --baseline /tmp/old_index.html
"""

import argparse
import re
import sys


def extract_balanced(text, start_idx):
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


def top_level_keys_array(array_text):
    """For an EPS-style array of arrays: [[306, ...], [305, ...], ...],
    return the leading number of each top-level element, in order."""
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
    return numbers


def top_level_keys_object(obj_text):
    """For a THUMBS/INSIGHTS/SCHEDULE-style object: {key: value, key: ...},
    return the top-level keys in order (quoted or bare numeric keys)."""
    keys = []
    depth = 0
    in_str = False
    quote = None
    escape = False
    i = 0
    n = len(obj_text)
    expect_key = True
    while i < n:
        c = obj_text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == quote:
                in_str = False
            i += 1
            continue
        if c in ("'", '"') and depth == 1 and expect_key:
            # quoted key
            j = i + 1
            while j < n and obj_text[j] != c:
                if obj_text[j] == "\\":
                    j += 1
                j += 1
            keys.append(obj_text[i + 1:j])
            i = j + 1
            expect_key = False
            continue
        if c in ("'", '"'):
            in_str = True
            quote = c
            i += 1
            continue
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            continue
        if depth == 1 and expect_key:
            m = re.match(r"\s*(\d+)\s*:", obj_text[i:])
            if m:
                keys.append(m.group(1))
                i += m.end() - 1
                expect_key = False
                continue
        if c == ":" and depth == 1:
            expect_key = False
        if c == "," and depth == 1:
            expect_key = True
        i += 1
    return keys


def parse_all(html):
    result = {}
    for name, kind in [("EPS", "array"), ("THUMBS", "object"),
                        ("SCHEDULE", "object"), ("INSIGHTS", "object")]:
        marker = f"const {name}="
        if marker not in html:
            result[name] = None
            continue
        idx = html.index(marker) + len(marker)
        open_ch = "[" if kind == "array" else "{"
        bracket_start = html.index(open_ch, idx)
        text, _ = extract_balanced(html, bracket_start)
        if kind == "array":
            result[name] = top_level_keys_array(text)
        else:
            result[name] = top_level_keys_object(text)
    return result


def validate_single(html, errors):
    data = parse_all(html)
    eps = data.get("EPS")
    if eps is None:
        errors.append("const EPS= not found")
        return data
    if len(eps) != len(set(eps)):
        dupes = {x for x in eps if eps.count(x) > 1}
        errors.append(f"Duplicate EPS episode numbers: {sorted(dupes)}")

    thumbs = data.get("THUMBS") or []
    missing_thumbs = [n for n in eps if n not in thumbs]
    if missing_thumbs:
        errors.append(f"Episodes in EPS with no THUMBS entry: {missing_thumbs}")

    insights = data.get("INSIGHTS")
    if insights is not None:
        idx = html.index("const INSIGHTS=")
        bracket_start = html.index("{", idx)
        insights_text, _ = extract_balanced(html, bracket_start)
        for m in re.finditer(r"type:'siteLink',epId:(\d+)", insights_text):
            ep_id = m.group(1)
            if ep_id not in eps:
                errors.append(f"INSIGHTS references epId {ep_id} which is not in EPS")

    if not re.search(r'<meta name="build-version" content="\d+">', html):
        errors.append("Missing or non-numeric build-version meta tag")

    return data


def validate_against_baseline(current_data, baseline_data, errors):
    for name in ("EPS", "THUMBS", "INSIGHTS"):
        cur = set(current_data.get(name) or [])
        base = set(baseline_data.get(name) or [])
        removed = base - cur
        if removed:
            errors.append(
                f"{name} lost {len(removed)} key(s) versus baseline: {sorted(removed)} "
                "— an edit must never remove existing entries from this file."
            )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("index_html_path")
    p.add_argument("--baseline", default=None, help="Path to a previous version of index.html to compare against")
    args = p.parse_args()

    with open(args.index_html_path, "r", encoding="utf-8") as f:
        html = f.read()

    errors = []
    current_data = validate_single(html, errors)

    if args.baseline:
        with open(args.baseline, "r", encoding="utf-8") as f:
            baseline_html = f.read()
        baseline_errors = []
        baseline_data = validate_single(baseline_html, baseline_errors)
        validate_against_baseline(current_data, baseline_data, errors)

    if errors:
        print("INDEX.HTML VALIDATION FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    print("index.html validation passed.")
    if current_data.get("EPS") is not None:
        print(f"  EPS: {len(current_data['EPS'])} episodes")
    sys.exit(0)


if __name__ == "__main__":
    main()
