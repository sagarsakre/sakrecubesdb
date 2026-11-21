#!/usr/bin/env python3
"""
Queue Manager & Worker for Sakrecubes Reel Ingestion:
Enqueues Instagram reel URLs and processes them sequentially in the background.
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
        print(f"[Telegram Notice - not sent, missing token]: {text}", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML"
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
        print(f"Failed to send Telegram message: {e}", file=sys.stderr)
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
                result TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status)")
    return conn

def extract_shortcode(url: str) -> str:
    import re
    m = re.search(r"/(?:reel|reels|p)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else "reel"

def enqueue(url: str, chat_id: str = None):
    conn = get_db()
    shortcode = extract_shortcode(url)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with conn:
        # Check if already active or pending
        cur = conn.cursor()
        cur.execute(
            "SELECT id, status FROM queue WHERE shortcode = ? AND status IN ('pending', 'processing')",
            (shortcode,)
        )
        existing = cur.fetchone()
        if existing:
            # Count position
            cur.execute(
                "SELECT COUNT(*) FROM queue WHERE status IN ('pending', 'processing') AND id <= ?",
                (existing[0],)
            )
            pos = cur.fetchone()[0]
            return {
                "status": "already_queued",
                "job_id": existing[0],
                "shortcode": shortcode,
                "position": pos
            }

        cur.execute(
            "INSERT INTO queue (url, shortcode, chat_id, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (url, shortcode, chat_id, now_iso)
        )
        job_id = cur.lastrowid

        # Calculate position (number of active jobs up to this one)
        cur.execute(
            "SELECT COUNT(*) FROM queue WHERE status IN ('pending', 'processing') AND id <= ?",
            (job_id,)
        )
        position = cur.fetchone()[0]

    return {
        "status": "queued",
        "job_id": job_id,
        "shortcode": shortcode,
        "position": position
    }

def process_one_job():
    conn = get_db()
    with conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, url, shortcode, chat_id FROM queue WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
        )
        job = cur.fetchone()
        if not job:
            return False

        job_id, url, shortcode, chat_id = job
        started_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur.execute(
            "UPDATE queue SET status = 'processing', started_at = ? WHERE id = ?",
            (started_iso, job_id)
        )

    print(f"\n[Worker] 🎬 Starting Job #{job_id}: {shortcode} ({url})", flush=True)
    ingest_script = APP_DIR / "scripts" / "ingest_reel.py"
    cmd = [sys.executable, str(ingest_script), url, "--clean"]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        finished_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if proc.returncode == 0:
            # Extract success JSON output if available
            result_json = "{}"
            for line in proc.stdout.splitlines():
                if "SUCCESS:" in line or '"status": "success"' in line:
                    try:
                        # find json object in output
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
            except Exception:
                title = shortcode
                size_str = ""
                shots = ""

            with conn:
                conn.execute(
                    "UPDATE queue SET status = 'completed', finished_at = ?, result = ? WHERE id = ?",
                    (finished_iso, result_json, job_id)
                )

            # Send Telegram success notification
            msg = (
                f"🟢 <b>Reel ingestion finished on the Pi!</b>\n\n"
                f"🎬 <b>Title:</b> {title}\n"
                f"🆔 <code>{shortcode}</code>\n"
                f"{f'📦 <b>Size:</b> {size_str} ({shots} shots)' if size_str else ''}\n\n"
                f"⚡ Available on your Trip Board & Library Manager!"
            )
            send_telegram(msg, chat_id)
            print(f"[Worker] ✅ Job #{job_id} completed successfully.", flush=True)
        else:
            err_msg = proc.stdout[-500:] if proc.stdout else "Unknown error"
            with conn:
                conn.execute(
                    "UPDATE queue SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                    (finished_iso, err_msg, job_id)
                )
            fail_msg = (
                f"🔴 <b>Reel ingestion failed on the Pi!</b>\n\n"
                f"🆔 Reel: <code>{shortcode}</code>\n"
                f"⚠️ Error: <code>{err_msg[:200]}</code>"
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
        send_telegram(f"🔴 <b>Reel ingestion crashed on the Pi!</b>\n\n<code>{exc}</code>", chat_id)
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
    cur.execute("SELECT id, shortcode, status, created_at, started_at, finished_at FROM queue ORDER BY id DESC LIMIT 15")
    rows = cur.fetchall()
    print(f"📊 Queue Status ({len(rows)} recent jobs):")
    for r in rows:
        print(f"  #{r[0]:03d} [{r[2].upper():<10}] {r[1]} (Created: {r[3]})")

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
