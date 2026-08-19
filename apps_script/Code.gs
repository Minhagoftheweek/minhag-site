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
 *   (tracked in column J), it fires a GitHub repository_dispatch event.
 * - Marks column J with a timestamp so it's never triggered twice.
 */

const REPO_OWNER = 'Minhagoftheweek';
const REPO_NAME = 'minhag-site';

// Column indices (0-based, matches the sheet layout):
// A=Date(0) B=Episode#(1) C=Dedication(2) D=Topic(3) E=Presenter(4)
// F=Video#(5) G=SentToVictor(6) H=Edited(7) I=Scheduled(8) J=AutomationStatus(9)
const COL_EPISODE = 1;
const COL_DEDICATION = 2;
const COL_TOPIC = 3;
const COL_PRESENTER = 4;
const COL_STATUS = 9; // column J

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

    if (!episodeNum || status) continue; // no episode # yet, or already triggered
    if (!isGreenish(episodeBg)) continue; // not marked ready
    if (!topic || !presenter) {
      console.log('Row ' + (r + 1) + ' is green but missing Topic/Presenter — skipping for now.');
      continue;
    }

    const ok = triggerPublish(episodeNum, topic, presenter, dedication);
    const cell = sheet.getRange(r + 1, COL_STATUS + 1);
    if (ok) {
      cell.setValue('Triggered ' + new Date().toLocaleString());
    } else {
      cell.setValue('ERROR — check GitHub Actions');
    }
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
