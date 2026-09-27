// IDMLike Capture — alihkan unduhan browser ke IDMLike (aria2) via localhost.
// Kontrak: extension/background.js (lihat SPEC.md)

const CAPTURE_URL = "http://127.0.0.1:20129/api/capture";
const PING_URL = "http://127.0.0.1:20129/api/ping";
const FETCH_TIMEOUT_MS = 2000;

// Gid yang sedang ditangani supaya tidak ada loop cancel/restore.
const pending = new Set();

function isSkippable(url) {
  return (
    url.startsWith("blob:") ||
    url.startsWith("data:") ||
    url.startsWith("chrome-extension://") ||
    url.startsWith("about:") ||
    url.startsWith("file://")
  );
}

async function pingCaptureServer() {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(PING_URL, { signal: ctrl.signal });
    return res.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(t);
  }
}

async function sendToIdmlike(item) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(CAPTURE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: item.url, filename: item.filename || undefined }),
      signal: ctrl.signal,
    });
    return res.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(t);
  }
}

async function restoreNativeDownload(item) {
  // IDMLike tidak merespons -> kembalikan download asli biar tidak hilang.
  try {
    await chrome.downloads.download({
      url: item.url,
      filename: item.filename || undefined,
      saveAs: false,
    });
  } catch (err) {
    console.warn("[IDMLike] gagal restore download asli:", err);
  }
}

chrome.downloads.onCreated.addListener(async (item) => {
  if (pending.has(item.id)) {
    return; // download yang kita buat sendiri
  }

  const { capture_enabled = true } = await chrome.storage.session.get("capture_enabled");
  if (!capture_enabled) {
    return;
  }
  if (isSkippable(item.url)) {
    return;
  }
  // File dengan ukuran tidak diketahui (0) tetap diproses — bisa jadi besar.
  if (item.totalBytes === 0) {
    // tetap proses; aria2 akan menangani redirect 303 dll.
  }

  const alive = await pingCaptureServer();
  if (!alive) {
    return; // app tidak jalan -> biarkan browser download normal
  }

  pending.add(item.id);
  try {
    await chrome.downloads.cancel(item.id);
    const ok = await sendToIdmlike(item);
    if (!ok) {
      await restoreNativeDownload(item);
    }
  } catch (err) {
    console.warn("[IDMLike] intercept gagal:", err);
    await restoreNativeDownload(item);
  } finally {
    pending.delete(item.id);
  }
});