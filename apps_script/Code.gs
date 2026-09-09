/**
 * Watches the Minhag of the Week sheet for a green-highlighted episode row
 * and triggers the GitHub publish automation (video pull, thumbnail,
 * categories, scheduling, link-preview page).
 *
 * SETUP (one time):
 * 1. Open the sheet -> Extensions -> Apps Script.
 * 2. Delete any placeholder code, paste this whole file in.
 * 3. Click the gear icon (Project Settings) -> Script Properties ->
 *    Add property: GITHUB_TOKEN = <your GitHub token>
 * 4. Run the `setupTrigger` function once (select it in the dropdown at
 *    top, click Run). Approve the permissions Google asks for.
 * 5. Done — it now checks every 15 minutes automatically.
 *
 * HOW IT WORKS:
 * - Looks at the sheet tab named for the current year (e.g. "2026").
 * - For each row, if the Episode # cell (column B) is highlighted green
 *   AND Topic/Presenter are filled in AND it hasn't been triggered yet
 *   (tracked in column J), it fires a GitHub repository_dispatch event
 *   and marks column J so it's never triggered twice.
 * - The Website Link (column K) is intentionally NOT written at trigger
 *   time. The GitHub Action + Cloudflare Pages deploy take roughly a
 *   minute to actually put the per-episode preview page live — writing
 *   the link immediately would let it be copied and shared before the
 *   page exists, which gets a 404/generic preview permanently cached by
 *   iMessage/WhatsApp/etc. for that URL. Instead, every run checks any
 *   already-triggered row whose link is still blank, fetches the
 *   expected URL, and only fills in the link once that page actually
 *   returns 200 — so the link never appears until it's safe to share.
 */

const REPO_OWNER = 'Minhagoftheweek';
const REPO_NAME = 'minhag-site';

// Column indices (0-based, matches the sheet layout):
// A=Date(0) B=Episode#(1) C=Dedication(2) D=Topic(3) E=Presenter(4)
// F=Video#(5) G=SentToVictor(6) H=Edited(7) I=Scheduled(8) J=AutomationStatus(9) K=DirectLink(10)
const COL_DATE = 0;
const COL_EPISODE = 1;
const COL_DEDICATION = 2;
const COL_TOPIC = 3;
const COL_PRESENTER = 4;
const COL_STATUS = 9; // column J
const COL_LINK = 10; // column K

// Mirrors slugify_title() in scripts/publish_episode.py exactly.
function slugifyTitle(topic) {
  const cleaned = topic.replace(/[^\w\s-]/g, '');
  return cleaned.trim().replace(/\s+/g, '-');
}

function checkForNewEpisodes() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const yearName = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy');
  const sheet = ss.getSheetByName(yearName);
  if (!sheet) {
    console.log('No sheet tab found for year ' + yearName);
    return;
  }

  const range = sheet.getDataRange();
  const values = range.getValues();
  const backgrounds = range.getBackgrounds();
  const numRows = values.length;

  for (let r = 1; r < numRows; r++) { // skip header row
    const episodeBg = backgrounds[r][COL_EPISODE];
    const episodeNum = values[r][COL_EPISODE];
    const status = values[r][COL_STATUS];
    const topic = values[r][COL_TOPIC];
    const presenter = values[r][COL_PRESENTER];
    const dedication = values[r][COL_DEDICATION];
    const link = values[r][COL_LINK];

    // Already triggered, just waiting on the live page — check it, and
    // write the link in only once it's actually reachable. Runs on every
    // 15-minute pass until it succeeds, then leaves it alone forever.
    if (status && String(status).indexOf('Triggered') === 0 && !link) {
      if (topic) tryWriteLinkIfLive(sheet, r, topic);
      continue;
    }

    if (!episodeNum || status) continue; // no episode # yet, or already triggered
    if (!isGreenish(episodeBg)) continue; // not marked ready

    const rowDate = new Date(values[r][COL_DATE]);
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - 3);
    if (!isNaN(rowDate) && rowDate < cutoff) continue; // old row — ignore even if green

    if (!topic || !presenter) {
      console.log('Row ' + (r + 1) + ' is green but missing Topic/Presenter — skipping for now.');
      continue;
    }

    const ok = triggerPublish(episodeNum, topic, presenter, dedication);
    const cell = sheet.getRange(r + 1, COL_STATUS + 1);
    if (ok) {
      cell.setValue('Triggered ' + new Date().toLocaleString());
      // Link column left blank on purpose — see tryWriteLinkIfLive above.
    } else {
      cell.setValue('ERROR — check GitHub Actions');
    }
  }
}

/** Checks whether the episode's live preview page is up yet; if so, writes
  * the link into column K. If not, does nothing — it gets checked again
  * automatically on the next 15-minute run. */
function tryWriteLinkIfLive(sheet, r, topic) {
  const url = 'https://minhagoftheweek.com/' + slugifyTitle(topic);
  try {
    const resp = UrlFetchApp.fetch(url, { muteHttpExceptions: true, followRedirects: true });
    if (resp.getResponseCode() === 200) {
      sheet.getRange(r + 1, COL_LINK + 1).setValue(url);
    }
  } catch (e) {
    console.log('Live-check failed for ' + url + ': ' + e);
  }
}

function isGreenish(hex) {
  if (!hex || hex.charAt(0) !== '#' || hex.length !== 7) return false;
  const r = parseInt(hex.substr(1, 2), 16);
  const g = parseInt(hex.substr(3, 2), 16);
  const b = parseInt(hex.substr(5, 2), 16);
  return g > 140 && g > r + 25 && g > b + 25;
}

function triggerPublish(episodeNum, topic, presenter, dedication) {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    console.log('GITHUB_TOKEN script property is not set.');
    return false;
  }

  const url = 'https://api.github.com/repos/' + REPO_OWNER + '/' + REPO_NAME + '/dispatches';
  const payload = {
    event_type: 'publish_episode',
    client_payload: {
      episode_num: String(episodeNum),
      topic: String(topic),
      presenter: String(presenter),
      dedication: dedication ? String(dedication) : ''
    }
  };

  const options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'token ' + token,
      Accept: 'application/vnd.github+json'
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };

  const resp = UrlFetchApp.fetch(url, options);
  const code = resp.getResponseCode();
  if (code !== 204) {
    console.log('GitHub dispatch failed (' + code + '): ' + resp.getContentText());
  }
  return code === 204;
}

/** Run this once manually to set up the recurring 15-minute check. */
function setupTrigger() {
  // Clear any existing triggers for this function first, so re-running is safe.
  ScriptTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'checkForNewEpisodes') {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger('checkForNewEpisodes')
    .timeBased()
    .everyMinutes(15)
    .create();
  console.log('Trigger installed — checking every 15 minutes.');
}

function ScriptTriggers() {
  return ScriptApp.getProjectTriggers();
}
