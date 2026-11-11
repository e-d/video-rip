#!/bin/bash
# Installs the VideoRip native helper so the browser extension can talk to it.
# Safe to re-run any time (e.g. after moving this folder).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
HOST_NAME="com.videorip.host"
EXT_ID="addnflenpkomihmmmfpjaalfknopmhac"   # fixed by the "key" in extension/manifest.json
WRAPPER="$DIR/host/videorip-host"

# 1. Dependencies --------------------------------------------------------------
if ! command -v brew >/dev/null; then
  echo "Homebrew is required: https://brew.sh" >&2; exit 1
fi
for pkg in yt-dlp ffmpeg; do
  if ! brew list --formula "$pkg" >/dev/null 2>&1; then
    echo "Installing $pkg…"; brew install "$pkg"
  fi
done
PY="$(command -v python3)"
BREW_PREFIX="$(brew --prefix)"

# 2. Wrapper (Chrome launches hosts with a bare PATH, so pin everything) --------
cat > "$WRAPPER" <<EOF
#!/bin/bash
export PATH="$BREW_PREFIX/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec "$PY" -I "$DIR/host/videorip_host.py" "\$@"
EOF
chmod 755 "$WRAPPER"

# 3. Register the host with every Chromium browser that's present ---------------
MANIFEST=$(cat <<EOF
{
  "name": "$HOST_NAME",
  "description": "VideoRip download helper",
  "path": "$WRAPPER",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
EOF
)
SUPPORT="$HOME/Library/Application Support"
registered=0
for browser in "Google/Chrome" "Google/Chrome Beta" "Microsoft Edge" "BraveSoftware/Brave-Browser" "Chromium" "Arc/User Data"; do
  if [ -d "$SUPPORT/$browser" ]; then
    mkdir -p "$SUPPORT/$browser/NativeMessagingHosts"
    echo "$MANIFEST" > "$SUPPORT/$browser/NativeMessagingHosts/$HOST_NAME.json"
    echo "Registered helper for: $browser"
    registered=1
  fi
done
[ "$registered" = 1 ] || echo "Warning: no Chromium browser profile folder found." >&2

mkdir -p "$HOME/Downloads/VideoRip"

cat <<EOF

✅ Helper installed. yt-dlp $(yt-dlp --version)

Next (one time):
  1. Open chrome://extensions
  2. Turn on "Developer mode" (top right)
  3. Click "Load unpacked" and choose:  $DIR/extension
  4. Pin the VideoRip icon to your toolbar.

Files are saved to ~/Downloads/VideoRip
EOF
