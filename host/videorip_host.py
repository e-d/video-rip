"""VideoRip native messaging host.

Chrome launches this process when the extension calls connectNative /
sendNativeMessage. Messages use Chrome's native messaging framing: a 4-byte
little-endian length followed by UTF-8 JSON, on stdin/stdout.

Actions:
  {"action": "ping"}                        -> {"ok": true, "ytdlp": "<version>", "dir": "..."}
  {"action": "open_folder"}                 -> opens the output folder in Finder
  {"action": "reveal", "path": "..."}       -> reveals a downloaded file (or job log) in Finder
  {"action": "download", ...}               -> streams progress, then a final "done"/"error"
      mode:     "audio" | "video"
      url:      page URL
      media:    [{"url": ..., "referer": ...}]   media URLs sniffed from the tab (fallbacks)
      browser:  "chrome" | "edge"                 used for --cookies-from-browser retry
"""

import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

OUT_DIR = Path(os.environ.get("VIDEORIP_DIR", Path.home() / "Downloads" / "VideoRip"))
TMP_ROOT = OUT_DIR / ".tmp"
YTDLP = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"
FFMPEG_DIR = os.path.dirname(shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg")

_write_lock = threading.Lock()


# ---------------------------------------------------------------- messaging

def read_message():
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    (length,) = struct.unpack("<I", raw_len)
    data = sys.stdin.buffer.read(length)
    if len(data) < length:
        return None
    return json.loads(data.decode("utf-8"))


def send(msg):
    data = json.dumps(msg).encode("utf-8")
    with _write_lock:
        try:
            sys.stdout.buffer.write(struct.pack("<I", len(data)))
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            pass  # extension went away; keep going quietly


# ---------------------------------------------------------------- helpers

def is_http_url(value):
    return isinstance(value, str) and re.match(r"^https?://", value, re.I) is not None


def ytdlp_version():
    try:
        return subprocess.run([YTDLP, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({exc})"


LOG_DIR = OUT_DIR / ".logs"
LOG_KEEP = 50

# Rank how far an attempt got, so we report the error from the one that got furthest.
STAGE_NONE, STAGE_INFO, STAGE_DOWNLOADING, STAGE_DOWNLOADED, STAGE_POSTPROCESS = range(5)
STAGE_NAMES = {
    STAGE_NONE: "failed before finding a video",
    STAGE_INFO: "found the video but couldn't start downloading",
    STAGE_DOWNLOADING: "failed while downloading",
    STAGE_DOWNLOADED: "downloaded, then failed",
    STAGE_POSTPROCESS: "downloaded, then conversion failed",
}

# yt-dlp postprocessor tags, e.g. "[ExtractAudio] Destination: ..."
PP_TAG_RE = re.compile(
    r"^\[(ExtractAudio|Merger|VideoConvertor|VideoRemuxer|EmbedThumbnail|Metadata|"
    r"ThumbnailsConvertor|FixupM3u8|FixupM4a|FixupStretched|FixupDuration|FixupTimestamp|ffmpeg|MoveFiles)\]"
)
# Postprocessors that are nice-to-have; if one of these fails we retry without them.
OPTIONAL_PP = {"EmbedThumbnail", "Metadata", "ThumbnailsConvertor"}
# What counts as a usable media file when rescuing a failed conversion.
MEDIA_EXTS = {".mp4", ".m4a", ".m4v", ".webm", ".mkv", ".mov", ".mp3", ".aac", ".opus", ".ogg",
              ".oga", ".flac", ".wav", ".ts", ".flv", ".3gp"}


def safe_name(title):
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", title or "").strip(" .")
    return (name[:150] or "video").replace("%", "%%")


def build_args(mode, url, tmp_dir, referer=None, cookies_browser=None, name=None, plain=False):
    """plain=True drops the optional embedding steps (cover art, metadata)."""
    args = [
        YTDLP,
        "--verbose",  # full detail goes to the job log; we only parse a few marker lines
        "--no-playlist",
        "--newline",
        "--progress",
        "--no-colors",
        "--ffmpeg-location", FFMPEG_DIR,
        "-P", str(OUT_DIR),
        "-P", f"temp:{tmp_dir}",
        "-o", f"{safe_name(name)}.%(ext)s" if name else "%(title).150B [%(id)s].%(ext)s",
        "--progress-template", "download:VRPROG %(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
        "--print", "video:VRTITLE %(title)s",
        "--print", "after_move:VRFILE %(filepath)s",
        "--no-simulate",
    ]
    if not plain:
        args += ["--embed-metadata"]
    if mode == "audio":
        args += ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "2"]
        if not plain:
            args += ["--embed-thumbnail"]
    else:
        args += ["-f", "bv*+ba/b", "-S", "res:1080,vcodec:h264,acodec:m4a", "--merge-output-format", "mp4"]
    if referer:
        args += ["--referer", referer]
    if cookies_browser:
        args += ["--cookies-from-browser", cookies_browser]
    args += ["--", url]
    return args


class Job:
    def __init__(self):
        self.proc = None
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class JobLog:
    """Full yt-dlp output for one job, in OUT_DIR/.logs/, so failures can be diagnosed."""

    def __init__(self, title):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title or "job").strip("-")[:60] or "job"
        self.path = LOG_DIR / f"{stamp}_{slug}.log"
        self.f = open(self.path, "w", encoding="utf-8", errors="replace")
        self._prune()

    def _prune(self):
        logs = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in logs[LOG_KEEP:]:
            old.unlink(missing_ok=True)

    def write(self, text):
        try:
            self.f.write(text.rstrip("\n") + "\n")
            self.f.flush()
        except OSError:
            pass

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass


def run_attempt(job, args, label, log):
    """Run one yt-dlp invocation and report how far it got."""
    send({"type": "status", "text": label})
    log.write(f"\n===== {label}\n$ " + " ".join(shlex.quote(a) for a in args))
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    job.proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env,
        encoding="utf-8", errors="replace",
    )
    res = {"filepath": None, "title": None, "error": "", "stage": STAGE_NONE, "last_pp": None}
    last_logged_pct = -10.0
    for line in job.proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("VRPROG "):
            pct, _, rest = line[7:].partition("|")
            speed, _, eta = rest.partition("|")
            try:
                pct_num = float(pct.strip().rstrip("%"))
            except ValueError:
                pct_num = None
            res["stage"] = max(res["stage"], STAGE_DOWNLOADING)
            if pct_num is not None and pct_num >= 100:
                res["stage"] = max(res["stage"], STAGE_DOWNLOADED)
            if pct_num is not None and (pct_num - last_logged_pct >= 10 or pct_num >= 100):
                last_logged_pct = pct_num
                log.write(f"[progress] {pct.strip()} {speed.strip()} ETA {eta.strip()}")
            send({"type": "progress", "percent": pct_num, "speed": speed.strip(), "eta": eta.strip()})
            continue
        log.write(line)
        if line.startswith("VRTITLE "):
            res["title"] = line[8:]
            res["stage"] = max(res["stage"], STAGE_INFO)
            send({"type": "title", "title": res["title"]})
        elif line.startswith("VRFILE "):
            res["filepath"] = line[7:]
        elif line.startswith("ERROR:"):
            res["error"] = line[6:].strip()
        elif "has already been downloaded" in line:
            res["stage"] = max(res["stage"], STAGE_DOWNLOADED)
        else:
            m = PP_TAG_RE.match(line)
            if m and m.group(1) != "MoveFiles":
                res["stage"] = max(res["stage"], STAGE_POSTPROCESS)
                res["last_pp"] = m.group(1)
                if m.group(1) in ("ExtractAudio", "Merger", "VideoConvertor", "VideoRemuxer"):
                    send({"type": "status", "text": "Converting…"})
    job.proc.wait()
    if job.proc.returncode != 0 and not res["error"]:
        res["error"] = f"yt-dlp exited with code {job.proc.returncode}"
    if res["filepath"] and not os.path.exists(res["filepath"]):
        res["filepath"] = None
    if res["filepath"] and job.proc.returncode != 0:
        # The file was moved into place but something after that failed (rare). Keep it.
        res["error"] = ""
    log.write(f"----- exit {job.proc.returncode}; stage: {STAGE_NAMES[res['stage']]}; "
              f"file: {res['filepath']}; error: {res['error'] or '-'}")
    return res


def unique_path(path):
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{path.stem} {uuid.uuid4().hex[:6]}{path.suffix}")


def rescue_file(attempt_dir, title, mode, failed_pp, log):
    """If an attempt fully downloaded a file but conversion failed, move the original
    (unconverted) file into the output folder instead of deleting it. Returns the path."""
    candidates = [
        p for p in attempt_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS
        and ".part" not in p.name and not p.name.endswith((".ytdl", ".temp"))
        and ".temp." not in p.name and p.stat().st_size >= 50 * 1024
    ]
    if not candidates:
        return None
    target_ext = ".mp3" if mode == "audio" else ".mp4"
    if failed_pp in ("ExtractAudio", "VideoConvertor"):
        # The half-written converted file can't be trusted; prefer the source.
        sources = [p for p in candidates if p.suffix.lower() != target_ext]
        candidates = sources or candidates
    # A failed merge leaves separate streams (name.f137.mp4 + name.f140.m4a): keep them all.
    split = [p for p in candidates if re.search(r"\.f[\w-]+\.[^.]+$", p.name)]
    to_move = split if len(split) > 1 else [max(candidates, key=lambda p: p.stat().st_size)]
    base = safe_name(title).replace("%%", "%")
    moved = []
    for src in sorted(to_move, key=lambda p: p.stat().st_size, reverse=True):
        part = re.search(r"\.(f[\w-]+)\.[^.]+$", src.name)
        suffix = f" (unconverted {part.group(1)})" if len(to_move) > 1 and part else " (unconverted)"
        dest = unique_path(OUT_DIR / f"{base}{suffix}{src.suffix.lower()}")
        shutil.move(str(src), dest)
        log.write(f"Rescued original download -> {dest}")
        moved.append(str(dest))
    return moved[0]


def do_download(job, msg):
    mode = "audio" if msg.get("mode") == "audio" else "video"
    page_url = msg.get("url")
    browser = msg.get("browser") if msg.get("browser") in ("chrome", "edge", "brave", "chromium") else "chrome"
    page_title = msg.get("title") if isinstance(msg.get("title"), str) else None
    media = [m for m in (msg.get("media") or []) if isinstance(m, dict) and is_http_url(m.get("url"))][:5]

    if not is_http_url(page_url) and not media:
        send({"type": "error", "error": "This tab doesn't have a web URL to download from."})
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    job_tmp = TMP_ROOT / uuid.uuid4().hex
    log = JobLog(page_title or page_url)
    log.write(f"VideoRip job  mode={mode}  yt-dlp={ytdlp_version()}")
    log.write(f"page: {page_url}\ntitle: {page_title}")
    for m in media:
        log.write(f"sniffed: {m.get('url')}  (referer {m.get('referer')})")

    # (label, kind, args-builder taking (tmp_dir, plain))
    attempts = []
    if is_http_url(page_url):
        attempts.append(("Fetching from page…", "page",
                         lambda d, plain: build_args(mode, page_url, d, plain=plain)))
        attempts.append((f"Retrying with your {browser.title()} login…", "cookies",
                         lambda d, plain: build_args(mode, page_url, d, cookies_browser=browser, plain=plain)))
    for i, m in enumerate(media, 1):
        referer = m.get("referer") if is_http_url(m.get("referer")) else page_url if is_http_url(page_url) else None
        attempts.append((f"Trying detected stream {i} of {len(media)}…", "media",
                         lambda d, plain, u=m["url"], r=referer: build_args(mode, u, d, referer=r, name=page_title, plain=plain)))

    results = []  # (order, label, kind, res, attempt_dir)
    title = None
    log_path = str(log.path)
    try:
        for order, (label, kind, make_args) in enumerate(attempts):
            if job.cancelled:
                break
            if kind == "cookies" and results and "unsupported url" in results[-1][3]["error"].lower():
                log.write("\n===== Skipping login retry (site not supported by yt-dlp)")
                continue
            attempt_dir = job_tmp / f"attempt{order}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            res = run_attempt(job, make_args(attempt_dir, False), label, log)

            # Fix: if an optional step (cover art / metadata) broke conversion, retry without it.
            # The download is reused from attempt_dir, so this is quick.
            if (not job.cancelled and not res["filepath"] and res["stage"] == STAGE_POSTPROCESS
                    and res["last_pp"] in OPTIONAL_PP):
                res = run_attempt(job, make_args(attempt_dir, True),
                                  "Retrying without cover art / tags…", log)

            title = res["title"] or title
            if job.cancelled:
                break
            if res["filepath"]:
                if kind == "media" and page_title:
                    title = page_title
                log.write(f"\n===== DONE: {res['filepath']}")
                send({"type": "done", "path": res["filepath"],
                      "title": title or os.path.basename(res["filepath"]), "log": log_path})
                return
            results.append((order, label, kind, res, attempt_dir))

        if job.cancelled:
            log.write("\n===== CANCELLED")
            send({"type": "cancelled"})
            return

        # Report the error from the attempt that got furthest (ties -> the later attempt).
        ranked = sorted(results, key=lambda r: (r[3]["stage"], r[0]), reverse=True)
        details = [{"label": lbl.rstrip("…"), "error": res["error"] or "Unknown error",
                    "stage": STAGE_NAMES[res["stage"]]} for _, lbl, _, res, _ in results]

        # If something fully downloaded but couldn't be converted, keep the original file.
        for _, _, kind, res, attempt_dir in ranked:
            if res["stage"] < STAGE_DOWNLOADED:
                break
            name = page_title if (kind == "media" and page_title) else (res["title"] or title or page_title)
            rescued = rescue_file(attempt_dir, name, mode, res["last_pp"], log)
            if rescued:
                want = "MP3" if mode == "audio" else "MP4"
                warning = f"Couldn't convert to {want} ({res['error'] or 'unknown error'}). Saved the original file instead."
                log.write(f"\n===== RESCUED: {rescued}")
                send({"type": "done", "path": rescued, "title": name or os.path.basename(rescued),
                      "warning": warning, "log": log_path, "details": details})
                return

        best = ranked[0][3] if ranked else {"error": "Nothing to try", "stage": STAGE_NONE}
        error = best["error"] or "Unknown error"
        if best["stage"] >= STAGE_DOWNLOADING:
            error = f"{error} ({STAGE_NAMES[best['stage']]})"
        log.write(f"\n===== FAILED: {error}")
        send({"type": "error", "error": error, "details": details, "log": log_path})
    finally:
        log.close()
        shutil.rmtree(job_tmp, ignore_errors=True)


# ---------------------------------------------------------------- main

def main():
    msg = read_message()
    if msg is None:
        return
    action = msg.get("action")

    if action == "ping":
        send({"ok": True, "ytdlp": ytdlp_version(), "dir": str(OUT_DIR)})
        return

    if action == "open_folder":
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(OUT_DIR)])
        send({"ok": True})
        return

    if action == "reveal":
        path = msg.get("path")
        # Only reveal files inside the output folder.
        try:
            resolved = Path(path).resolve()
            ok = resolved.is_relative_to(OUT_DIR.resolve()) and resolved.exists()
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            subprocess.run(["open", "-R", str(resolved)])
        else:
            subprocess.run(["open", str(OUT_DIR)])
        send({"ok": True})
        return

    if action == "download":
        job = Job()

        def watch_stdin():
            # Any further message (e.g. {"action": "cancel"}) or EOF (port
            # disconnected) cancels the download.
            while True:
                m = read_message()
                if m is None or m.get("action") == "cancel":
                    job.cancel()
                    return

        threading.Thread(target=watch_stdin, daemon=True).start()
        try:
            do_download(job, msg)
        except Exception as exc:  # noqa: BLE001
            send({"type": "error", "error": f"Helper crashed: {exc}"})
        return

    send({"ok": False, "error": f"Unknown action: {action}"})


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    # Hard exit: the stdin watcher thread may be blocked in read(), which
    # makes normal interpreter shutdown abort noisily.
    os._exit(0)
