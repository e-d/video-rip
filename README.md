# VideoRip

A Chrome extension and a small helper on your Mac. Click the toolbar icon on any page with a video, then choose **Save audio (MP3)** or **Save video (MP4)**. Files go to `~/Downloads/VideoRip`.

The downloading is done by [yt-dlp](https://github.com/yt-dlp/yt-dlp) (YouTube and 1000+ other sites) and ffmpeg. The browser can't do it alone.

## Setup (one time)

```bash
./install.sh
```

Then in Chrome:

1. Go to `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and pick the `extension/` folder.
4. Pin VideoRip to the toolbar.

Edge and Brave also work: load the same folder at `edge://extensions` or `brave://extensions`.

## Ways to use it

- **Toolbar popup**: Save audio or Save video, with progress, Cancel, and Show in Finder.
- **Right-click** a page, a video, or a **link** to a video: "VideoRip: save audio / video".
- **Keyboard**: `⌥⇧A` saves audio and `⌥⇧V` saves video. You can change these at `chrome://extensions/shortcuts`.
- A notification appears when a file is done. Click it to show the file in Finder.

## How it finds the video

The helper tries these in order and stops at the first one that works:

1. **The page URL** through yt-dlp. This covers YouTube, Vimeo, X, Reddit, podcasts, news sites, and many more.
2. **The page URL again with your browser's login cookies**, for videos that need you to be signed in. The first time, macOS asks to let it read "Chrome Safe Storage". Click *Always Allow*.
3. **Video streams the extension saw while the page played**, such as HLS `.m3u8`, DASH `.mpd`, or direct `.mp4`/`.mp3` files. This covers custom players that yt-dlp doesn't recognize. **Tip:** on an unusual site, press play for a few seconds before you click Save. The popup shows "Detected N media sources" once it has seen some.

## What it can't do

- DRM-protected streams such as Netflix, Disney+, Spotify, and most paid streaming services.
- Videos that only exist as `blob:` URLs and that yt-dlp doesn't support, unless you play them first so the stream is picked up (step 3).

## When something fails

- The popup shows the error from the attempt that **got furthest**. Click **What was tried** to see every attempt.
- If a download finished but converting it to MP3 or MP4 failed, the original file is kept as `<title> (unconverted).<ext>`. It still plays, and VLC or IINA will play anything. If cover art or tags are what broke, it retries without them first.
- **Show log** opens the full yt-dlp output for that job. Logs are in `~/Downloads/VideoRip/.logs/` (the 50 most recent are kept).

## When a site stops working

Sites change often. Updating yt-dlp usually fixes it:

```bash
brew upgrade yt-dlp
```

## Details

| | |
|---|---|
| Audio | MP3, VBR ~190 kbps, with title/artist metadata and cover art |
| Video | MP4 (H.264/AAC preferred, up to 1080p) so it plays in QuickTime and on iPhone |
| Playlists | Only the video you're on is downloaded, never the whole playlist |
| Output folder | `~/Downloads/VideoRip`. To change it, set `VIDEORIP_DIR` in the `host/videorip-host` wrapper |

## Files

```
extension/          Chrome MV3 extension (popup, background service worker, icons)
host/videorip_host.py   Native-messaging helper that runs yt-dlp and reports progress
install.sh          Installs yt-dlp/ffmpeg via Homebrew and registers the helper
uninstall.sh        Removes the helper registration
tools/make_icons.py Regenerates the icons
```

The extension talks to the helper through Chrome **native messaging**. No local server or open port is involved, and only this extension's fixed ID can start the helper. If you move this folder, run `./install.sh` again.

## License

MIT. See [LICENSE](LICENSE).
