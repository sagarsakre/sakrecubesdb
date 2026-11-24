#!/usr/bin/env python3
"""
ReelShot CLI Automator:
Takes any input video, automatically detects scenes, exports 640p GIFs,
generates a cover thumbnail and editable reel.json, and registers it in the Master Catalog.

Usage:
  python3 export_reel.py "path/to/video.mp4" --title "Munnar Tea Hills" --subtitle "Kerala Roadtrip"
"""

import os
import sys
import json
import argparse
import subprocess
import re
from datetime import datetime, timezone

def main():
    parser = argparse.ArgumentParser(description="Auto-export video scenes to ReelShot PWA package")
    parser.add_argument("video", help="Path to input video file (mp4/mov)")
    parser.add_argument("--id", help="Unique reel ID (slug). Defaults to filename slug.")
    parser.add_argument("--title", help="Human-readable title. Defaults to filename.")
    parser.add_argument("--subtitle", default="", help="Optional subtitle or location info.")
    parser.add_argument("--threshold", type=float, default=0.3, help="Scene detection sensitivity (0.1 to 0.5, default 0.3)")
    parser.add_argument("--fps", type=int, default=24, help="Output framerate (default 24 fps)")
    parser.add_argument("--resolution", type=int, default=640, help="Output clip max dimension (e.g. 640, 480, default 640)")
    parser.add_argument("--app-dir", default=None, help="Root path of the PWA app directory.")
    args = parser.parse_args()

    video_path = os.path.abspath(args.video)
    if not os.path.isfile(video_path):
        print(f"Error: Video file not found: {video_path}")
        sys.exit(1)

    base_name = os.path.splitext(os.path.basename(video_path))[0]
    reel_id = args.id or re.sub(r'[^a-zA-Z0-9_\-]+', '-', base_name).strip('-').lower()
    title = args.title or base_name
    subtitle = args.subtitle

    script_dir = os.path.dirname(os.path.abspath(__file__))
    app_dir = args.app_dir or os.path.abspath(os.path.join(script_dir, ".."))
    reel_dir = os.path.join(app_dir, "data", "reels", reel_id)
    os.makedirs(reel_dir, exist_ok=True)

    print(f"🎬 Processing: {title}")
    print(f"📁 Destination: {reel_dir}")

    # 1. Detect scenes
    print("⏳ Step 1/4: Analyzing video & detecting scene cuts...")
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

    print(f"✨ Found {len(intervals)} distinct scenes!")

    # 2. Extract cover thumbnail
    print("⏳ Step 2/4: Generating cover thumbnail.jpg...")
    thumb_path = os.path.join(reel_dir, "thumbnail.jpg")
    thumb_time = min(1.0, total_dur / 2.0)
    subprocess.run([
        "ffmpeg", "-y",
        "-ss", str(thumb_time),
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        thumb_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # 3. Export looping MP4 clips (CRF 23, +faststart, -an)
    res = args.resolution
    print(f"⏳ Step 3/4: Exporting {len(intervals)} scenes directly to {res}p MP4 clips (H.264, +faststart)...")
    vf_scale = f"scale='if(gt(iw,ih), -2, min({res},iw))':'if(gt(iw,ih), min({res},ih), -2)'"
    shots = []

    for idx, (start, dur) in enumerate(intervals, 1):
        clip_filename = f"split_{idx:02d}.mp4"
        clip_out = os.path.join(reel_dir, clip_filename)

        cmd_clip = [
            "ffmpeg", "-y",
            "-ss", f"{start:.6f}",
            "-t", f"{dur:.6f}",
            "-i", video_path,
            "-vf", vf_scale,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-level", "4.1",
            "-crf", "23",
            "-preset", "medium",
            "-movflags", "+faststart",
            "-an",
            clip_out
        ]
        subprocess.run(cmd_clip, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        size_mb = os.path.getsize(clip_out) / (1024 * 1024)

        shot_title = f"Shot {idx:02d}"
        shots.append({
            "id": idx,
            "title": shot_title,
            "clip": clip_filename,
            "duration": f"{dur:.2f}s"
        })
        print(f"   [{idx:02d}/{len(intervals)}] {dur:.2f}s -> {clip_filename} ({size_mb:.2f} MB)")

    # 4. Write reel.json
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("⏳ Step 4/4: Writing reel.json and registering reel in Master Catalog...")
    reel_data = {
        "id": reel_id,
        "title": title,
        "subtitle": subtitle,
        "thumbnail": "thumbnail.jpg",
        "total_shots": len(shots),
        "created_at": now_iso,
        "shots": shots
    }
    reel_json_path = os.path.join(reel_dir, "reel.json")
    with open(reel_json_path, "w") as f:
        json.dump(reel_data, f, indent=2)

    # Register in data/reels.json (Master Catalog, sorted chronological latest to oldest)
    reels_json_path = os.path.join(app_dir, "data", "reels.json")
    reels_list = []
    if os.path.isfile(reels_json_path):
        try:
            with open(reels_json_path, "r") as f:
                reels_list = json.load(f)
        except Exception:
            reels_list = []

    # Remove existing entry if re-processing
    reels_list = [r for r in reels_list if r.get("id") != reel_id]
    
    # Add new entry
    reels_list.insert(0, {
        "id": reel_id,
        "title": title,
        "subtitle": subtitle,
        "thumbnail": f"data/reels/{reel_id}/thumbnail.jpg",
        "reel_json": f"data/reels/{reel_id}/reel.json",
        "total_shots": len(shots),
        "created_at": now_iso
    })

    # Sort chronological (newest first)
    reels_list.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    with open(reels_json_path, "w") as f:
        json.dump(reels_list, f, indent=2)

    print("\n🎉 All Done! The reel is registered in your ReelShot Master Catalog.")
    print(f"📝 You can edit shot titles anytime in:\n   {reel_json_path}")

if __name__ == "__main__":
    main()
