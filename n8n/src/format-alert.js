// Turns a CiteDesk event into a Telegram message for the support team.
// Events: "handoff" (the bot could not or should not answer) and "contact" (the customer left an e-mail).
const cfg = $('Config').first().json;
const request = $('CiteDesk event').first().json;
const body = request.body || {};
const headers = request.headers || {};

// Reject requests that don't carry the shared secret (when one is configured).
if (cfg.sharedSecret && headers['x-citedesk-secret'] !== cfg.sharedSecret) {
  return [];
}

const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const cut = (s, n) => {
  const t = String(s ?? '').trim();
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
};
const line = (label, value, n = 300) => (value ? `<b>${label}:</b> ${esc(cut(value, n))}\n` : '');
const company = body.company || 'CiteDesk';
const footer = [body.page_url ? `🔗 ${esc(cut(body.page_url, 200))}` : '', body.session_id ? `<i>session ${esc(cut(body.session_id, 40))}</i>` : '']
  .filter(Boolean)
  .join('\n');

let message;
if (body.event === 'handoff') {
  const why = { not_found: 'not covered by the help docs', handoff: 'needs a person' }[body.status] || body.status;
  const sources = Array.isArray(body.sources) && body.sources.length
    ? `<b>Sources shown:</b>\n${body.sources.slice(0, 4).map((s) => `• ${esc(cut(s, 120))}`).join('\n')}\n`
    : '';
  message =
    `🙋 <b>${esc(company)}: a customer needs a human</b>\n\n` +
    line('Question', body.question, 600) +
    line('Why', body.reason ? `${body.reason} (${why})` : why, 300) +
    '\n' +
    line('The bot replied', body.answer, 800) +
    sources;
} else if (body.event === 'contact') {
  message =
    `📬 <b>${esc(company)}: a customer left their e-mail</b>\n\n` +
    line('E-mail', body.email, 254) +
    line('Name', body.name, 100) +
    line('About', body.question, 600) +
    line('Message', body.message, 1500);
} else {
  message = `⚠️ <b>Unknown CiteDesk event</b>\n<pre>${esc(cut(JSON.stringify(body), 1500))}</pre>`;
}

message = (message + (footer ? `\n${footer}` : '')).trim();
return [{ json: { chatId: cfg.telegramChatId, message: cut(message, 4000), event: body.event || 'unknown' } }];
