"""
download_redis.py — fetches redis-server.exe for bundling into the installer.
Downloads from tporadowski/redis (the maintained Windows port).
Run automatically by build.bat; safe to run manually too.
"""
import sys
import io
import zipfile
import urllib.request
from pathlib import Path

REDIS_VERSION = "5.0.14.1"
REDIS_URL = (
    f"https://github.com/tporadowski/redis/releases/download/"
    f"v{REDIS_VERSION}/Redis-x64-{REDIS_VERSION}.zip"
)
OUT_DIR = Path(__file__).parent / "redis_bundled"
TARGET  = OUT_DIR / "redis-server.exe"


def download_redis():
    OUT_DIR.mkdir(exist_ok=True)

    if TARGET.exists():
        print(f"  redis-server.exe already present — skipping download.")
        return

    print(f"  Downloading Redis {REDIS_VERSION} for Windows...")
    print(f"  Source: {REDIS_URL}")
    try:
        with urllib.request.urlopen(REDIS_URL, timeout=60) as resp:
            data = resp.read()
    except Exception as e:
        print(f"ERROR: Download failed: {e}")
        sys.exit(1)

    print("  Extracting redis-server.exe...")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        found = False
        for name in zf.namelist():
            if name.lower().endswith("redis-server.exe"):
                raw = zf.read(name)
                TARGET.write_bytes(raw)
                found = True
                break
        if not found:
            print("ERROR: redis-server.exe not found inside zip.")
            sys.exit(1)

    print(f"  Done: {TARGET}  ({TARGET.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    download_redis()
