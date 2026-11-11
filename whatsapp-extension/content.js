// Scout WhatsApp Sender - runs inside web.whatsapp.com
// Scout decides WHAT to send and WHEN (daily cap, hours, gaps). This script only types it into WhatsApp Web
// like you would, presses send, and reports replies from lead numbers.
(() => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const api = (path, method, body) => new Promise(res => {
    try { chrome.runtime.sendMessage({ type: "api", path, method, body }, r => res(r || { error: "no response" })); }
    catch (e) { res({ error: "extension reloaded, refresh WhatsApp Web" }); }
  });
  const store = {
    get: k => new Promise(r => chrome.storage.local.get([k], o => r(o[k]))),
    set: (k, v) => new Promise(r => chrome.storage.local.set({ [k]: v }, r)),
  };
  const $ = s => document.querySelector(s);
  const loggedIn = () => !!$("#pane-side");

  // small status badge so you can see it working
  const badge = document.createElement("div");
  badge.style.cssText = "position:fixed;z-index:99999;left:12px;bottom:12px;background:#121826;color:#e6e9ef;font:12px system-ui;padding:6px 10px;border-radius:8px;opacity:.92;pointer-events:none;max-width:340px";
  const say = t => { badge.textContent = "Scout · " + t; if (!badge.isConnected) document.body.appendChild(badge); };

  async function waitFor(fn, ms) { const end = Date.now() + ms; while (Date.now() < end) { const v = fn(); if (v) return v; await sleep(500); } return null; }

  function composeBox() { return $('#main footer div[contenteditable="true"]') || $('footer div[contenteditable="true"][role="textbox"]'); }
  function sendButton() {
    return $('#main footer button[aria-label="Send"]') || $('footer button[aria-label="Send"]') ||
      ($('#main footer [data-icon*="send"]') || {}).closest?.("button, [role=button]") || null;
  }
  function notOnWhatsAppDialog() {
    const d = $('[role="dialog"]');
    return d && /isn.t on WhatsApp|invalid/i.test(d.innerText) ? d : null;
  }

  // ---- finish a send that started before the page reloaded ----
  async function finishPending(job) {
    say("opening chat for " + job.business + "…");
    const res = await waitFor(() => notOnWhatsAppDialog() ? "invalid" : (composeBox() && composeBox().innerText.trim() ? "ready" : null), 45000);
    if (res === "invalid") {
      [...notOnWhatsAppDialog().querySelectorAll("button")].find(b => /ok/i.test(b.innerText))?.click();
      await api("/api/waweb/result", "POST", { id: job.id, ok: false, error: "not_on_whatsapp" });
      say(job.business + ": number not on WhatsApp, skipped");
      return;
    }
    if (res !== "ready") {
      await api("/api/waweb/result", "POST", { id: job.id, ok: false, error: "chat_did_not_open" });
      say("chat didn't open, will retry later"); return;
    }
    await sleep(1500 + Math.random() * 2500);                        // a human-like pause
    const btn = sendButton();
    if (btn) btn.click();
    else composeBox().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
    const sent = await waitFor(() => { const c = composeBox(); return c && !c.innerText.trim(); }, 8000);
    await api("/api/waweb/result", "POST", { id: job.id, ok: !!sent, error: sent ? null : "send_failed" });
    say(sent ? "sent to " + job.business + " ✓" : "couldn't press send for " + job.business);
    await sleep(1500);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", keyCode: 27, bubbles: true }));  // close the chat so new replies show as unread
  }

  // ---- replies: unread chats (and read ones whose last message is from them) from lead numbers ----
  async function scanReplies(leadNumbers, leadNames) {
    const seen = new Set((await store.get("seenReplies")) || []);
    const found = [];
    document.querySelectorAll('#pane-side [role="row"]').forEach(row => {
      const spans = [...row.querySelectorAll("span[title]")];
      if (!spans.length) return;
      const title = spans[0].getAttribute("title") || "";
      const preview = (spans[1] && spans[1].getAttribute("title")) || "";
      const digits = title.replace(/\D/g, "");
      const unread = [...row.querySelectorAll("[aria-label]")].some(e => /unread/i.test(e.getAttribute("aria-label")));
      const fromMe = !!row.querySelector('[data-icon*="check"], [data-icon*="status-"], [data-icon*="dblcheck"]');
      const isLead = (digits.length >= 10 && leadNumbers.includes(digits)) || leadNames.includes(title.trim().toLowerCase());
      if (!isLead || !preview) return;            // only ever look at chats with your leads, never personal chats
      if (!unread && fromMe) return;              // last message was yours: nothing new from them
      const key = (digits || title) + "|" + preview;
      if (seen.has(key)) return;
      found.push({ number: digits, title, text: preview, key });
    });
    for (const f of found) {
      const r = await api("/api/waweb/incoming", "POST", { number: f.number, title: f.title, text: f.text });
      if (!r.error) seen.add(f.key);
      if (r.matched) say("reply from " + (r.business || f.title) + " → sent to Scout");
    }
    await store.set("seenReplies", [...seen].slice(-800));
  }

  async function loop() {
    if (!loggedIn()) { say("waiting for WhatsApp Web to load…"); return; }
    const pending = await store.get("pendingJob");
    if (pending) {
      await store.set("pendingJob", null);
      if (Date.now() - pending.started < 3 * 60 * 1000) await finishPending(pending);
      else await api("/api/waweb/result", "POST", { id: pending.id, ok: false, error: "timed_out" });
    }
    const r = await api("/api/waweb/next", "GET");
    if (r.error) { say(r.error); return; }
    await scanReplies(r.numbers || [], r.names || []);
    if (r.job) {
      say("sending to " + r.job.business + "…");
      await store.set("pendingJob", { ...r.job, started: Date.now() });
      location.href = "https://web.whatsapp.com/send?phone=" + r.job.number + "&text=" + encodeURIComponent(r.job.text);
      return;   // page reloads; finishPending() continues after load
    }
    say(r.status || "connected, waiting");
  }

  (async () => {
    await waitFor(loggedIn, 60000);
    while (true) { try { await loop(); } catch (e) { say("error: " + e.message); } await sleep(20000); }
  })();
})();
