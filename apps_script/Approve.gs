/**
 * "Approve" button in the weekly email's test copy.
 * Lives in the tracking sheet's Apps Script project ("Minhag"), deployed as a
 * Web app (Execute as: Me, Who has access: Anyone). The deployed URL plus
 * ?key=... is stored in the GitHub secret APPROVE_URL.
 *
 * Clicking Approve changes that row's column F ("Ready for mailchimp?") from
 * Yes to Approved, then runs the usual sheet check so GitHub schedules the
 * email right away.
 *
 * The real key is only in the Apps Script copy, never in this public repo.
 */
const APPROVE_KEY = 'PUT-KEY-HERE';

function doGet(e) {
  const p = e.parameter || {};
  if (p.key !== APPROVE_KEY) return page_('This link is not valid.');
  const ep = String(p.ep || '').trim();
  const m = String(p.date || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!ep || !m) return page_('This link is missing the episode or date.');
  const wantDate = Number(m[2]) + '/' + Number(m[3]) + '/' + m[1];

  const sheet = SpreadsheetApp.getActive().getSheetByName(m[1]);
  if (!sheet) return page_('Could not find the ' + m[1] + ' tab on the tracking sheet.');
  const rows = sheet.getDataRange().getDisplayValues();
  for (let r = 0; r < rows.length; r++) {
    const num = (String(rows[r][1]).match(/\d+(\.\d+)?/) || [''])[0];
    if (rows[r][0].trim() !== wantDate || num !== ep) continue;
    const f = String(rows[r][5]).trim().toLowerCase();
    if (f.indexOf('approv') === 0) return page_('Episode ' + ep + ' was already approved. It is scheduled for Wednesday at 1:00 PM.');
    sheet.getRange(r + 1, 6).setValue('Approved');
    SpreadsheetApp.flush();
    try { if (typeof checkMailchimpReady === 'function') checkMailchimpReady(); } catch (err) {}
    return page_('Approved. Episode ' + ep + ' will be scheduled to send Wednesday at 1:00 PM. You can close this page.');
  }
  return page_('Could not find episode ' + ep + ' for ' + wantDate + ' on the tracking sheet.');
}

function page_(msg) {
  return HtmlService.createHtmlOutput(
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:20px;padding:40px 24px;text-align:center;color:#184878;">' +
    msg + '</div>').setTitle('Minhag of the Week');
}
