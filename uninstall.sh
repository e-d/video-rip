#!/bin/bash
# Removes the VideoRip helper registration. Leaves your downloads and yt-dlp/ffmpeg alone.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SUPPORT="$HOME/Library/Application Support"
for browser in "Google/Chrome" "Google/Chrome Beta" "Microsoft Edge" "BraveSoftware/Brave-Browser" "Chromium" "Arc/User Data"; do
  f="$SUPPORT/$browser/NativeMessagingHosts/com.videorip.host.json"
  [ -f "$f" ] && rm "$f" && echo "Removed $f"
done
rm -f "$DIR/host/videorip-host"
echo "Done. Remove the extension itself from chrome://extensions."
