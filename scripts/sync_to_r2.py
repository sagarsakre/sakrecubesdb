#!/usr/bin/env python3
"""
Sync local reel assets in data/ to Cloudflare R2 bucket.
Supports multi-threaded uploading and setting correct Content-Type.
"""

import os
import mimetypes
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
from botocore.config import Config

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"

# Auto-load .env if present
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

if not (R2_ENDPOINT and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY):
    raise ValueError("Missing R2 credentials in environment or .env file.")

s3_client = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4", max_pool_connections=20),
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

def upload_file(local_path: Path, r2_key: str):
    content_type = get_content_type(local_path)
    file_size = local_path.stat().st_size / (1024 * 1024)
    try:
        with open(local_path, "rb") as f:
            s3_client.put_object(
                Bucket=R2_BUCKET,
                Key=r2_key,
                Body=f,
                ContentType=content_type
            )
        return True, r2_key, f"{file_size:.2f} MB"
    except Exception as e:
        return False, r2_key, str(e)

def sync_data_folder():
    if not DATA_DIR.exists():
        print(f"Error: {DATA_DIR} does not exist.")
        return

    files_to_upload = []
    for root, _, files in os.walk(DATA_DIR):
        for f in files:
            if f.startswith("."):
                continue
            full_path = Path(root) / f
            rel_path = full_path.relative_to(APP_DIR).as_posix()
            files_to_upload.append((full_path, rel_path))

    print(f"🚀 Found {len(files_to_upload)} files in {DATA_DIR.name}/ to sync to R2 ({R2_BUCKET})...")

    success_count = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(upload_file, local, key): (local, key) for local, key in files_to_upload}
        for future in as_completed(futures):
            ok, key, msg = future.result()
            if ok:
                success_count += 1
                print(f"  ✅ Uploaded ({success_count}/{len(files_to_upload)}): {key} ({msg})")
            else:
                print(f"  ❌ FAILED: {key} -> {msg}")

    print(f"\n✨ Sync complete! Successfully uploaded {success_count}/{len(files_to_upload)} files to R2.")
    print(f"🌐 Public CDN Base: {CDN_PUBLIC_URL}")

if __name__ == "__main__":
    sync_data_folder()
