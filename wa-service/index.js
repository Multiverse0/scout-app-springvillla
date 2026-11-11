// Scout WhatsApp service: connects ONE WhatsApp account (via WhatsApp Web) and exposes a tiny local API
// that Scout uses to send messages and receive replies. Pacing/limits are enforced by Scout (max 100/day).
//
//   GET  /status          -> { state, qr (data URL when scanning needed), me }
//   POST /send {to,text}  -> { ok, id } | { error: "not_on_whatsapp" | ... }
//   POST /logout
//   Incoming messages are POSTed to  SCOUT_URL/api/wa/incoming
//
// Env: PORT (3001) · SCOUT_URL (http://127.0.0.1:8000) · WA_SECRET (shared secret) · CHROME_PATH · SESSION_DIR
const http = require("http");
const fs = require("fs");
const path = require("path");
const QRCode = require("qrcode");
const { Client, LocalAuth } = require("whatsapp-web.js");

const PORT = +(process.env.PORT || 3001);
const HOST = process.env.WA_HOST || "127.0.0.1";
const SCOUT_URL = process.env.SCOUT_URL || "http://127.0.0.1:8000";
const SECRET = process.env.WA_SECRET || "";
const SESSION_DIR = process.env.SESSION_DIR || "./wa-session";

let state = "starting", qr = null, me = null, client = null, lastError = null;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), "[wa]", ...a);

let retries = 0;
function clearLocks() {   // a crashed browser leaves lock files that block the next start
  const dir = path.join(SESSION_DIR, "session");
  for (const f of ["SingletonLock", "SingletonSocket", "SingletonCookie"]) { try { fs.rmSync(path.join(dir, f), { force: true }); } catch {} }
}
function clearCache() {   // a stale cached copy of WhatsApp Web breaks startup; the login (IndexedDB/Local Storage) is kept
  const d = path.join(SESSION_DIR, "session", "Default");
  for (const f of ["Service Worker", "Cache", "Code Cache", "GPUCache"]) { try { fs.rmSync(path.join(d, f), { recursive: true, force: true }); } catch {} }
  log("cleared WhatsApp Web cache (login kept)");
}
async function restart(why, delay) {
  log("restarting in", delay / 1000, "s:", why);
  try { await client.destroy(); } catch {}
  clearLocks();
  const authed = fs.existsSync(path.join(SESSION_DIR, ".authed"));
  if ((!authed && retries >= 2) || retries >= 6) {   // never logged in (nothing to lose), or hopelessly stuck
    try { fs.rmSync(path.join(SESSION_DIR, "session"), { recursive: true, force: true }); } catch {}
    try { fs.rmSync(path.join(SESSION_DIR, ".authed"), { force: true }); } catch {}
    log(authed ? "session reset: scan the QR code again in Scout" : "cleared unfinished session");
  } else if (retries >= 2) clearCache();
  setTimeout(start, delay);
}
function start() {
  state = "starting"; qr = null; clearLocks();
  client = new Client({
    authStrategy: new LocalAuth({ dataPath: SESSION_DIR }),
    puppeteer: {
      headless: true,
      executablePath: process.env.CHROME_PATH || undefined,
      args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    },
  });
  client.on("qr", async q => { state = "qr"; qr = await QRCode.toDataURL(q); log("scan the QR code in Scout > Settings > WhatsApp"); });
  client.on("authenticated", () => { state = "authenticated"; qr = null; log("authenticated"); });
  client.on("auth_failure", m => { state = "auth_failure"; lastError = String(m); log("auth failure", m); });
  client.on("ready", () => { state = "ready"; qr = null; retries = 0; me = client.info?.wid?.user || null;
    try { fs.mkdirSync(SESSION_DIR, { recursive: true }); fs.writeFileSync(path.join(SESSION_DIR, ".authed"), new Date().toISOString()); } catch {} log("ready as", me); });
  client.on("disconnected", async reason => {
    state = "disconnected"; lastError = String(reason);
    restart("disconnected: " + reason, 15000);
  });
  client.on("message", async msg => {
    try {
      if (msg.fromMe || msg.isStatus || msg.from.endsWith("@g.us") || msg.from === "status@broadcast") return;
      const contact = await msg.getContact();
      const number = (contact?.number || msg.from.split("@")[0]).replace(/\D/g, "");
      const body = msg.hasMedia && !msg.body ? "(sent a photo/file)" : msg.body;
      await fetch(SCOUT_URL + "/api/wa/incoming", {
        method: "POST", headers: { "Content-Type": "application/json", "x-scout-secret": SECRET },
        body: JSON.stringify({ from: number, text: body, id: msg.id?._serialized, ts: msg.timestamp }),
      });
      log("incoming from", number);
    } catch (e) { log("incoming handler error", e.message); }
  });
  client.initialize().catch(e => {
    state = "error"; lastError = e.message; retries++;
    restart("init error: " + e.message, Math.min(5000 * retries, 60000));
  });
}

async function send(to, text) {
  if (state !== "ready") return { error: "not_ready", state };
  const digits = String(to).replace(/\D/g, "");
  const id = await client.getNumberId(digits);
  if (!id) return { error: "not_on_whatsapp" };
  const chatId = id._serialized;
  try {   // look human: open chat, show "typing…" for a few seconds, then send
    const chat = await client.getChatById(chatId);
    await chat.sendSeen().catch(() => {});
    await chat.sendStateTyping();
    await sleep(Math.min(9000, 1500 + text.length * 35));
    await chat.clearState().catch(() => {});
  } catch {}
  const m = await client.sendMessage(chatId, text);
  return { ok: true, id: m.id?._serialized };
}

function body(req) {
  return new Promise(res => { let b = ""; req.on("data", c => (b += c)); req.on("end", () => { try { res(JSON.parse(b || "{}")); } catch { res({}); } }); });
}

http.createServer(async (req, res) => {
  const out = (code, obj) => { res.writeHead(code, { "Content-Type": "application/json" }); res.end(JSON.stringify(obj)); };
  if (req.method === "GET" && req.url === "/health") return out(200, { ok: true, state });
  if (SECRET && req.headers["x-scout-secret"] !== SECRET) return out(401, { error: "unauthorized" });
  try {
    if (req.method === "GET" && req.url === "/status") return out(200, { state, qr, me, lastError });
    if (req.method === "POST" && req.url === "/send") {
      const { to, text } = await body(req);
      if (!to || !text) return out(400, { error: "to and text required" });
      return out(200, await send(to, text));
    }
    if (req.method === "POST" && req.url === "/logout") {
      try { await client.logout(); } catch {}
      try { await client.destroy(); } catch {}
      me = null; setTimeout(start, 2000); return out(200, { ok: true });
    }
    out(404, { error: "not found" });
  } catch (e) { out(500, { error: e.message }); }
}).listen(PORT, HOST, () => log(`listening on ${HOST}:${PORT}`));

start();
