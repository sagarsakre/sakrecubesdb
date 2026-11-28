#!/usr/bin/env bash
set -euo pipefail

# Export Instagram cookies from local Chrome and sync to Raspberry Pi.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/sagarsakre/sakrecubesdb/main/scripts/sync_ig_cookies.sh | bash
#   ./scripts/sync_ig_cookies.sh [user@pi-host]

DEFAULT_PI_USER="admin"
DEFAULT_PI_HOST="192.168.0.118"
PI_APP_DIR="${PI_APP_DIR:-~/sakrecubesdb}"

if [[ -n "${1:-}" ]]; then
  PI_TARGET="$1"
elif [[ -n "${PI_SSH_TARGET:-}" ]]; then
  PI_TARGET="$PI_SSH_TARGET"
elif [[ -r /dev/tty && -w /dev/tty ]]; then
  read -r -p "🖥️  Pi username [${DEFAULT_PI_USER}]: " INPUT_USER </dev/tty
  read -r -p "🌐 Pi IP/host [${DEFAULT_PI_HOST}]: " INPUT_HOST </dev/tty
  PI_USER="${INPUT_USER:-$DEFAULT_PI_USER}"
  PI_HOST="${INPUT_HOST:-$DEFAULT_PI_HOST}"
  PI_TARGET="${PI_USER}@${PI_HOST}"
else
  PI_TARGET="${DEFAULT_PI_USER}@${DEFAULT_PI_HOST}"
fi

if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]:-}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
  COOKIE_FILE="$APP_DIR/data/ig_cookies.txt"
  mkdir -p "$APP_DIR/data"
else
  COOKIE_FILE="/tmp/ig_cookies_$$.txt"
  trap 'rm -f "$COOKIE_FILE"' EXIT
fi

echo "🍪 Exporting Instagram cookies from Chrome..."
# Ensure target doesn't exist as a 0-byte file before yt-dlp writes to it.
rm -f "$COOKIE_FILE"
# yt-dlp may exit non-zero on carousel photo slides even after writing --cookies, so allow that and validate below.
yt-dlp --cookies-from-browser chrome --cookies "$COOKIE_FILE" --skip-download --no-warnings "https://www.instagram.com/p/DSPhRkGksIR/" >/dev/null 2>&1 || true

if [[ ! -s "$COOKIE_FILE" ]]; then
  echo "❌ Failed to export cookies from Chrome. Make sure Chrome is installed and try again."
  exit 1
fi

echo "🧹 Filtering to Instagram cookies only..."
python3 - "$COOKIE_FILE" <<'PY'
import sys
p = sys.argv[1]
lines = open(p, encoding="utf-8", errors="ignore").read().splitlines()
kept = [l for l in lines if l.startswith("#") or "instagram.com" in l.split("\t")[0]]
open(p, "w", encoding="utf-8").write("\n".join(kept) + "\n")
has_session = any("\tsessionid\t" in l for l in kept)
print(f"   Kept {len(kept)} lines (sessionid present: {has_session})")
if not has_session:
    sys.exit(2)
PY

status=$?
if [[ $status -eq 2 ]]; then
  echo "❌ No Instagram 'sessionid' cookie found in Chrome."
  echo "   Open https://www.instagram.com in Chrome, log in, and re-run this script."
  exit 1
fi

echo "📡 Syncing cookies to Pi ($PI_TARGET:$PI_APP_DIR/data/ig_cookies.txt)..."
scp "$COOKIE_FILE" "$PI_TARGET:$PI_APP_DIR/data/ig_cookies.txt"

echo "✅ Done! Instagram cookies are synced on both this Mac and the Pi."
