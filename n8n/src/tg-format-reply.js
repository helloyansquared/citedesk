// Formats CiteDesk's answer for Telegram (HTML) and remembers the exchange for follow-up questions.
const q = $('Build question').first().json;
const a = $input.first().json || {};
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

let message;
if (a.error || typeof a.answer !== 'string') {
  message = "Sorry, I can't answer right now. Please try again in a few minutes.";
} else {
  // **bold** -> <b>, [1] -> ¹ style markers stay readable in a chat
  let text = esc(a.answer).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  const sources = (a.citations || [])
    .map((c) => `[${c.n}] ${c.url && /^https?:/.test(c.url) ? `<a href="${esc(c.url)}">${esc(c.location)}</a>` : esc(c.location)}`)
    .join('\n');
  message = text + (sources ? `\n\n<i>Sources</i>\n${sources}` : '');
  if (a.needs_human) {
    message += '\n\n🙋 I\'ve passed your question to a teammate. They will reply here.';
  }

  const memory = $getWorkflowStaticData('global');
  memory.chats = memory.chats || {};
  const history = memory.chats[q.chatId] || [];
  history.push({ role: 'user', content: q.question }, { role: 'assistant', content: a.answer.slice(0, 2000) });
  memory.chats[q.chatId] = history.slice(-6);
}

return [{ json: { chatId: q.chatId, message: message.slice(0, 4000) } }];
