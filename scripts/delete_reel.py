#!/usr/bin/env python3
"""
ReelShot Deleter CLI:
Deletes a reel entirely from Cloudflare R2 (all GIFs, video.mp4, thumbnails, reel.json)
and removes its entry from the Master Catalog on R2 and locally.

Usage:
  python3 delete_reel.py <reel_id>
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

def main():
    parser = argparse.ArgumentParser(description="Delete a reel from Cloudflare R2 and Master Catalog")
    parser.add_argument("reel_id", nargs="?", help="Reel ID to delete")
    parser.add_argument("--list", action="store_true", help="List all reels in catalog")
    parser.add_argument("--all", action="store_true", help="Wipe all reels from R2")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    catalog = get_catalog()

    if args.list:
        print(f"📚 Master Catalog ({len(catalog)} reels):")
        for idx, r in enumerate(catalog, 1):
            print(f"  {idx:02d}. [{r.get('id')}] {r.get('title')} ({r.get('total_shots', 0)} shots)")
        return

    if args.all:
        if not args.yes:
            conf = input(f"⚠️ Are you sure you want to delete ALL {len(catalog)} reels from Cloudflare R2? (type 'yes'): ")
            if conf.strip().lower() != "yes":
                print("Aborted.")
                return
        count = delete_prefix("data/reels/")
        save_catalog([])
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
    if not args.yes:
        conf = input(f"🗑️ Delete reel '{title}' ({target_id}) from Cloudflare R2? [y/N]: ")
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
    print(f"✅ Removed '{target_id}' from Master Catalog (remaining: {len(new_catalog)}).")

    # Clean local folder if present
    local_dir = APP_DIR / "data" / "reels" / target_id
    if local_dir.exists():
        shutil.rmtree(local_dir, ignore_errors=True)
        print(f"🧹 Removed local folder: {local_dir}")

    print(f"\n🎉 Successfully deleted '{target_id}' entirely!")

if __name__ == "__main__":
    main()
