// Pulls transcripts from the 2 consolidated, published-to-web Google Docs
// and writes /transcripts.json in the repo. Runs on a schedule via GitHub
// Actions (see .github/workflows/sync-transcripts.yml) and can be run
// manually with: node scripts/sync_transcripts.js
//
// No gating: whatever is in the Docs goes live. Review happens upstream
// (in the Docs themselves) before content is typed in.

const fs = require('fs');
const path = require('path');

const DOC_URLS = [
  'https://docs.google.com/document/d/e/2PACX-1vSRWCoCmr6dM8DOAyBrXHooI0SEcGvyKnGuwomtHw7rpzgqYg7xhlBwbnKUwk1HwIQ7y7NIEpmQA4f6/pub',
  'https://docs.google.com/document/d/e/2PACX-1vT7YctTWqwb0erhBGkF9m3h3au-hRtLZZRmtE7WZxL7fBUldwg9Slhu-AIAkTy8kraSrDc4W8wy29uk/pub',
];

const OUTPUT_PATH = path.join(__dirname, '..', 'transcripts.json');

const DISCLAIMER = "This is a transcript of a video episode and may contain spelling or grammatical errors. As this transcript was generated from video, it may not fully capture the speaker's tone, emphasis, or intent — we recommend watching the video for the full experience and accuracy.";
const COURTESY = "Courtesy of Minhagoftheweek.com";

function decodeEntities(str) {
  return str
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>');
}

function htmlToText(html) {
  return decodeEntities(
    html
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/<\/p>/gi, '\n\n')
      .replace(/<\/div>/gi, '\n')
      .replace(/<[^>]+>/g, '')
  ).trim();
}

async function fetchDoc(url) {
  const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
  if (!res.ok) throw new Error(`Fetch failed (${res.status}): ${url}`);
  return res.text();
}

// Google Docs' "Publish to web" output does NOT use semantic <h1>-<h3> tags;
// headings are plain <p> tags styled to look like headings via CSS classes.
// So instead of matching on tag name, we scan the flattened plain text for
// lines that start with "Episode <number>" (the "## Episode N — Title"
// convention used when writing the Docs), and split on those.
function parseEpisodes(html) {
  const episodes = {};

  const body = html
    .replace(/<style[\s\S]*?<\/style>/gi, '')
    .replace(/<script[\s\S]*?<\/script>/gi, '');

  const text = htmlToText(body);
  const lines = text.split('\n').map(l => l.trim());

  // Matches lines like: "Episode 1 — Shabu'ot, Torah Study and Dairy"
  // (with —, -, or : as separator, optionally preceded by "## ")
  const headingLineRe = /^(?:#+\s*)?Episode\s+(\d+)\s*(?:—|-|:)\s*(.+)$/i;

  const headingIdxs = [];
  lines.forEach((line, idx) => {
    const m = line.match(headingLineRe);
    if (m) headingIdxs.push({ idx, num: m[1], title: m[2].trim() });
  });

  for (let i = 0; i < headingIdxs.length; i++) {
    const { idx, num, title } = headingIdxs[i];
    const endIdx = i + 1 < headingIdxs.length ? headingIdxs[i + 1].idx : lines.length;
    const bodyLines = lines
      .slice(idx + 1, endIdx)
      .filter(l => l && !/^Audio:/i.test(l));
    const bodyText = bodyLines.join('\n\n').replace(/\n{3,}/g, '\n\n').trim();

    episodes[num] = {
      title,
      text: bodyText,
      disclaimer: DISCLAIMER,
      courtesy: COURTESY,
      syncedAt: new Date().toISOString(),
    };
  }

  return episodes;
}

async function main() {
  const all = {};
  for (const url of DOC_URLS) {
    console.log('Fetching', url);
    const html = await fetchDoc(url);
    const episodes = parseEpisodes(html);
    Object.assign(all, episodes);
    console.log(`  -> ${Object.keys(episodes).length} episodes parsed`);
  }

  fs.writeFileSync(OUTPUT_PATH, JSON.stringify(all, null, 2));
  console.log(`Wrote ${Object.keys(all).length} episodes to ${OUTPUT_PATH}`);
}

main().catch(err => { console.error(err); process.exit(1); });
