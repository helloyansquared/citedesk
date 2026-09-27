// ============================================================
//  CITEDESK NIGHTLY RE-INDEX - SETTINGS
//  Edit the values below, then save.
// ============================================================
return [{
  json: {
    // Where the CiteDesk server runs (from inside Docker use http://host.docker.internal:8000).
    citedeskUrl: 'http://localhost:8000',

    // CITEDESK_ADMIN_KEY of the server. Can stay '' when n8n and CiteDesk run on the same computer.
    adminKey: '',

    // Telegram chat that gets the report.
    telegramChatId: 'YOUR_TELEGRAM_CHAT_ID',

    // true = only send a message when a document changed or something failed.
    onlyReportChanges: true,
  },
}];
