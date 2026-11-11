const HOST = "com.videorip.host";
const $ = (id) => document.getElementById(id);
let currentTab = null;

function el(tag, props = {}, children = []) {
  const n = document.createElement(tag);
  Object.assign(n, props);
  for (const c of [].concat(children)) n.append(c);
  return n;
}

async function init() {
  [currentTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const isWeb = currentTab && /^https?:/i.test(currentTab.url || "");
  $("pageTitle").textContent = currentTab?.title || "No page";
  $("pageHost").textContent = isWeb ? new URL(currentTab.url).hostname : "Not a web page";
  $("btnAudio").disabled = $("btnVideo").disabled = !isWeb;

  if (isWeb) {
    const media = (await chrome.storage.session.get(`media:${currentTab.id}`))[`media:${currentTab.id}`] || [];
    if (media.length) {
      const streams = media.filter((m) => m.kind === "stream").length;
      $("detected").textContent =
        `Detected ${media.length} media source${media.length > 1 ? "s" : ""}` +
        (streams ? ` (${streams} stream${streams > 1 ? "s" : ""})` : "") + " — used as a fallback.";
    }
  }

  $("btnAudio").onclick = () => start("audio");
  $("btnVideo").onclick = () => start("video");
  $("openFolder").onclick = (e) => { e.preventDefault(); native({ action: "open_folder" }); };
  $("clear").onclick = (e) => { e.preventDefault(); chrome.runtime.sendMessage({ cmd: "clear" }); };

  render((await chrome.storage.session.get("jobs")).jobs || []);
  chrome.storage.session.onChanged.addListener((changes) => {
    if (changes.jobs) render(changes.jobs.newValue || []);
  });

  chrome.runtime.sendNativeMessage(HOST, { action: "ping" }, (resp) => {
    if (chrome.runtime.lastError || !resp?.ok) {
      $("helperWarn").hidden = false;
      $("footer").textContent = "";
      return;
    }
    $("outDir").textContent = resp.dir.replace(/^\/Users\/[^/]+/, "~");
    $("footer").textContent = `yt-dlp ${resp.ytdlp}`;
  });
}

function start(mode) {
  const btn = mode === "audio" ? $("btnAudio") : $("btnVideo");
  btn.disabled = true;
  setTimeout(() => { btn.disabled = false; }, 1500); // debounce double-clicks
  chrome.runtime.sendMessage({ cmd: "start", mode, tabId: currentTab.id });
}

function native(msg) {
  chrome.runtime.sendNativeMessage(HOST, msg, () => void chrome.runtime.lastError);
}

function render(jobs) {
  const ul = $("jobs");
  ul.replaceChildren();
  $("empty").hidden = jobs.length > 0;
  for (const j of jobs) {
    const icon = el("span", { className: "icon", textContent: j.mode === "audio" ? "🎧" : "🎬" });
    const title = el("span", { className: "title", textContent: j.title, title: j.title });
    const row = el("div", { className: "row" }, [icon, title]);
    const li = el("li", {}, [row]);

    if (j.status === "running") {
      row.append(el("button", { className: "mini", textContent: "Cancel",
        onclick: () => chrome.runtime.sendMessage({ cmd: "cancel", id: j.id }) }));
      const bar = el("div", { className: "bar" + (j.percent == null ? " indet" : "") }, [el("div")]);
      if (j.percent != null) bar.firstChild.style.width = `${Math.min(100, j.percent)}%`;
      li.append(bar);
      const bits = [j.statusText];
      if (j.percent != null) bits.push(`${j.percent.toFixed(0)}%`);
      if (j.speed && !/unknown|N\/A/i.test(j.speed)) bits.push(j.speed);
      if (j.eta && !/unknown|N\/A/i.test(j.eta)) bits.push(`ETA ${j.eta}`);
      li.append(el("div", { className: "meta", textContent: bits.filter(Boolean).join(" · ") }));
    } else if (j.status === "done") {
      row.append(el("button", { className: "mini", textContent: "Show",
        onclick: () => native({ action: "reveal", path: j.path }) }));
      const file = (j.path || "").split("/").pop();
      if (j.warning) {
        li.append(el("div", { className: "meta warn-text", textContent: j.warning }));
        li.append(el("div", { className: "meta", textContent: "Saved · " + file }));
        appendDetails(li, j);
      } else {
        li.append(el("div", { className: "meta ok", textContent: "Saved · " + file }));
      }
    } else if (j.status === "error") {
      li.append(el("div", { className: "meta err", textContent: j.error || "Failed" }));
      appendDetails(li, j);
    } else if (j.status === "cancelled") {
      li.append(el("div", { className: "meta", textContent: "Cancelled" }));
    }
    ul.append(li);
  }
}

// Per-attempt errors (collapsed) plus a button to show the full log in Finder.
function appendDetails(li, j) {
  const box = el("div", { className: "meta details" });
  if (j.details?.length > 1 || (j.details?.length && j.warning)) {
    const d = el("details", {}, [el("summary", { textContent: `What was tried (${j.details.length})` })]);
    for (const a of j.details) {
      d.append(el("div", { className: "attempt", textContent: `${a.label}: ${a.stage}. ${a.error}` }));
    }
    box.append(d);
  }
  if (j.log) {
    box.append(el("a", { href: "#", textContent: "Show log",
      onclick: (e) => { e.preventDefault(); native({ action: "reveal", path: j.log }); } }));
  }
  if (box.childNodes.length) li.append(box);
}

init();
