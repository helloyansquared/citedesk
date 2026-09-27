// Reads the /reindex result (or the error) and writes a short report.
const cfg = $('Config').first().json;
const result = $input.first().json || {};
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const list = (items) => (items && items.length ? items.map(esc).join(', ') : '–');

let message;
let changed = false;
if (result.error || result.ok !== true) {
  const detail = result.error?.message || result.error?.description || result.detail || JSON.stringify(result).slice(0, 300);
  message = `⚠️ <b>CiteDesk re-index failed</b>\n${esc(detail)}`;
  changed = true;
} else {
  const failed = Object.entries(result.failed || {});
  changed = result.added.length + result.changed.length + result.removed.length + failed.length > 0;
  message =
    `🔄 <b>CiteDesk knowledge base refreshed</b>\n` +
    `${result.sources} documents, ${result.chunks} passages (${result.embedded} re-embedded) in ${result.seconds}s\n\n` +
    `<b>Changed:</b> ${list(result.changed)}\n` +
    `<b>Added:</b> ${list(result.added)}\n` +
    `<b>Removed:</b> ${list(result.removed)}` +
    (failed.length ? `\n<b>Could not read:</b>\n${failed.map(([k, v]) => `• ${esc(k)}: ${esc(v)}`).join('\n')}` : '');
}

if (cfg.onlyReportChanges && !changed) {
  return []; // nothing new: stay quiet
}
return [{ json: { chatId: cfg.telegramChatId, message, changed } }];
