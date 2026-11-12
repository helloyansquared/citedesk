/*!
 * CiteDesk chat widget. No dependencies, one file, isolated in a shadow DOM.
 *
 *   <script src="https://your-server/widget.js" defer
 *           data-title="Ask Loopline"
 *           data-color="#0f7a5a"
 *           data-suggestions="How long does charging take?|Can I return a bike?"></script>
 *
 * Options (all optional):
 *   data-endpoint     URL of POST /ask (default: /ask next to this script)
 *   data-contact      URL of POST /contact (default: /contact next to the endpoint)
 *   data-title        header title
 *   data-subtitle     header subtitle
 *   data-greeting     first message
 *   data-color        accent colour
 *   data-suggestions  questions shown as chips, separated by |
 *   data-open         "true" to start open
 *   data-recorded     URL of recorded answers (static demo without a server)
 */
(function () {
  "use strict";
  if (window.__citedeskLoaded) return;
  window.__citedeskLoaded = true;

  var script = document.currentScript || document.querySelector("script[src*='widget.js']");
  var ds = (script && script.dataset) || {};
  var scriptBase = script && script.src ? new URL(".", script.src).href : location.href;
  var endpoint = new URL(ds.endpoint || "ask", scriptBase).href;
  var contactEndpoint = new URL(ds.contact || "contact", endpoint).href;
  var cfg = {
    title: ds.title || "Help center assistant",
    subtitle: ds.subtitle || "Answers from our help docs, with sources",
    greeting: ds.greeting || "Hi! Ask me anything about our products, delivery, returns or repairs.",
    color: ds.color || "#0f7a5a",
    suggestions: (ds.suggestions || "").split("|").map(function (s) { return s.trim(); }).filter(Boolean),
    open: ds.open === "true",
    recorded: ds.recorded ? new URL(ds.recorded, scriptBase).href : null,
  };

  var history = [];
  var busy = false;
  var recorded = null;
  var sessionId = (function () {
    var id = null;
    try { id = sessionStorage.getItem("citedesk-session"); } catch (e) { /* storage blocked */ }
    if (!id) {
      id = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
      try { sessionStorage.setItem("citedesk-session", id); } catch (e) { /* ignore */ }
    }
    return id;
  })();

  // ------------------------------------------------------------------ DOM

  var host = document.createElement("div");
  host.id = "citedesk-widget";
  var root = host.attachShadow({ mode: "open" });
  root.innerHTML =
    "<style>" + css(cfg.color) + "</style>" +
    '<button class="launcher" type="button" aria-label="Open the help chat" aria-expanded="false">' +
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.2 3.6c-.5.4-1.3.1-1.3-.6V16A2.5 2.5 0 0 1 4 13.5z"/></svg>' +
    '<span class="launcher-label">Ask us</span></button>' +
    '<section class="panel" role="dialog" aria-modal="false" aria-label="' + esc(cfg.title) + '" hidden>' +
    '<header><div class="who"><span class="dot" aria-hidden="true"></span><div><h2></h2><p></p></div></div>' +
    '<button class="close" type="button" aria-label="Close the chat">&times;</button></header>' +
    '<div class="log" role="log" aria-live="polite"></div>' +
    '<div class="chips"></div>' +
    '<form class="composer"><label class="sr" for="cd-input">Your question</label>' +
    '<textarea id="cd-input" rows="1" maxlength="1000" placeholder="Type your question…"></textarea>' +
    '<button class="send" type="submit" aria-label="Send"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.4 20.4 21 12 3.4 3.6 3 10l12 2-12 2z"/></svg></button></form>' +
    '<footer>Answers come from our help documents · <a href="https://github.com/helloyansquared/citedesk" target="_blank" rel="noopener">CiteDesk</a></footer>' +
    "</section>";
  var $ = function (sel) { return root.querySelector(sel); };
  var launcher = $(".launcher"), panel = $(".panel"), log = $(".log"), chips = $(".chips");
  var form = $(".composer"), input = $("textarea"), sendBtn = $(".send");
  $("h2").textContent = cfg.title;
  $("header p").textContent = cfg.subtitle;

  function mount() {
    document.body.appendChild(host);
    addBot({ answer: cfg.greeting, status: "smalltalk", citations: [] }, true);
    renderChips(cfg.suggestions);
    if (cfg.open) toggle(true);
  }

  // Small public API so the host page can open the chat or ask something: CiteDesk.ask("...")
  window.CiteDesk = {
    open: function () { toggle(true); },
    close: function () { toggle(false); },
    ask: function (question) {
      toggle(true);
      input.value = String(question || "");
      submit();
    },
  };
  if (document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);

  if (cfg.recorded) {
    fetch(cfg.recorded).then(function (r) { return r.json(); }).then(function (data) {
      recorded = data;
      if (!cfg.suggestions.length && data.suggestions) renderChips(data.suggestions);
    }).catch(function () { recorded = { answers: {} }; });
  }

  // ------------------------------------------------------------------ behaviour

  function toggle(open) {
    var show = open === undefined ? panel.hidden : open;
    panel.hidden = !show;
    launcher.setAttribute("aria-expanded", String(show));
    launcher.classList.toggle("is-open", show);
    if (show) setTimeout(function () { input.focus(); }, 30);
  }
  launcher.addEventListener("click", function () { toggle(); });
  $(".close").addEventListener("click", function () { toggle(false); launcher.focus(); });
  root.addEventListener("keydown", function (e) { if (e.key === "Escape" && !panel.hidden) { toggle(false); launcher.focus(); } });

  input.addEventListener("input", autosize);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  form.addEventListener("submit", function (e) { e.preventDefault(); submit(); });

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  }

  function renderChips(list) {
    chips.innerHTML = "";
    list.slice(0, 4).forEach(function (q) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.textContent = q;
      b.addEventListener("click", function () { input.value = q; submit(); });
      chips.appendChild(b);
    });
    chips.hidden = !list.length;
  }

  function submit() {
    var q = input.value.trim();
    if (!q || busy) return;
    input.value = "";
    autosize();
    chips.hidden = true;
    addUser(q);
    busy = true;
    sendBtn.disabled = true;
    var typing = addTyping();
    ask(q).then(function (answer) {
      typing.remove();
      addBot(answer);
      history.push({ role: "user", content: q }, { role: "assistant", content: answer.answer });
      history = history.slice(-12);
    }).catch(function (err) {
      typing.remove();
      addBot({
        answer: err && err.userMessage ? err.userMessage : "Sorry, I can't answer right now.",
        status: "handoff", needs_human: true, citations: [], question: q, offline: true,
      });
    }).then(function () {
      busy = false;
      sendBtn.disabled = false;
      input.focus();
    });
  }

  function ask(question) {
    if (cfg.recorded) return askRecorded(question);
    return fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question, session_id: sessionId, history: history, page_url: location.href }),
    }).then(function (r) {
      if (r.ok) return r.json();
      return r.json().catch(function () { return {}; }).then(function (body) {
        var e = new Error("HTTP " + r.status);
        e.userMessage = (body && typeof body.detail === "string") ? body.detail : "Sorry, I can't answer right now.";
        throw e;
      });
    });
  }

  function askRecorded(question) {
    var key = normalise(question);
    var answers = (recorded && recorded.answers) || {};
    var hit = null;
    Object.keys(answers).forEach(function (k) { if (normalise(k) === key) hit = answers[k]; });
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve(hit || {
          answer: "This is a recorded demo, so I can only replay the suggested questions. Run CiteDesk yourself to ask anything (see the GitHub repository).",
          status: "smalltalk", needs_human: false, citations: [],
        });
      }, hit ? 700 : 350);
    });
  }

  // ------------------------------------------------------------------ rendering

  function addUser(text) {
    var row = el("div", "msg user");
    var bubble = el("div", "bubble");
    bubble.textContent = text;
    row.appendChild(bubble);
    log.appendChild(row);
    scroll();
  }

  function addTyping() {
    var row = el("div", "msg bot");
    row.innerHTML = '<div class="bubble typing" aria-label="Assistant is typing"><i></i><i></i><i></i></div>';
    log.appendChild(row);
    scroll();
    return row;
  }

  function addBot(a, silent) {
    var row = el("div", "msg bot");
    var bubble = el("div", "bubble");
    var cites = a.citations || [];
    bubble.innerHTML = format(a.answer || "", cites.length);
    row.appendChild(bubble);

    if (cites.length) {
      var list = el("ol", "sources");
      list.setAttribute("aria-label", "Sources");
      cites.forEach(function (c) {
        var li = document.createElement("li");
        var btn = el("button", "src");
        btn.type = "button";
        btn.setAttribute("aria-expanded", "false");
        btn.innerHTML = '<span class="n">' + c.n + "</span><span>" + esc(c.location) + "</span>";
        var detail = el("div", "snippet");
        detail.hidden = true;
        detail.innerHTML = "<q>" + esc(c.snippet) + "</q>" +
          (c.url ? ' <a href="' + esc(resolveUrl(c.url)) + '" target="_blank" rel="noopener">Open document</a>' : "");
        btn.addEventListener("click", function () {
          detail.hidden = !detail.hidden;
          btn.setAttribute("aria-expanded", String(!detail.hidden));
        });
        li.appendChild(btn);
        li.appendChild(detail);
        list.appendChild(li);
        c._li = li;
      });
      row.appendChild(list);
      bubble.querySelectorAll("button.cite").forEach(function (b) {
        b.addEventListener("click", function () {
          var c = cites.filter(function (x) { return String(x.n) === b.dataset.n; })[0];
          if (c && c._li) { c._li.querySelector("button").click(); c._li.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
        });
      });
    }

    if (a.needs_human) row.appendChild(handoffBox(a));
    log.appendChild(row);
    if (!silent) scroll();
  }

  function handoffBox(a) {
    var box = el("form", "handoff");
    box.innerHTML =
      '<p><strong>' + (a.offline ? "Leave your e-mail and a teammate will get back to you." : "I've flagged this for a teammate.") + "</strong>" +
      (a.offline ? "" : " Leave your e-mail and they will reply to you directly.") + "</p>" +
      '<div class="row"><label class="sr">E-mail</label><input type="email" required placeholder="you@example.com" autocomplete="email">' +
      '<button type="submit">Send</button></div><p class="note" hidden></p>';
    var email = box.querySelector("input"), note = box.querySelector(".note"), btn = box.querySelector("button");
    box.addEventListener("submit", function (e) {
      e.preventDefault();
      if (cfg.recorded) { done("Recorded demo: nothing was sent."); return; }
      btn.disabled = true;
      fetch(contactEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.value, question: a.question || null, session_id: sessionId, page_url: location.href }),
      }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        done("Thanks! We'll reply to " + email.value + ".");
      }).catch(function () {
        btn.disabled = false;
        note.hidden = false;
        note.textContent = "That didn't work. Please check the address and try again.";
      });
    });
    function done(msg) {
      box.querySelector(".row").remove();
      note.hidden = false;
      note.textContent = msg;
    }
    return box;
  }

  // Minimal, safe formatting: escape everything, then **bold**, "- " lists, paragraphs and [n] citations.
  function format(text, nCites) {
    var lines = esc(text).split(/\n/);
    var html = "", inList = false;
    lines.forEach(function (line) {
      var m = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
      if (m) {
        if (!inList) { html += "<ul>"; inList = true; }
        html += "<li>" + m[1] + "</li>";
        return;
      }
      if (inList) { html += "</ul>"; inList = false; }
      if (line.trim()) html += "<p>" + line + "</p>";
    });
    if (inList) html += "</ul>";
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    return html.replace(/\[(\d+)\]/g, function (all, n) {
      return Number(n) >= 1 && Number(n) <= nCites
        ? '<button type="button" class="cite" data-n="' + n + '" aria-label="Source ' + n + '">' + n + "</button>"
        : "";
    });
  }

  function resolveUrl(url) {
    try {
      var u = new URL(url, endpoint);
      return (u.protocol === "http:" || u.protocol === "https:") ? u.href : "#";
    } catch (e) { return "#"; }
  }
  function normalise(s) { return s.toLowerCase().replace(/[\s?!.,;:'"’？！。，]+/g, " ").trim(); }
  function el(tag, cls) { var n = document.createElement(tag); if (cls) n.className = cls; return n; }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }
  function scroll() { log.scrollTop = log.scrollHeight; }

  function css(accent) {
    return [
      ":host{all:initial}",
      "*{box-sizing:border-box}",
      "button,input,textarea{font-family:inherit}",
      ".sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}",
      ":host{--accent:" + accent + ";--bg:#fff;--surface:#f4f6f5;--text:#15201b;--muted:#5d6b65;--line:#dde4e1;--user:#e8f3ee;--warn-bg:#fff4dc;--warn:#7a4b00;",
      "font:15px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,'PingFang SC','Microsoft YaHei',sans-serif;color:var(--text)}",
      "@media (prefers-color-scheme:dark){:host{--bg:#141b18;--surface:#1b2420;--text:#e5ede9;--muted:#9aa9a2;--line:#2a3631;--user:#1f3a2e;--warn-bg:#3a2d12;--warn:#f3c26b}}",
      ".launcher{position:fixed;right:20px;bottom:20px;z-index:2147483000;display:flex;align-items:center;gap:8px;border:0;border-radius:999px;",
      "padding:12px 18px 12px 14px;background:var(--accent);color:#fff;font-weight:600;font-size:15px;line-height:1;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.18)}",
      ".launcher svg{width:22px;height:22px;fill:currentColor}",
      ".launcher:focus-visible,.panel button:focus-visible,.panel textarea:focus-visible,.panel input:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 45%,transparent);outline-offset:2px}",
      ".launcher.is-open{display:none}",
      ".panel{position:fixed;right:20px;bottom:20px;z-index:2147483001;width:min(400px,calc(100vw - 32px));height:min(620px,calc(100vh - 40px));",
      "display:flex;flex-direction:column;background:var(--bg);border:1px solid var(--line);border-radius:16px;overflow:hidden;box-shadow:0 16px 48px rgba(0,0,0,.22)}",
      ".panel[hidden]{display:none}",
      "header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 14px 12px 16px;background:var(--accent);color:#fff}",
      ".who{display:flex;align-items:center;gap:10px;min-width:0}",
      ".who h2{margin:0;font-size:16px;font-weight:650;line-height:1.2}",
      ".who p{margin:2px 0 0;font-size:12.5px;opacity:.85}",
      ".dot{width:9px;height:9px;border-radius:50%;background:#7dffbf;box-shadow:0 0 0 3px rgba(255,255,255,.2);flex:none}",
      ".close{border:0;background:transparent;color:#fff;font-size:26px;line-height:1;width:34px;height:34px;border-radius:8px;cursor:pointer}",
      ".close:hover{background:rgba(255,255,255,.15)}",
      ".log{flex:1;overflow-y:auto;padding:16px 14px 8px;display:flex;flex-direction:column;gap:12px;background:var(--bg)}",
      ".msg{display:flex;flex-direction:column;max-width:88%}",
      ".msg.user{align-self:flex-end}",
      ".bubble{padding:10px 13px;border-radius:14px;background:var(--surface);overflow-wrap:anywhere}",
      ".user .bubble{background:var(--user);border-bottom-right-radius:4px;white-space:pre-wrap}",
      ".bot .bubble{border-bottom-left-radius:4px}",
      ".bubble p{margin:0 0 6px}.bubble p:last-child{margin:0}",
      ".bubble ul{margin:4px 0 6px;padding-left:20px}",
      ".cite{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:18px;padding:0 4px;margin:0 1px;vertical-align:1px;",
      "border:0;border-radius:6px;background:color-mix(in srgb,var(--accent) 16%,transparent);color:var(--accent);font-weight:700;font-size:11px;line-height:1;cursor:pointer}",
      "@media (prefers-color-scheme:dark){.cite{color:#9fe7c7}}",
      ".sources{list-style:none;margin:6px 0 0;padding:0;display:flex;flex-direction:column;gap:4px}",
      ".src{display:flex;align-items:flex-start;gap:8px;width:100%;text-align:left;border:1px solid var(--line);background:var(--bg);color:var(--muted);",
      "border-radius:10px;padding:6px 9px;font-size:12.5px;line-height:1.35;cursor:pointer}",
      ".src:hover{border-color:var(--accent);color:var(--text)}",
      ".src .n{flex:none;font-weight:700;color:var(--accent)}",
      "@media (prefers-color-scheme:dark){.src .n{color:#9fe7c7}}",
      ".snippet{margin:4px 2px 2px;padding:8px 10px;border-left:3px solid var(--accent);background:var(--surface);border-radius:0 8px 8px 0;font-size:12.5px;color:var(--muted)}",
      ".snippet q{quotes:'“' '”';color:var(--text)}",
      ".snippet a{color:var(--accent);font-weight:600;white-space:nowrap}",
      ".handoff{margin-top:6px;padding:10px 12px;border-radius:12px;background:var(--warn-bg);color:var(--warn);font-size:13px}",
      ".handoff p{margin:0 0 8px}.handoff .note{margin:6px 0 0}",
      ".handoff .row{display:flex;gap:6px}",
      ".handoff input{flex:1;min-width:0;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit}",
      ".handoff button{border:0;border-radius:8px;padding:7px 12px;background:var(--accent);color:#fff;font-weight:600;font-size:13px;line-height:1;cursor:pointer}",
      ".typing{display:flex;gap:4px;padding:14px}",
      ".typing i{width:7px;height:7px;border-radius:50%;background:var(--muted);opacity:.4;animation:cd 1.2s infinite}",
      ".typing i:nth-child(2){animation-delay:.2s}.typing i:nth-child(3){animation-delay:.4s}",
      "@keyframes cd{0%,80%,100%{opacity:.25;transform:translateY(0)}40%{opacity:.9;transform:translateY(-3px)}}",
      "@media (prefers-reduced-motion:reduce){.typing i{animation:none}}",
      ".chips{display:flex;flex-wrap:wrap;gap:6px;padding:4px 14px 10px}",
      ".chips[hidden]{display:none}",
      ".chip{border:1px solid color-mix(in srgb,var(--accent) 40%,var(--line));background:var(--bg);color:var(--text);border-radius:999px;padding:6px 11px;font-size:13px;line-height:1.2;cursor:pointer;text-align:left}",
      ".chip:hover{background:color-mix(in srgb,var(--accent) 10%,var(--bg))}",
      ".composer{display:flex;align-items:flex-end;gap:8px;padding:10px 12px;border-top:1px solid var(--line);background:var(--bg)}",
      "textarea{flex:1;resize:none;border:1px solid var(--line);border-radius:12px;padding:9px 12px;background:var(--surface);color:var(--text);font:inherit;max-height:120px}",
      ".send{flex:none;width:40px;height:40px;border:0;border-radius:12px;background:var(--accent);color:#fff;cursor:pointer;display:grid;place-items:center}",
      ".send:disabled{opacity:.5;cursor:default}",
      ".send svg{width:20px;height:20px;fill:currentColor}",
      "footer{padding:0 12px 9px;font-size:11px;color:var(--muted);text-align:center;background:var(--bg)}",
      "footer a{color:inherit}",
      "@media (max-width:480px){.panel{right:0;bottom:0;width:100vw;height:100dvh;border-radius:0;border:0}.launcher{right:14px;bottom:14px}}",
    ].join("");
  }
})();
