/**
 * "Approve" button in the weekly email's test copy.
 * Lives in the tracking sheet's Apps Script project, deployed as a Web app
 * (Execute as: Me, Who has access: Anyone). The deployed URL is stored in the
 * GitHub secret APPROVE_URL (kept out of this public repo).
 *
 * The email link opens a page with one button. Pressing it changes that row's
 * column F ("Ready for mailchimp?") from Yes to Approved and runs the usual
 * sheet check, so GitHub schedules the email right away. (The extra press stops
 * email link-scanners from approving by just opening the link.)
 * It only flips a row that says Yes, for a date in the next 8 days.
 */

function doGet(e) {
  const p = e.parameter || {};
  const ep = String(p.ep || '').replace(/[^0-9.]/g, '');
  const date = String(p.date || '').replace(/[^0-9-]/g, '');
  const msg = approveCheck_(ep, date, false);
  if (msg) return page_(msg, '');
  const btn =
    '<button id="b" onclick="this.disabled=true;this.textContent=\'Approving...\';' +
    'google.script.run.withSuccessHandler(function(m){document.getElementById(\'m\').textContent=m;' +
    'document.getElementById(\'b\').style.display=\'none\';}).approveEpisode(\'' + ep + '\',\'' + date + '\')" ' +
    'style="font-size:22px;font-weight:bold;color:#fff;background:#1e8e3e;border:0;border-radius:8px;padding:16px 40px;">' +
    'Approve Episode ' + ep + '</button>';
  return page_('Send Episode ' + ep + ' to the list on Wednesday at 1:00 PM?', btn);
}

function approveEpisode(ep, date) {
  return approveCheck_(String(ep), String(date), true);
}

// Returns a message to show, or '' when the row is waiting for approval (and doIt is false).
function approveCheck_(ep, date, doIt) {
  const m = date.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!ep || !m) return 'This link is missing the episode or date.';
  const wantDate = Number(m[2]) + '/' + Number(m[3]) + '/' + m[1];
  const days = (new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) - new Date()) / 86400000;
  if (days < -1 || days > 8) return 'This approval link has expired.';
  const sheet = SpreadsheetApp.getActive().getSheetByName(m[1]);
  if (!sheet) return 'Could not find the ' + m[1] + ' tab on the tracking sheet.';
  const rows = sheet.getDataRange().getDisplayValues();
  for (let r = 0; r < rows.length; r++) {
    const num = (String(rows[r][1]).match(/\d+(\.\d+)?/) || [''])[0];
    if (rows[r][0].trim() !== wantDate || num !== ep) continue;
    const f = String(rows[r][5]).trim().toLowerCase();
    if (f.indexOf('approv') === 0) return 'Episode ' + ep + ' is already approved. It is scheduled for Wednesday at 1:00 PM.';
    if (f !== 'yes') return 'Episode ' + ep + ' is not waiting for approval.';
    if (!doIt) return '';
    sheet.getRange(r + 1, 6).setValue('Approved');
    SpreadsheetApp.flush();
    try { if (typeof checkMailchimpReady === 'function') checkMailchimpReady(); } catch (err) {}
    return 'Approved. Episode ' + ep + ' will go out Wednesday at 1:00 PM. You can close this page.';
  }
  return 'Could not find episode ' + ep + ' for ' + wantDate + ' on the tracking sheet.';
}

function page_(msg, extra) {
  return HtmlService.createHtmlOutput(

    '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;text-align:center;padding:48px 24px;color:#184878;">' +
    '<div id="m" style="font-size:20px;padding-bottom:28px;">' + msg + '</div>' + extra + '</div>'
  ).setTitle('Minhag of the Week').addMetaTag('viewport', 'width=device-width, initial-scale=1');
}
