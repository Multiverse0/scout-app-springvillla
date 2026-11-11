const $ = id => document.getElementById(id);
chrome.storage.local.get(["scoutUrl", "token"], c => { $("u").value = c.scoutUrl || "http://localhost:8000"; $("t").value = c.token || ""; });
$("save").onclick = async () => {
  const url = $("u").value.trim().replace(/\/+$/, "") || "http://localhost:8000", token = $("t").value.trim();
  if (!/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(url)) {
    const ok = await chrome.permissions.request({ origins: [new URL(url).origin + "/*"] }).catch(() => false);
    if (!ok) { $("msg").textContent = "Permission needed to reach " + url; return; }
  }
  await chrome.storage.local.set({ scoutUrl: url, token });
  chrome.runtime.sendMessage({ type: "api", path: "/api/waweb/next?test=1", method: "GET" }, r => {
    $("msg").textContent = r && !r.error ? "✓ Connected to Scout. Keep a WhatsApp Web tab open." : "✗ " + ((r && r.error) || "no response");
  });
};
