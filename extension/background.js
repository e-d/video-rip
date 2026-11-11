// VideoRip service worker: sniffs media URLs per tab, runs downloads through
// the native helper, and keeps job state in chrome.storage.session for the popup.

const HOST = "com.videorip.host";
const MAX_MEDIA_PER_TAB = 15;
const MAX_JOBS_KEPT = 20;

// ------------------------------------------------------------------ media sniffing

const MEDIA_CT = /^(video\/|audio\/|application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml))/i;
const MANIFEST_RE = /\.(m3u8|mpd)(\?|#|$)/i;
const FILE_RE = /\.(mp4|webm|m4a|m4v|mp3|ogg|oga|opus|mov|aac|flac|wav)(\?|#|$)/i;
// Segment / chunk requests aren't useful on their own.
const SEGMENT_RE = /\.(ts|m4s|aac\.part)(\?|#|$)|[/_-](seg|segment|chunk|frag)[-_]?\d+/i;
// yt-dlp handles these sites natively from the page URL; their raw streams are useless.
const SKIP_HOST_RE = /(googlevideo\.com|youtube\.com|ytimg\.com|doubleclick\.net|googlesyndication\.com)/i;

function headerValue(headers, name) {
  const h = (headers || []).find((x) => x.name.toLowerCase() === name);
  return h ? h.value || "" : "";
}

function mediaKind(url, ct) {
  if (MANIFEST_RE.test(url) || /mpegurl|dash\+xml/i.test(ct)) return "stream";
  if (FILE_RE.test(url) || MEDIA_CT.test(ct)) return "file";
  return null;
}

async function getTabMedia(tabId) {
  const key = `media:${tabId}`;
  const got = await chrome.storage.session.get(key);
  return got[key] || [];
}

async function setTabMedia(tabId, list) {
  await chrome.storage.session.set({ [`media:${tabId}`]: list });
}

chrome.webRequest.onHeadersReceived.addListener(
  (d) => {
    if (d.tabId < 0 || !/^https?:/i.test(d.url)) return;
    if (SKIP_HOST_RE.test(new URL(d.url).hostname)) return;
    if (SEGMENT_RE.test(d.url)) return;
    const ct = headerValue(d.responseHeaders, "content-type");
    const kind = mediaKind(d.url, ct);
    if (!kind) return;
    // Ignore tiny files (thumbnails, beeps, ad pings).
    const len = parseInt(headerValue(d.responseHeaders, "content-length") || "0", 10);
    if (kind === "file" && len && len < 300 * 1024 && d.statusCode !== 206) return;
    recordMedia(d.tabId, { url: d.url, kind, referer: d.initiator ? d.initiator + "/" : null, ts: Date.now() });
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other", "object"] },
  ["responseHeaders"]
);

let mediaQueue = Promise.resolve();
function recordMedia(tabId, item) {
  // Serialize writes so concurrent requests don't clobber each other.
  mediaQueue = mediaQueue.then(async () => {
    const list = await getTabMedia(tabId);
    // Dedupe on URL without query string for files (range requests repeat).
    const norm = (u) => u.split("#")[0];
    if (list.some((m) => norm(m.url) === norm(item.url))) return;
    list.push(item);
    // Streams (HLS/DASH manifests) first, then most recent.
    list.sort((a, b) => (a.kind === b.kind ? b.ts - a.ts : a.kind === "stream" ? -1 : 1));
    await setTabMedia(tabId, list.slice(0, MAX_MEDIA_PER_TAB));
  }).catch(() => {});
}

chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.url) chrome.storage.session.remove(`media:${tabId}`);
});
chrome.tabs.onRemoved.addListener((tabId) => chrome.storage.session.remove(`media:${tabId}`));

// ------------------------------------------------------------------ jobs

const ports = new Map(); // jobId -> native Port (in-memory only)

async function getJobs() {
  return (await chrome.storage.session.get("jobs")).jobs || [];
}

let jobsQueue = Promise.resolve();
function updateJobs(fn) {
  jobsQueue = jobsQueue.then(async () => {
    const jobs = await getJobs();
    const next = fn(jobs) || jobs;
    await chrome.storage.session.set({ jobs: next.slice(0, MAX_JOBS_KEPT) });
  }).catch((e) => console.error(e));
  return jobsQueue;
}

function patchJob(id, patch) {
  return updateJobs((jobs) => jobs.map((j) => (j.id === id ? { ...j, ...patch } : j)));
}

function detectBrowser() {
  if (navigator.userAgent.includes("Edg/")) return "edge";
  if (navigator.brave) return "brave";
  return "chrome";
}

async function startJob(tab, mode, { sniffed = true } = {}) {
  if (!tab || !/^https?:/i.test(tab.url || "")) {
    notify("VideoRip", "This tab isn't a regular web page.");
    return;
  }
  const media = sniffed ? await getTabMedia(tab.id) : [];
  const id = crypto.randomUUID();
  const job = {
    id, mode, url: tab.url, title: tab.title || tab.url, status: "running",
    statusText: "Starting…", percent: null, speed: "", eta: "", startedAt: Date.now(),
  };
  await updateJobs((jobs) => [job, ...jobs]);

  let port;
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch (e) {
    await patchJob(id, { status: "error", error: "Couldn't start the helper. Did you run install.sh?" });
    return;
  }
  ports.set(id, port);

  let lastWrite = 0;
  let finished = false;
  port.onMessage.addListener((msg) => {
    switch (msg.type) {
      case "progress": {
        const now = Date.now();
        if (now - lastWrite < 250 && msg.percent !== 100) return; // throttle storage writes
        lastWrite = now;
        patchJob(id, { percent: msg.percent, speed: msg.speed, eta: msg.eta, statusText: "Downloading…" });
        break;
      }
      case "status":
        patchJob(id, { statusText: msg.text });
        break;
      case "title":
        patchJob(id, { title: msg.title });
        break;
      case "done":
        finished = true;
        patchJob(id, {
          status: "done", path: msg.path, title: msg.title, percent: 100, statusText: "Saved",
          warning: msg.warning || null, details: msg.details || null, log: msg.log || null,
        });
        if (msg.warning) notify("Saved, but not converted", `${msg.title}\n${msg.warning}`, id);
        else notify(mode === "audio" ? "Audio saved" : "Video saved", msg.title, id);
        port.disconnect();
        ports.delete(id);
        break;
      case "error":
        finished = true;
        patchJob(id, { status: "error", error: msg.error, details: msg.details || null, log: msg.log || null });
        notify("VideoRip failed", `${job.title}\n${msg.error}`);
        port.disconnect();
        ports.delete(id);
        break;
      case "cancelled":
        finished = true;
        patchJob(id, { status: "cancelled", statusText: "Cancelled" });
        port.disconnect();
        ports.delete(id);
        break;
    }
  });
  port.onDisconnect.addListener(() => {
    ports.delete(id);
    if (finished) return;
    const err = chrome.runtime.lastError?.message || "Helper stopped unexpectedly.";
    const friendly = /not found|forbidden/i.test(err)
      ? "Helper not installed. Run install.sh in the video-rip folder, then reload the extension."
      : err;
    patchJob(id, { status: "error", error: friendly });
  });

  port.postMessage({
    action: "download", mode, url: tab.url, title: tab.title, browser: detectBrowser(),
    media: media.map((m) => ({ url: m.url, referer: m.referer })),
  });
}

async function cancelJob(id) {
  const port = ports.get(id);
  if (port) {
    try { port.postMessage({ action: "cancel" }); } catch (_) {}
    // Give the helper a moment to clean up, then force-close.
    setTimeout(() => { try { port.disconnect(); } catch (_) {} }, 3000);
  }
  ports.delete(id);
  await patchJob(id, { status: "cancelled", statusText: "Cancelled" });
}

// On service-worker restart, any job still "running" has lost its port.
chrome.runtime.onStartup.addListener(markOrphans);
markOrphans();
function markOrphans() {
  updateJobs((jobs) => jobs.map((j) =>
    j.status === "running" && !ports.has(j.id) ? { ...j, status: "error", error: "Interrupted (browser restarted)." } : j
  ));
}

// ------------------------------------------------------------------ notifications

function notify(title, message, jobId) {
  chrome.notifications.create(jobId ? `job:${jobId}` : "", {
    type: "basic", iconUrl: "icons/icon128.png", title, message: message || "",
  });
}

chrome.notifications.onClicked.addListener(async (nid) => {
  if (!nid.startsWith("job:")) return;
  const job = (await getJobs()).find((j) => j.id === nid.slice(4));
  if (job?.path) chrome.runtime.sendNativeMessage(HOST, { action: "reveal", path: job.path }, () => void chrome.runtime.lastError);
  chrome.notifications.clear(nid);
});

// ------------------------------------------------------------------ entry points

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg.cmd === "start") {
      const tab = await chrome.tabs.get(msg.tabId);
      await startJob(tab, msg.mode);
    } else if (msg.cmd === "cancel") {
      await cancelJob(msg.id);
    } else if (msg.cmd === "clear") {
      await updateJobs((jobs) => jobs.filter((j) => j.status === "running"));
    }
    sendResponse({ ok: true });
  })();
  return true;
});

chrome.commands.onCommand.addListener(async (command) => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (command === "save-audio") startJob(tab, "audio");
  if (command === "save-video") startJob(tab, "video");
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "audio", title: "VideoRip: save audio (MP3)", contexts: ["page", "video", "audio", "link"] });
    chrome.contextMenus.create({ id: "video", title: "VideoRip: save video (MP4)", contexts: ["page", "video", "audio", "link"] });
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const mode = info.menuItemId === "audio" ? "audio" : "video";
  // Right-clicking a link downloads that link (e.g. a video in a feed) instead of the page.
  if (info.linkUrl && /^https?:/i.test(info.linkUrl)) {
    startJob({ id: tab.id, url: info.linkUrl, title: info.linkUrl }, mode, { sniffed: false });
  } else {
    startJob(tab, mode);
  }
});
