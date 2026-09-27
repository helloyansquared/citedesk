// Turns an incoming Telegram message into a CiteDesk question, with the chat's recent history.
const cfg = $('Config').first().json;
const msg = $('Telegram message').first().json.message;
if (!msg || typeof msg.text !== 'string' || !msg.text.trim()) {
  return []; // stickers, photos, joins...: nothing to answer
}
let question = msg.text.trim();
if (question.startsWith('/start')) question = 'Hi!';
question = question.slice(0, 1000);

const chatId = String(msg.chat.id);
const memory = $getWorkflowStaticData('global');
memory.chats = memory.chats || {};
const history = (memory.chats[chatId] || []).slice(-6);

return [{
  json: {
    chatId,
    question,
    url: `${cfg.citedeskUrl.replace(/\/$/, '')}/ask`,
    request: { question, session_id: `telegram-${chatId}`, history },
  },
}];
