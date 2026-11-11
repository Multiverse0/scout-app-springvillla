// Talks to Scout for the WhatsApp Web page (pages can't call Scout directly).
async function cfg() {
  const c = await chrome.storage.local.get(["scoutUrl", "token"]);
  return { scoutUrl: (c.scoutUrl || "http://localhost:8000").replace(/\/+$/, ""), token: c.token || "" };
}
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type !== "api") return;
  (async () => {
    const { scoutUrl, token } = await cfg();
    try {
      const r = await fetch(scoutUrl + msg.path, {
        method: msg.method || "GET",
        headers: { "Content-Type": "application/json", "x-scout-token": token },
        body: msg.body ? JSON.stringify(msg.body) : undefined,
      });
      const j = await r.json().catch(() => ({}));
      reply(r.ok ? j : { error: j.detail || ("HTTP " + r.status) });
    } catch (e) {
      reply({ error: "Can't reach Scout at " + scoutUrl + " (is it running?)" });
    }
  })();
  return true;
});
