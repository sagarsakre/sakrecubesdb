#!/usr/bin/env python3
"""
ReelShot Ingestion Pipeline for Raspberry Pi / Local Worker:
Takes any input video (mp4/mov), detects scenes, generates 640p MP4 looping clips,
uploads all assets to Cloudflare R2 CDN, and updates the Master Catalog on R2.

Usage:
  python3 ingest_reel.py "/path/to/video.mp4" --title "Munnar Hills" --subtitle "Kerala Stay"
"""

import os
import sys
import json
import argparse
import subprocess
import re
import mimetypes
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
from botocore.config import Config

# Load .env if present
APP_DIR = Path(__file__).resolve().parent.parent
env_file = APP_DIR / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET = os.getenv("R2_BUCKET", "sakrecubesdb-assets")
CDN_PUBLIC_URL = os.getenv("CDN_PUBLIC_URL", "https://pub-fd6036bfc4d84b359e94558bed250b7c.r2.dev")

# Dual Logger: Mirrors all print output to both terminal (n8n) and logs/ingest.log
class DualLogger:
    def __init__(self, log_file_path: Path):
        self.terminal = sys.stdout
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = open(log_file_path, "a", buffering=1, encoding="utf-8")

    def write(self, message):
        self.terminal.write(message)
        self.terminal.flush()
        self.log_file.write(message)
        self.log_file.flush()

    def flush(self):
        self.terminal.flush()
        self.log_file.flush()

LOG_FILE = APP_DIR / "logs" / "ingest.log"
sys.stdout = DualLogger(LOG_FILE)

def extract_reel_shortcode(url: str) -> str:
    m = re.search(r"/(?:reel|reels|p)/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else ""

def shortcode_to_pk(shortcode: str) -> str:
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    pk = 0
    for c in shortcode:
        if c in alphabet:
            pk = pk * 64 + alphabet.index(c)
    return str(pk)

def get_s3_client():
    if not (R2_ENDPOINT and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY):
        return None
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4", max_pool_connections=10),
        region_name="auto"
    )

def get_content_type(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext in [".jpg", ".jpeg"]:
        return "image/jpeg"
    elif ext == ".png":
        return "image/png"
    elif ext == ".json":
        return "application/json"
    elif ext == ".mp4":
        return "video/mp4"
    ctype, _ = mimetypes.guess_type(str(file_path))
    return ctype or "application/octet-stream"

def upload_to_r2(s3, local_path: Path, r2_key: str):
    content_type = get_content_type(local_path)
    s3.upload_file(str(local_path), R2_BUCKET, r2_key, ExtraArgs={"ContentType": content_type})


def get_ig_cookie_file() -> Path:
    """Return the persistent path for Instagram cookies."""
    env_path = os.getenv("IG_COOKIES_FILE")
    if env_path:
        return Path(env_path).expanduser()
    return APP_DIR / "data" / "ig_cookies.txt"


def fetch_instagram_media_info(shortcode: str, ytdlp_bin: str = "yt-dlp"):
    """Fetch Instagram media JSON via mobile API using cookies (auto-refreshing via Chrome if needed)."""
    if not shortcode:
        return None
    pk = shortcode_to_pk(shortcode)
    cookie_file = get_ig_cookie_file()

    def _query():
        if not cookie_file.exists():
            return None
        cmd = [
            "curl", "-s", "-b", str(cookie_file),
            "-H", "User-Agent: Instagram 278.0.0.19.115 Android",
            "-H", "X-IG-App-ID: 936619743392459",
            f"https://i.instagram.com/api/v1/media/{pk}/info/"
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode("utf-8")
        data = json.loads(out)
        if data.get("status") == "ok" and data.get("items"):
            return data["items"][0]
        return None

    try:
        item = _query()
        if item:
            return item
    except Exception:
        pass

    # Refresh cookies from Chrome if missing/expired (works on desktop with Chrome)
    try:
        cookie_file.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [ytdlp_bin, "--cookies-from-browser", "chrome", "--cookies", str(cookie_file),
             "--skip-download", f"https://www.instagram.com/p/{shortcode}/"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20
        )
        return _query()
    except Exception:
        return None


def download_binary_url(url: str, dest: Path):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
        f.write(resp.read())


def transcode_h264_aac(src_path: Path, dst_path: Path):
    cmd = [
        "ffmpeg", "-y", "-i", str(src_path),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart",
        str(dst_path)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def update_master_catalog_entry(s3, summary_entry: dict):
    catalog_list = []
    if s3:
        try:
            res = s3.get_object(Bucket=R2_BUCKET, Key="data/reels.json")
            catalog_list = json.loads(res["Body"].read().decode())
        except Exception:
            pass

    if not catalog_list:
        local_cat = APP_DIR / "data" / "reels.json"
        if local_cat.exists():
            try:
                with open(local_cat) as f:
                    catalog_list = json.load(f)
            except Exception:
                catalog_list = []

    catalog_list = [r for r in catalog_list if r.get("id") != summary_entry["id"]]
    catalog_list.insert(0, summary_entry)
    catalog_list.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    if s3:
        s3.put_object(
            Bucket=R2_BUCKET,
            Key="data/reels.json",
            Body=json.dumps(catalog_list, indent=2).encode(),
            ContentType="application/json"
        )
        print("   ✅ data/reels.json updated on R2!")

    local_cat = APP_DIR / "data" / "reels.json"
    local_cat.parent.mkdir(parents=True, exist_ok=True)
    with open(local_cat, "w") as f:
        json.dump(catalog_list, f, indent=2)


def ingest_carousel_post(item_ig: dict, shortcode: str, reel_id: str, title: str, subtitle: str,
                         full_caption: str, like_count: int, view_count: int, clean_local: bool):
    carousel_media = item_ig.get("carousel_media") or []
    if not carousel_media and item_ig.get("media_type") == 1:
        carousel_media = [item_ig]

    post_type = "carousel" if len(carousel_media) > 1 else "photo"
    output_dir = APP_DIR / "data" / "reels" / reel_id
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"📸 Processing {post_type.title()}: {title} (ID: {reel_id}, {len(carousel_media)} slides)")

    shots = []
    files_to_upload = []
    for idx, slide in enumerate(carousel_media, start=1):
        slide_type = slide.get("media_type", 1)
        candidates = (slide.get("image_versions2") or {}).get("candidates") or []
        img_url = candidates[0]["url"] if candidates else None
        vid_versions = slide.get("video_versions") or []
        vid_url = vid_versions[0]["url"] if (slide_type == 2 and vid_versions) else None

        img_filename = f"slide_{idx:02d}.jpg"
        img_path = output_dir / img_filename
        if img_url:
            download_binary_url(img_url, img_path)
            files_to_upload.append((img_path, f"data/reels/{reel_id}/{img_filename}"))

        shot_entry = {
            "id": idx,
            "title": f"Photo {idx:02d}" if not vid_url else f"Slide {idx:02d} (Video)",
            "media_type": "video" if vid_url else "image",
            "image": img_filename,
            "clip": img_filename,
            "duration": ""
        }

        if vid_url:
            raw_vid = output_dir / f"slide_{idx:02d}_raw.mp4"
            vid_filename = f"slide_{idx:02d}.mp4"
            vid_path = output_dir / vid_filename
            download_binary_url(vid_url, raw_vid)
            transcode_h264_aac(raw_vid, vid_path)
            raw_vid.unlink(missing_ok=True)
            shot_entry["video"] = vid_filename
            shot_entry["clip"] = vid_filename
            files_to_upload.append((vid_path, f"data/reels/{reel_id}/{vid_filename}"))

        shots.append(shot_entry)
        print(f"   [{idx:02d}/{len(carousel_media)}] Saved {shot_entry['clip']}")

    thumb_path = output_dir / "thumbnail.jpg"
    first_img = output_dir / "slide_01.jpg"
    if first_img.exists():
        thumb_path.write_bytes(first_img.read_bytes())
        files_to_upload.append((thumb_path, f"data/reels/{reel_id}/thumbnail.jpg"))

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_files = [lp for lp, _ in files_to_upload if lp.exists()]
    total_bytes = sum(f.stat().st_size for f in all_files)
    size_mb = round(total_bytes / (1024 * 1024), 1)
    instagram_url = f"https://www.instagram.com/p/{shortcode}/" if shortcode else ""

    reel_data = {
        "id": reel_id,
        "type": post_type,
        "shortcode": shortcode,
        "instagram_url": instagram_url,
        "title": title,
        "subtitle": subtitle,
        "caption": full_caption or title,
        "views": view_count,
        "view_count": view_count,
        "like_count": like_count,
        "thumbnail": "thumbnail.jpg",
        "total_shots": len(shots),
        "total_bytes": total_bytes,
        "size_mb": size_mb,
        "created_at": now_iso,
        "shots": shots
    }
    reel_json_path = output_dir / "reel.json"
    with open(reel_json_path, "w") as f:
        json.dump(reel_data, f, indent=2)
    files_to_upload.append((reel_json_path, f"data/reels/{reel_id}/reel.json"))

    s3 = get_s3_client()
    if s3:
        print("⏳ Uploading carousel assets to Cloudflare R2 CDN...")
        for i, (lp, rk) in enumerate(files_to_upload, 1):
            upload_to_r2(s3, lp, rk)
            print(f"   [{i:02d}/{len(files_to_upload)}] ☁️ Synced to R2: {rk}")

    summary_entry = {
        "id": reel_id,
        "type": post_type,
        "shortcode": shortcode,
        "instagram_url": instagram_url,
        "title": title,
        "subtitle": subtitle,
        "caption": full_caption or title,
        "views": view_count,
        "view_count": view_count,
        "like_count": like_count,
        "thumbnail": f"data/reels/{reel_id}/thumbnail.jpg",
        "reel_json": f"data/reels/{reel_id}/reel.json",
        "total_shots": len(shots),
        "total_bytes": total_bytes,
        "size_mb": size_mb,
        "created_at": now_iso
    }
    update_master_catalog_entry(s3, summary_entry)

    if clean_local:
        for lp, _ in files_to_upload:
            if lp.name not in ("reel.json", "thumbnail.jpg"):
                lp.unlink(missing_ok=True)

    result = {
        "status": "success",
        "reel_id": reel_id,
        "type": post_type,
        "title": title,
        "shots_count": len(shots),
        "total_size": f"{size_mb} MB",
        "created_at": now_iso,
        "cdn_url": f"{CDN_PUBLIC_URL}/data/reels/{reel_id}/reel.json"
    }
    print(f"\n🎉 SUCCESS: {json.dumps(result, indent=2)}")

def main():
    parser = argparse.ArgumentParser(description="Ingest video reel into ReelShot PWA and sync to Cloudflare R2 CDN")
    parser.add_argument("input", help="Path to input video file (mp4/mov) OR direct video URL (Instagram/YouTube)")
    parser.add_argument("--id", help="Unique reel ID (slug). Defaults to cleaned title.")
    parser.add_argument("--title", help="Human-readable title. Defaults to post title or filename.")
    parser.add_argument("--subtitle", default="", help="Optional subtitle or location.")
    parser.add_argument("--threshold", type=float, default=0.28, help="Scene detection sensitivity (default 0.28)")
    parser.add_argument("--fps", type=int, default=24, help="Output framerate (default 24 fps)")
    parser.add_argument("--resolution", type=int, default=640, help="Output clip max dimension (e.g. 640, 480, default 640)")
    parser.add_argument("--clean", action="store_true", help="Delete local output clips after uploading to R2")
    parser.add_argument("--delete-source", action="store_true", help="Delete the source video after processing")
    args = parser.parse_args()

    input_target = args.input
    downloaded_temp_video = False
    shortcode = ""
    instagram_url = ""
    desc = ""
    full_caption = ""
    view_count = 0
    like_count = 0

    # Check if input is a URL
    if input_target.startswith("http://") or input_target.startswith("https://"):
        print(f"🌐 URL detected: {input_target}")

        # Locate yt-dlp binary (standard PATH or ~/.local/bin)
        ytdlp_bin = "yt-dlp"
        user_local_ytdlp = Path.home() / ".local" / "bin" / "yt-dlp"
        if user_local_ytdlp.exists():
            ytdlp_bin = str(user_local_ytdlp)

        # Check if Instagram Carousel or Single Photo first
        if "instagram.com" in input_target:
            ig_sc = extract_reel_shortcode(input_target)
            item_ig = fetch_instagram_media_info(ig_sc, ytdlp_bin)
            if item_ig and item_ig.get("media_type") in (1, 8):
                user_obj = item_ig.get("user") or {}
                uploader = user_obj.get("username") or user_obj.get("full_name") or "creator"
                desc = (item_ig.get("caption") or {}).get("text") or ""
                full_caption = desc.strip()
                caption_lines = [l.strip() for l in desc.splitlines() if l.strip() and not l.strip().startswith("#")]
                first_caption = re.sub(r'#\w+', '', caption_lines[0]).strip() if caption_lines else ""

                if args.title:
                    title = args.title.strip()
                elif first_caption and len(first_caption) >= 4:
                    title = first_caption[:50].strip()
                else:
                    title = f"Post by @{uploader}"

                subtitle = args.subtitle or f"@{uploader}"
                base_name = re.sub(r'[^a-zA-Z0-9_\-]+', '-', title).strip('-').lower() or "post"
                reel_id = args.id.strip().lower() if args.id else f"{base_name[:24]}-{ig_sc.lower()}".strip('-')
                like_count = int(item_ig.get("like_count") or 0)
                view_count = int(item_ig.get("play_count") or item_ig.get("ig_play_count") or item_ig.get("view_count") or 0)

                ingest_carousel_post(item_ig, ig_sc, reel_id, title, subtitle, full_caption, like_count, view_count, args.clean)
                return

        print("⏳ Step 0/5: Downloading video via yt-dlp...")

        temp_out = f"/tmp/reelshot_dl_{int(datetime.now().timestamp())}.%(ext)s"
        cmd_dl = [ytdlp_bin, input_target, "-o", temp_out, "--print-json"]
        
        proc_dl = subprocess.run(cmd_dl, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc_dl.returncode != 0:
            cookie_file = get_ig_cookie_file()
            if cookie_file.exists():
                print(f"⚠️ Anonymous download failed, retrying with cookies ({cookie_file})...")
                cmd_dl_cookies = [ytdlp_bin, "--cookies", str(cookie_file), input_target, "-o", temp_out, "--print-json"]
                proc_dl = subprocess.run(cmd_dl_cookies, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if proc_dl.returncode != 0:
                print("⚠️ Retrying with Chrome browser cookies...")
                cmd_dl_chrome = [ytdlp_bin, "--cookies-from-browser", "chrome", input_target, "-o", temp_out, "--print-json"]
                proc_dl = subprocess.run(cmd_dl_chrome, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if proc_dl.returncode != 0:
                print(json.dumps({"status": "error", "message": f"yt-dlp download failed: {proc_dl.stderr}"}))
                sys.exit(1)

        # Parse downloaded video info
        info = {}
        for line in proc_dl.stdout.splitlines():
            try:
                info = json.loads(line)
                break
            except Exception:
                pass

        # Find actual downloaded file on disk
        video_path = None
        for ext in [".mp4", ".mov", ".mkv", ".webm"]:
            candidate = temp_out.replace(".%(ext)s", ext)
            if os.path.isfile(candidate):
                video_path = candidate
                break

        if not video_path:
            # Fallback scan in /tmp
            matches = list(Path("/tmp").glob("reelshot_dl_*.*"))
            if matches:
                video_path = str(matches[-1])

        if not video_path or not os.path.isfile(video_path):
            print(json.dumps({"status": "error", "message": "Failed to locate downloaded video file"}))
            sys.exit(1)

        downloaded_temp_video = True
        unique_id = info.get("id") or extract_reel_shortcode(input_target)
        shortcode = unique_id or ""
        instagram_url = f"https://www.instagram.com/reel/{shortcode}/" if shortcode else (input_target if "instagram.com" in input_target else "")
        uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
        desc = info.get("description") or ""
        full_caption = desc.strip()
        view_count = int(info.get("view_count") or info.get("play_count") or info.get("ig_play_count") or 0)
        like_count = int(info.get("like_count") or 0)

        # Attempt to fetch exact play_count directly from Instagram API if missing
        if view_count == 0 and shortcode:
            try:
                cookie_file = get_ig_cookie_file()
                if cookie_file.exists():
                    pk = shortcode_to_pk(shortcode)
                    cmd_ig = [
                        'curl', '-s',
                        '-b', str(cookie_file),
                        '-H', 'User-Agent: Instagram 278.0.0.19.115 Android',
                        '-H', 'X-IG-App-ID: 936619743392459',
                        f'https://i.instagram.com/api/v1/media/{pk}/info/'
                    ]
                    out_ig = subprocess.check_output(cmd_ig, stderr=subprocess.DEVNULL).decode('utf-8')
                    data_ig = json.loads(out_ig)
                    item_ig = data_ig.get('items', [{}])[0]
                    view_count = int(item_ig.get('play_count') or item_ig.get('ig_play_count') or item_ig.get('view_count') or 0)
                    if like_count == 0:
                        like_count = int(item_ig.get('like_count') or 0)
            except Exception:
                pass

        caption_lines = [l.strip() for l in desc.splitlines() if l.strip() and not l.strip().startswith("#")]
        first_caption = caption_lines[0] if caption_lines else ""
        if first_caption:
            first_caption = re.sub(r'#\w+', '', first_caption).strip()

        if args.title:
            title = args.title.strip()
        elif first_caption and len(first_caption) >= 4:
            title = first_caption[:50].strip()
        elif info.get("title") and not info.get("title").lower().startswith("video by"):
            title = info.get("title").strip()
        elif uploader:
            title = f"Reel by @{uploader}"
        else:
            title = "Instagram Reel"

        subtitle = args.subtitle
        if not subtitle and uploader:
            subtitle = f"@{uploader}"

        base_name = re.sub(r'[^a-zA-Z0-9_\-]+', '-', title).strip('-').lower()
        if args.id:
            reel_id = args.id.strip().lower()
        elif unique_id:
            # Include unique Instagram ID / shortcode so multiple reels from the same creator never collide
            short_unique = unique_id.lower()
            if short_unique in base_name:
                reel_id = base_name[:40]
            else:
                reel_id = f"{base_name[:24]}-{short_unique}".strip('-')
        else:
            reel_id = base_name[:35]

        print(f"   ✅ Downloaded: {video_path} ({os.path.getsize(video_path) / (1024*1024):.2f} MB)")
    else:
        video_path = os.path.abspath(input_target)
        if not os.path.isfile(video_path):
            print(json.dumps({"status": "error", "message": f"Video not found: {video_path}"}))
            sys.exit(1)

        base_name = os.path.splitext(os.path.basename(video_path))[0]
        title = args.title or base_name
        subtitle = args.subtitle
        reel_id = args.id or re.sub(r'[^a-zA-Z0-9_\-]+', '-', base_name).strip('-').lower()

    output_dir = APP_DIR / "data" / "reels" / reel_id
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"🎬 Processing Reel: {title} (ID: {reel_id})")

    # 1. Detect scenes
    print("⏳ Step 1/5: Detecting scene cuts with FFmpeg...")
    cmd_detect = [
        "ffmpeg", "-i", video_path,
        "-filter:v", f"select='gt(scene,{args.threshold})',showinfo",
        "-f", "null", "-"
    ]
    proc = subprocess.run(cmd_detect, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)

    cut_times = []
    for line in proc.stderr.splitlines():
        if "pts_time:" in line:
            m = re.search(r"pts_time:\s*([\d\.]+)", line)
            if m:
                cut_times.append(float(m.group(1)))

    cmd_dur = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path
    ]
    total_dur = float(subprocess.check_output(cmd_dur).decode().strip())

    intervals = []
    prev = 0.0
    for ct in cut_times:
        intervals.append((prev, ct - prev))
        prev = ct
    if prev < total_dur:
        intervals.append((prev, total_dur - prev))

    print(f"✨ Found {len(intervals)} distinct scenes in {total_dur:.1f}s video.")

    # 2. Extract cover thumbnail & prepare web video
    print("⏳ Step 2/5: Extracting cover thumbnail.jpg & optimizing video.mp4...")
    thumb_path = output_dir / "thumbnail.jpg"
    thumb_time = min(1.0, total_dur / 2.0)
    subprocess.run([
        "ffmpeg", "-y",
        "-ss", str(thumb_time),
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        str(thumb_path)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # Save full video as iOS Safari compatible H.264 (yuv420p) + AAC with +faststart
    video_out = output_dir / "video.mp4"
    cmd_transcode = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart",
        str(video_out)
    ]
    subprocess.run(cmd_transcode, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # 3. Export looping MP4 clips (CRF 23, +faststart, -an)
    target_resolution = args.resolution
    print(f"⏳ Step 3/5: Generating {target_resolution}p MP4 clips (H.264, +faststart) for {len(intervals)} shots...")
    vf_scale = f"scale='if(gt(iw,ih), -2, min({target_resolution},iw))':'if(gt(iw,ih), min({target_resolution},ih), -2)'"
    shots = []

    for idx, (start, dur) in enumerate(intervals, 1):
        clip_filename = f"split_{idx:02d}.mp4"
        clip_out = output_dir / clip_filename

        cmd_clip = [
            "ffmpeg", "-y",
            "-ss", f"{start:.6f}",
            "-t", f"{dur:.6f}",
            "-i", str(video_path),
            "-vf", vf_scale,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-level", "4.1",
            "-crf", "23",
            "-preset", "medium",
            "-movflags", "+faststart",
            "-an",
            str(clip_out)
        ]
        subprocess.run(cmd_clip, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        size_mb = clip_out.stat().st_size / (1024 * 1024)

        shot_title = f"Shot {idx:02d}"
        shots.append({
            "id": idx,
            "title": shot_title,
            "clip": clip_filename,
            "duration": f"{dur:.2f}s"
        })
        print(f"   [{idx:02d}/{len(intervals)}] {dur:.2f}s -> {clip_filename} ({size_mb:.2f} MB)")

    # 4. Generate reel.json & compute total package size
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_package_files = [video_out, thumb_path] + [output_dir / s["clip"] for s in shots]
    total_bytes = sum(f.stat().st_size for f in all_package_files if f.exists())
    size_mb = round(total_bytes / (1024 * 1024), 1)

    reel_data = {
        "id": reel_id,
        "type": "reel",
        "shortcode": shortcode,
        "instagram_url": instagram_url,
        "title": title,
        "subtitle": subtitle,
        "caption": full_caption or title,
        "views": view_count,
        "view_count": view_count,
        "like_count": like_count,
        "resolution": f"{target_resolution}p",
        "thumbnail": "thumbnail.jpg",
        "video": "video.mp4",
        "total_shots": len(shots),
        "total_bytes": total_bytes,
        "size_mb": size_mb,
        "created_at": now_iso,
        "shots": shots
    }
    reel_json_path = output_dir / "reel.json"
    with open(reel_json_path, "w") as f:
        json.dump(reel_data, f, indent=2)

    # 5. Upload to Cloudflare R2
    print("⏳ Step 4/5: Uploading assets to Cloudflare R2 CDN...")
    s3 = get_s3_client()
    if s3:
        files_to_upload = [
            (thumb_path, f"data/reels/{reel_id}/thumbnail.jpg"),
            (video_out, f"data/reels/{reel_id}/video.mp4"),
            (reel_json_path, f"data/reels/{reel_id}/reel.json")
        ]
        for shot in shots:
            clip_p = output_dir / shot["clip"]
            files_to_upload.append((clip_p, f"data/reels/{reel_id}/{shot['clip']}"))

        total_files = len(files_to_upload)
        for idx, (lp, rk) in enumerate(files_to_upload, 1):
            upload_to_r2(s3, lp, rk)
            print(f"   [{idx:02d}/{total_files}] ☁️ Synced to R2: {rk}")

        # Update Master Catalog on R2
        print("⏳ Step 5/5: Updating Master Catalog (data/reels.json) on R2...")
        catalog_list = []
        try:
            res = s3.get_object(Bucket=R2_BUCKET, Key="data/reels.json")
            catalog_list = json.loads(res["Body"].read().decode())
        except Exception:
            local_cat = APP_DIR / "data" / "reels.json"
            if local_cat.exists():
                try:
                    with open(local_cat) as f:
                        catalog_list = json.load(f)
                except Exception:
                    catalog_list = []

        catalog_list = [r for r in catalog_list if r.get("id") != reel_id]
        catalog_list.insert(0, {
            "id": reel_id,
            "type": "reel",
            "shortcode": shortcode,
            "instagram_url": instagram_url,
            "title": title,
            "subtitle": subtitle,
            "caption": full_caption or title,
            "views": view_count,
            "view_count": view_count,
            "like_count": like_count,
            "resolution": f"{target_resolution}p",
            "thumbnail": f"data/reels/{reel_id}/thumbnail.jpg",
            "video": f"data/reels/{reel_id}/video.mp4",
            "reel_json": f"data/reels/{reel_id}/reel.json",
            "total_shots": len(shots),
            "total_bytes": total_bytes,
            "size_mb": size_mb,
            "created_at": now_iso
        })
        catalog_list.sort(key=lambda r: r.get("created_at", ""), reverse=True)

        # Upload updated catalog to R2
        s3.put_object(
            Bucket=R2_BUCKET,
            Key="data/reels.json",
            Body=json.dumps(catalog_list, indent=2).encode(),
            ContentType="application/json"
        )
        print("   ✅ data/reels.json updated on R2!")

        # Also update local reels.json for git sync
        local_cat = APP_DIR / "data" / "reels.json"
        local_cat.parent.mkdir(parents=True, exist_ok=True)
        with open(local_cat, "w") as f:
            json.dump(catalog_list, f, indent=2)

    # Optional Cleanups
    if args.clean:
        print("🧹 Cleaning local temporary files...")
        video_out.unlink(missing_ok=True)
        for shot in shots:
            (output_dir / shot["clip"]).unlink(missing_ok=True)
        print("   Done.")

    if args.delete_source or downloaded_temp_video:
        if downloaded_temp_video or args.delete_source:
            print(f"🗑️ Deleting temporary downloaded video: {video_path}")
            try:
                os.remove(video_path)
            except Exception:
                pass

    result = {
        "status": "success",
        "reel_id": reel_id,
        "title": title,
        "shots_count": len(shots),
        "total_size": f"{size_mb} MB",
        "created_at": now_iso,
        "cdn_url": f"{CDN_PUBLIC_URL}/data/reels/{reel_id}/reel.json"
    }
    print(f"\n🎉 SUCCESS: {json.dumps(result, indent=2)}")

if __name__ == "__main__":
    main()
