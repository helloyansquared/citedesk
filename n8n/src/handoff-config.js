// ============================================================
//  CITEDESK HANDOFF ALERTS - SETTINGS
//  Edit the values below, then save.
// ============================================================
return [{
  json: {
    // Your Telegram chat id (the same one you use for Gig Radar works).
    // Send /start to your bot, then open https://api.telegram.org/bot<TOKEN>/getUpdates to find it.
    telegramChatId: 'YOUR_TELEGRAM_CHAT_ID',

    // Must match CITEDESK_WEBHOOK_SECRET on the CiteDesk server. Leave '' to accept every request
    // (fine while n8n only listens on your own computer; set one as soon as n8n is on the internet).
    sharedSecret: '',
  },
}];
