#!/usr/bin/env python3
"""
Queue Manager & Worker for Sakrecubes Reel Ingestion:
Enqueues Instagram reel URLs with fast metadata discovery and processes them sequentially in the background.
Sends status updates directly to Telegram.
"""

import os
import sys
import json
import time
import sqlite3
import argparse
import subprocess
import urllib.request
import urllib.parse
import html
import re
from pathlib import Path
from datetime import datetime, timezone

APP_DIR = Path(__file__).resolve().parent.parent
DB_PATH = APP_DIR / "data" / "queue.db"
ENV_FILE = APP_DIR / ".env"

# Load local .env if present
if ENV_FILE.exists():
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

def get_telegram_config():
    """Finds Telegram Bot Token and Owner Chat ID."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    owner_id = os.environ.get("TELEGRAM_OWNER_USER_ID", "541881114")

    # Check systemd credential directory if on Pi
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir and not token:
        token_path = Path(cred_dir) / "telegram-token"
        if token_path.exists():
            token = token_path.read_text().strip()

    # Fallback to /etc/sakrecubes-telegram/ if readable
    if not token:
        etc_token = Path("/etc/sakrecubes-telegram/telegram-token")
        if etc_token.exists():
            try:
                token = etc_token.read_text().strip()
            except PermissionError:
                pass

    return token, owner_id

def send_telegram(text: str, chat_id: str = None):
    token, default_chat_id = get_telegram_config()
    target_chat = chat_id or default_chat_id
    if not token or not target_chat:
        print(f"[Telegram Notice - not sent, missing token]: {text}", file=sys.stderr, flush=True)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status == 200
    except Exception as e:
        print(f"Failed to send Telegram message: {e}", file=sys.stderr, flush=True)
        return False

def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                shortcode TEXT NOT NULL,
                chat_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error TEXT,
                result TEXT,
                caption TEXT,
                creator TEXT,
                reel_label TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status)")
        # Migrate table if columns missing
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(queue)").fetchall()}
        for col in ["caption", "creator", "reel_label"]:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE queue ADD COLUMN {col} TEXT")
    return conn

def fetch_reel_metadata(url: str):
    clean_url = re.sub(r"\?.*$", "", url).rstrip("/") + "/"
    m = re.search(r"/(?:reel|reels|p)/([A-Za-z0-9_-]+)", clean_url)
    shortcode = m.group(1) if m else "reel"

    caption = ""
    creator = ""
    try:
        oembed_url = f"https://www.instagram.com/api/v1/oembed/?url={clean_url}"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            creator = data.get("author_name", "").strip()
            raw_title = data.get("title", "").strip()
            for line in raw_title.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    caption = line
                    break
    except Exception as e:
        print(f"[queue_manager] oembed lookup notice for {shortcode}: {e}", file=sys.stderr, flush=True)

    if caption and len(caption) > 70:
        caption = caption[:67] + "..."

    if caption and creator:
        reel_label = f"{caption} | Reel by @{creator}"
    elif creator:
        reel_label = f"Reel by @{creator}"
    elif caption:
        reel_label = caption
    else:
        reel_label = f"Reel {shortcode}"

    return {
        "shortcode": shortcode,
        "url": clean_url,
        "caption": caption,
        "creator": creator,
        "reel_label": reel_label,
    }

def enqueue(url: str, chat_id: str = None):
    conn = get_db()
    meta = fetch_reel_metadata(url)
    shortcode = meta["shortcode"]
    clean_url = meta["url"]
    caption = meta["caption"]
    creator = meta["creator"]
    reel_label = meta["reel_label"]
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, status, reel_label FROM queue WHERE shortcode = ? AND status IN ('pending', 'processing')",
            (shortcode,)
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "SELECT COUNT(*) FROM queue WHERE status IN ('pending', 'processing') AND id <= ?",
                (existing[0],)
            )
            pos = cur.fetchone()[0]
            return {
                "status": "already_queued",
                "job_id": existing[0],
                "shortcode": shortcode,
                "url": clean_url,
                "caption": caption,
                "creator": creator,
                "reel_label": existing[2] or reel_label,
                "position": pos
            }

        cur.execute(
            """INSERT INTO queue (url, shortcode, chat_id, status, created_at, caption, creator, reel_label)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (clean_url, shortcode, chat_id, now_iso, caption, creator, reel_label)
        )
        job_id = cur.lastrowid

        cur.execute(
            "SELECT COUNT(*) FROM queue WHERE status IN ('pending', 'processing') AND id <= ?",
            (job_id,)
        )
        position = cur.fetchone()[0]

    return {
        "status": "queued",
        "job_id": job_id,
        "shortcode": shortcode,
        "url": clean_url,
        "caption": caption,
        "creator": creator,
        "reel_label": reel_label,
        "position": position
    }

def process_one_job():
    conn = get_db()
    with conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, url, shortcode, chat_id, caption, creator, reel_label FROM queue WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
        )
        job = cur.fetchone()
        if not job:
            return False

        job_id, url, shortcode, chat_id, caption, creator, reel_label = job
        started_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur.execute(
            "UPDATE queue SET status = 'processing', started_at = ? WHERE id = ?",
            (started_iso, job_id)
        )

    clean_url = re.sub(r"\?.*$", "", url).rstrip("/") + "/"
    print(f"\n[Worker] 🎬 Starting Job #{job_id}: {shortcode} ({clean_url})", flush=True)
    ingest_script = APP_DIR / "scripts" / "ingest_reel.py"
    cmd = [sys.executable, str(ingest_script), clean_url, "--clean"]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        finished_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if proc.returncode == 0:
            result_json = "{}"
            for line in proc.stdout.splitlines():
                if "SUCCESS:" in line or '"status": "success"' in line:
                    try:
                        start = proc.stdout.find('{\n  "status": "success"')
                        if start != -1:
                            end = proc.stdout.rfind('}') + 1
                            result_json = proc.stdout[start:end]
                    except Exception:
                        pass
                    break

            try:
                res_obj = json.loads(result_json)
                title = res_obj.get("title", shortcode)
                size_str = res_obj.get("total_size", "")
                shots = res_obj.get("shots_count", "")
                post_type = res_obj.get("type", "reel")
            except Exception:
                title = shortcode
                size_str = ""
                shots = ""
                post_type = "reel"

            with conn:
                conn.execute(
                    "UPDATE queue SET status = 'completed', finished_at = ?, result = ? WHERE id = ?",
                    (finished_iso, result_json, job_id)
                )

            final_label = reel_label
            if not final_label or final_label.startswith("Reel "):
                if creator:
                    final_label = f"{title[:70]} | {post_type.title()} by @{creator}"
                else:
                    final_label = title[:70]
            escaped_label = html.escape(final_label)

            # Send Telegram success notification matching the requested format
            unit_lbl = "photos" if post_type in ("carousel", "photo") else "shots"
            msg_lines = [
                f"🟢 {post_type.title()} ingestion finished on the Pi.",
                f'{post_type.title()}: <a href="{clean_url}">{escaped_label}</a>',
                f"Link: {clean_url}"
            ]
            if size_str and shots:
                msg_lines.append(f"📦 Size: {size_str} ({shots} {unit_lbl})")
            elif size_str:
                msg_lines.append(f"📦 Size: {size_str}")
            msg_lines.append("\n⚡ Available on your Trip Board & Library Manager!")
            msg = "\n".join(msg_lines)

            send_telegram(msg, chat_id)
            print(f"[Worker] ✅ Job #{job_id} completed successfully.", flush=True)
        else:
            err_msg = proc.stdout[-500:] if proc.stdout else "Unknown error"
            with conn:
                conn.execute(
                    "UPDATE queue SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                    (finished_iso, err_msg, job_id)
                )
            final_label = reel_label or f"Reel {shortcode}"
            escaped_label = html.escape(final_label)
            escaped_err = html.escape(err_msg[:200])
            full_out = (proc.stdout or "").lower()
            is_cookie_err = any(k in full_out for k in (
                "ig_cookie_auth_required",
                "no video formats found",
                "login required",
                "challenge_required",
                "cookies",
                "401",
                "403",
                "rate-limit",
            ))
            cookie_help = ""
            if is_cookie_err:
                cookie_help = (
                    "\n\n🍪 <b>Instagram Cookies Expired or Missing</b>\n"
                    "To refresh from your Mac:\n"
                    "1. Open Chrome on your Mac and make sure you're logged into Instagram.\n"
                    "2. Run this command in Terminal:\n"
                    "<code>curl -fsSL https://raw.githubusercontent.com/sagarsakre/sakrecubesdb/main/scripts/sync_ig_cookies.sh | bash</code>\n"
                    "3. Re-send the Instagram link here!"
                )
            fail_msg = (
                f"🔴 Reel ingestion failed on the Pi.\n"
                f'Reel: <a href="{clean_url}">{escaped_label}</a>\n'
                f"Link: {clean_url}\n"
                f"⚠️ Error: <code>{escaped_err}</code>"
                f"{cookie_help}"
            )
            send_telegram(fail_msg, chat_id)
            print(f"[Worker] ❌ Job #{job_id} failed with exit code {proc.returncode}.", flush=True)

    except Exception as exc:
        finished_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with conn:
            conn.execute(
                "UPDATE queue SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (finished_iso, str(exc), job_id)
            )
        final_label = reel_label or f"Reel {shortcode}"
        escaped_label = html.escape(final_label)
        escaped_exc = html.escape(str(exc)[:200])
        fail_msg = (
            f"🔴 Reel ingestion failed on the Pi.\n"
            f'Reel: <a href="{clean_url}">{escaped_label}</a>\n'
            f"Link: {clean_url}\n"
            f"⚠️ Error: <code>{escaped_exc}</code>"
        )
        send_telegram(fail_msg, chat_id)
        print(f"[Worker] 💥 Job #{job_id} encountered exception: {exc}", flush=True)

    return True

def run_worker():
    print("=" * 60, flush=True)
    print("🚀 Sakrecubes Reel Ingestion Queue Worker started.", flush=True)
    print(f"📁 Database: {DB_PATH}", flush=True)
    print("=" * 60, flush=True)
    while True:
        had_work = process_one_job()
        if not had_work:
            time.sleep(2)

def list_status():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, shortcode, status, reel_label, created_at, started_at, finished_at FROM queue ORDER BY id DESC LIMIT 15")
    rows = cur.fetchall()
    print(f"📊 Queue Status ({len(rows)} recent jobs):")
    for r in rows:
        label = r[3] or r[1]
        print(f"  #{r[0]:03d} [{r[2].upper():<10}] {label[:50]} (Created: {r[4]})")

def main():
    parser = argparse.ArgumentParser(description="Sakrecubes Reel Queue Manager")
    subparsers = parser.add_subparsers(dest="command")

    # enqueue
    p_enqueue = subparsers.add_parser("enqueue", help="Add a reel to queue")
    p_enqueue.add_argument("url", help="Instagram reel URL")
    p_enqueue.add_argument("--chat-id", help="Telegram chat ID for updates")

    # worker
    subparsers.add_parser("worker", help="Run background queue processing daemon")

    # status
    subparsers.add_parser("status", help="Print recent queue status")

    args = parser.parse_args()

    if args.command == "enqueue":
        res = enqueue(args.url, args.chat_id)
        print(json.dumps(res))
    elif args.command == "worker":
        run_worker()
    elif args.command == "status":
        list_status()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
