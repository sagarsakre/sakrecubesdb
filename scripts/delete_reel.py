#!/usr/bin/env python3
"""
ReelShot Deleter CLI:
Deletes a reel or specific scenes from Cloudflare R2 (MP4 clips, video.mp4, thumbnails, reel.json)
and removes its entry from the Master Catalog on R2 and locally.

Usage:
  python3 delete_reel.py <reel_id>
  python3 delete_reel.py <reel_id> --shots 1,3,5
  python3 delete_reel.py --list
  python3 delete_reel.py --all
"""

import os
import sys
import json
import argparse
import shutil
from pathlib import Path
import boto3
from botocore.config import Config

APP_DIR = Path(__file__).resolve().parent.parent

# Load .env if present
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

if not (R2_ENDPOINT and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY):
    print("Error: Missing R2 credentials in environment or .env file.")
    sys.exit(1)

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4", max_pool_connections=10),
    region_name="auto"
)

def get_catalog():
    try:
        res = s3.get_object(Bucket=R2_BUCKET, Key="data/reels.json")
        return json.loads(res["Body"].read().decode())
    except Exception:
        local_cat = APP_DIR / "data" / "reels.json"
        if local_cat.exists():
            try:
                with open(local_cat) as f:
                    return json.load(f)
            except Exception:
                pass
    return []

def save_catalog(catalog):
    # Upload to R2
    s3.put_object(
        Bucket=R2_BUCKET,
        Key="data/reels.json",
        Body=json.dumps(catalog, indent=2).encode(),
        ContentType="application/json"
    )
    # Save locally
    local_cat = APP_DIR / "data" / "reels.json"
    local_cat.parent.mkdir(parents=True, exist_ok=True)
    with open(local_cat, "w") as f:
        json.dump(catalog, f, indent=2)

def prune_cloud_trip_board(deleted_reel_id: str):
    try:
        res = s3.get_object(Bucket=R2_BUCKET, Key="data/trip_board.json")
        board = json.loads(res["Body"].read().decode())
        if isinstance(board, list) and deleted_reel_id in board:
            board = [r for r in board if r != deleted_reel_id]
            s3.put_object(
                Bucket=R2_BUCKET,
                Key="data/trip_board.json",
                Body=json.dumps(board, indent=2).encode(),
                ContentType="application/json"
            )
            local_tb = APP_DIR / "data" / "trip_board.json"
            if local_tb.exists():
                with open(local_tb, "w") as f:
                    json.dump(board, f, indent=2)
            print(f"☁️ Pruned '{deleted_reel_id}' from Cloud Trip Board.")
    except Exception as e:
        print(f"⚠️ Note: could not prune trip_board.json: {e}")

def prune_cloud_collections(deleted_reel_id: str, shot_ids: set = None):
    try:
        res = s3.get_object(Bucket=R2_BUCKET, Key="data/collections.json")
        cols = json.loads(res["Body"].read().decode())
        if not isinstance(cols, list):
            return
        modified = False
        for c in cols:
            items = c.get("items", [])
            initial_len = len(items)
            if shot_ids:
                c["items"] = [i for i in items if not (i.get("reelId") == deleted_reel_id and int(i.get("shotId", -1)) in shot_ids)]
            else:
                c["items"] = [i for i in items if i.get("reelId") != deleted_reel_id]
            if len(c["items"]) != initial_len:
                modified = True

        if modified:
            s3.put_object(
                Bucket=R2_BUCKET,
                Key="data/collections.json",
                Body=json.dumps(cols, indent=2).encode(),
                ContentType="application/json"
            )
            local_col = APP_DIR / "data" / "collections.json"
            if local_col.exists():
                with open(local_col, "w") as f:
                    json.dump(cols, f, indent=2)
            print(f"☁️ Pruned deleted clips from Cloud Saved Collections.")
    except Exception as e:
        print(f"⚠️ Note: could not prune collections.json: {e}")

def delete_prefix(prefix: str):
    res = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix=prefix)
    objects = res.get("Contents", [])
    while res.get("IsTruncated"):
        res = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix=prefix, ContinuationToken=res["NextContinuationToken"])
        objects.extend(res.get("Contents", []))

    if not objects:
        return 0

    for i in range(0, len(objects), 1000):
        batch = [{"Key": o["Key"]} for o in objects[i:i+1000]]
        s3.delete_objects(Bucket=R2_BUCKET, Delete={"Objects": batch})
    return len(objects)

def parse_shot_ids(shots_str: str) -> set:
    ids = set()
    for part in shots_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                ids.update(range(int(start), int(end) + 1))
            except ValueError:
                pass
        else:
            try:
                ids.add(int(part))
            except ValueError:
                pass
    return ids

def main():
    parser = argparse.ArgumentParser(description="Delete a reel or specific scenes from Cloudflare R2 and Master Catalog")
    parser.add_argument("reel_id", nargs="?", help="Reel ID to delete")
    parser.add_argument("--shots", help="Comma-separated shot IDs to delete (e.g. '1,3,5' or '2-4')")
    parser.add_argument("--list", action="store_true", help="List all reels in catalog")
    parser.add_argument("--all", action="store_true", help="Wipe all reels from R2")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    catalog = get_catalog()

    if args.list:
        print(f"📚 Master Catalog ({len(catalog)} reels):")
        for idx, r in enumerate(catalog, 1):
            size_str = f" • {r.get('size_mb')} MB" if r.get('size_mb') else ""
            print(f"  {idx:02d}. [{r.get('id')}] {r.get('title')} ({r.get('total_shots', 0)} shots{size_str})")
        return

    if args.all:
        if not args.yes:
            conf = input(f"⚠️ Are you sure you want to delete ALL {len(catalog)} reels from Cloudflare R2? (type 'yes'): ")
            if conf.strip().lower() != "yes":
                print("Aborted.")
                return
        count = delete_prefix("data/reels/")
        save_catalog([])
        prune_cloud_trip_board("")
        s3.put_object(Bucket=R2_BUCKET, Key="data/trip_board.json", Body=b"[]", ContentType="application/json")
        s3.put_object(Bucket=R2_BUCKET, Key="data/collections.json", Body=b"[]", ContentType="application/json")
        local_reels = APP_DIR / "data" / "reels"
        if local_reels.exists():
            shutil.rmtree(local_reels, ignore_errors=True)
        print(f"✅ Deleted {count} objects and wiped catalog on R2.")
        return

    if not args.reel_id:
        parser.print_help()
        sys.exit(1)

    target_id = args.reel_id.strip()
    match = next((r for r in catalog if r.get("id") == target_id), None)
    if not match:
        print(f"⚠️ Warning: '{target_id}' not found in catalog, but checking R2 storage prefix...")

    title = match.get("title", target_id) if match else target_id

    # Handle Partial Scene Deletion
    if args.shots:
        target_shot_ids = parse_shot_ids(args.shots)
        if not target_shot_ids:
            print("Error: No valid shot IDs provided in --shots.")
            sys.exit(1)

        # 1. Fetch reel.json
        reel_json_key = f"data/reels/{target_id}/reel.json"
        reel_data = None
        try:
            res = s3.get_object(Bucket=R2_BUCKET, Key=reel_json_key)
            reel_data = json.loads(res["Body"].read().decode())
        except Exception:
            local_json = APP_DIR / "data" / "reels" / target_id / "reel.json"
            if local_json.exists():
                with open(local_json) as f:
                    reel_data = json.load(f)

        if not reel_data or "shots" not in reel_data:
            print(f"Error: Could not retrieve reel.json for '{target_id}'.")
            sys.exit(1)

        shots_to_delete = [s for s in reel_data["shots"] if s.get("id") in target_shot_ids]
        if not shots_to_delete:
            print(f"⚠️ No matching shots found for IDs {sorted(target_shot_ids)} in '{target_id}'.")
            return

        print(f"✂️ Found {len(shots_to_delete)} scenes to delete:")
        for s in shots_to_delete:
            print(f"   • Shot {s.get('id')}: {s.get('clip')} ({s.get('duration')})")

        if not args.yes:
            conf = input(f"Permanently delete these {len(shots_to_delete)} scenes from Cloudflare R2? [y/N]: ")
            if conf.strip().lower() not in ["y", "yes"]:
                print("Aborted.")
                return

        # 2. Delete each clip from R2 and local
        local_dir = APP_DIR / "data" / "reels" / target_id
        for s in shots_to_delete:
            clip_key = f"data/reels/{target_id}/{s.get('clip')}"
            try:
                s3.delete_object(Bucket=R2_BUCKET, Key=clip_key)
                print(f"   ☁️ Deleted from R2: {clip_key}")
            except Exception as e:
                print(f"   ⚠️ Failed to delete {clip_key}: {e}")

            if local_dir.exists():
                local_file = local_dir / s.get("clip")
                local_file.unlink(missing_ok=True)

        # 3. Update reel.json
        remaining_shots = [s for s in reel_data["shots"] if s.get("id") not in target_shot_ids]
        reel_data["shots"] = remaining_shots
        reel_data["total_shots"] = len(remaining_shots)

        # Update package size
        if "size_mb" in reel_data and len(reel_data["shots"]) > 0:
            removed_ratio = len(shots_to_delete) / (len(remaining_shots) + len(shots_to_delete))
            # Estimated size reduction
            reel_data["size_mb"] = max(1.0, round(float(reel_data["size_mb"]) * (1.0 - 0.7 * removed_ratio), 1))

        # Upload updated reel.json to R2
        s3.put_object(
            Bucket=R2_BUCKET,
            Key=reel_json_key,
            Body=json.dumps(reel_data, indent=2).encode(),
            ContentType="application/json"
        )
        print(f"✅ Updated {reel_json_key} on R2 (remaining: {len(remaining_shots)} shots).")

        if local_dir.exists():
            with open(local_dir / "reel.json", "w") as f:
                json.dump(reel_data, f, indent=2)

        # 4. Update Master Catalog
        for r in catalog:
            if r.get("id") == target_id:
                r["total_shots"] = len(remaining_shots)
                if "size_mb" in reel_data:
                    r["size_mb"] = reel_data["size_mb"]
                break
        save_catalog(catalog)
        prune_cloud_collections(target_id, target_shot_ids)
        print(f"✅ Updated Master Catalog data/reels.json on R2.")
        print(f"\n🎉 Successfully deleted {len(shots_to_delete)} scenes from '{target_id}'!")
        return

    # Full Reel Deletion
    if not args.yes:
        conf = input(f"🗑️ Delete reel '{title}' ({target_id}) entirely from Cloudflare R2? [y/N]: ")
        if conf.strip().lower() not in ["y", "yes"]:
            print("Aborted.")
            return

    # Delete all objects under data/reels/<target_id>/
    prefix = f"data/reels/{target_id}/"
    del_count = delete_prefix(prefix)
    print(f"☁️ Deleted {del_count} objects under '{prefix}' on Cloudflare R2.")

    # Remove from catalog
    new_catalog = [r for r in catalog if r.get("id") != target_id]
    save_catalog(new_catalog)
    prune_cloud_trip_board(target_id)
    prune_cloud_collections(target_id)
    print(f"✅ Removed '{target_id}' from Master Catalog (remaining: {len(new_catalog)}).")

    # Clean local folder if present
    local_dir = APP_DIR / "data" / "reels" / target_id
    if local_dir.exists():
        shutil.rmtree(local_dir, ignore_errors=True)
        print(f"🧹 Removed local folder: {local_dir}")

    print(f"\n🎉 Successfully deleted '{target_id}' entirely!")

if __name__ == "__main__":
    main()
