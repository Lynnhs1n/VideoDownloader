// VD Cookie Exporter —— 用 chrome.cookies API 读取明文 cookie 并导出为 Netscape 格式。
// 说明：扩展由 Chrome 内部提供 cookie 明文，因此不受 v20 应用绑定加密限制。

const TARGETS = [
  { domain: "youtube.com" },
  { domain: "google.com" }
];
const URLS = [
  "https://www.youtube.com/",
  "https://youtube.com/",
  "https://www.google.com/",
  "https://accounts.google.com/"
];

async function collect() {
  const seen = new Set();
  const out = [];
  for (const q of TARGETS) {
    try {
      const cs = await chrome.cookies.getAll(q);
      for (const c of cs) {
        const k = c.domain + "|" + c.name + "|" + c.path;
        if (!seen.has(k)) { seen.add(k); out.push(c); }
      }
    } catch (e) { /* ignore */ }
  }
  for (const u of URLS) {
    try {
      const cs = await chrome.cookies.getAll({ url: u });
      for (const c of cs) {
        const k = c.domain + "|" + c.name + "|" + c.path;
        if (!seen.has(k)) { seen.add(k); out.push(c); }
      }
    } catch (e) { /* ignore */ }
  }
  return out.filter(c => (c.domain || "").includes("youtube.com") || (c.domain || "").includes("google.com"));
}

function toNetscape(cookies) {
  const lines = [
    "# Netscape HTTP Cookie File",
    "# Exported by VD Cookie Exporter (Chrome extension)",
    "# " + new Date().toISOString(),
    ""
  ];
  for (const c of cookies) {
    const domain = c.domain || "";
    const flag = domain.startsWith(".") ? "TRUE" : "FALSE";
    const secure = c.secure ? "TRUE" : "FALSE";
    let exp = c.expirationDate ? Math.floor(c.expirationDate) : 0;
    if (!exp || exp <= 0) exp = Math.floor(Date.now() / 1000) + 365 * 24 * 3600;
    const row = [domain, flag, c.path || "/", secure, exp, c.name, c.value].join("\t");
    if (!row.includes("\n") && !row.includes("\r")) lines.push(row);
  }
  return lines.join("\n") + "\n";
}

async function exportNow(reason) {
  let cookies = [];
  try {
    cookies = await collect();
  } catch (e) {
    console.error("collect failed", e);
  }
  console.log("VD-EXPORT reason=" + reason + " count=" + cookies.length);
  if (!cookies.length) return;

  const text = toNetscape(cookies);
  const dataUrl = "data:text/plain;charset=utf-8," + encodeURIComponent(text);
  try {
    await chrome.downloads.download({
      url: dataUrl,
      filename: "vd_cookies_export.txt",
      saveAs: false,
      conflictAction: "overwrite"
    });
    console.log("VD-EXPORT download triggered");
  } catch (e) {
    console.error("download failed", e);
  }

  // 同时写入扩展本地存储，便于校验
  try {
    await chrome.storage.local.set({
      vd_export: {
        time: new Date().toISOString(),
        count: cookies.length,
        names: Array.from(new Set(cookies.map(c => c.name))).sort()
      }
    });
  } catch (e) { /* ignore */ }
}

chrome.runtime.onInstalled.addListener(() => { exportNow("installed"); });
chrome.runtime.onStartup.addListener(() => { exportNow("startup"); });
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.action === "export") {
    exportNow("message").then(() => sendResponse({ ok: true })).catch(() => sendResponse({ ok: false }));
    return true;
  }
});
