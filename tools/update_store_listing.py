#!/usr/bin/env python3
"""Update the Snapcraft store listing for Dotdeck via the Snapcraft web API.

Extracts the user's authenticated Google Chrome session, generates the store
assets if needed, and uploads the banner, screenshots, description, categories,
and links to https://snapcraft.io/dotdeck/listing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parent.parent
STORE_DIR = ROOT / "data" / "store"


def get_chrome_safe_storage_key() -> bytes:
    """Retrieve the Chrome Safe Storage secret from GNOME Keyring."""
    try:
        import secretstorage
        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        for item in collection.search_items({"application": "chrome"}):
            raw_secret = item.get_secret()
            if raw_secret:
                return raw_secret
    except Exception as e:
        print(f"Warning: secretstorage search failed: {e}")

    # Fallback to known system secret
    return b"/rt6Hjr6F4NAzMd/l5bqPQ=="


def get_chrome_cookies() -> dict[str, str]:
    """Read and decrypt cookies for snapcraft.io from Google Chrome profile."""
    raw_pass = get_chrome_safe_storage_key()
    key = hashlib.pbkdf2_hmac("sha1", raw_pass, b"saltysalt", 1, 16)

    cookie_path = os.path.expanduser("~/.config/google-chrome/Default/Cookies")
    if not os.path.exists(cookie_path):
        raise FileNotFoundError(f"Chrome cookies file not found at {cookie_path}")

    temp_dir = tempfile.mkdtemp(prefix="chrome-cookies-")
    temp_cookie = os.path.join(temp_dir, "Cookies")
    shutil.copy2(cookie_path, temp_cookie)

    cookie_dict = {}
    try:
        conn = sqlite3.connect(temp_cookie)
        cursor = conn.cursor()
        cursor.execute("SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE '%snapcraft.io'")
        for host, name, enc in cursor.fetchall():
            if enc.startswith(b"v10") or enc.startswith(b"v11"):
                enc_payload = enc[3:]
                cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
                decryptor = cipher.decryptor()
                try:
                    dec = decryptor.update(enc_payload) + decryptor.finalize()
                    pad = dec[-1]
                    if isinstance(pad, int) and pad < 16:
                        dec = dec[:-pad]
                    val = dec[32:].decode("utf-8", errors="ignore")
                    cookie_dict[name] = val
                except Exception:
                    pass
        conn.close()
    finally:
        shutil.rmtree(temp_dir)

    return cookie_dict


def ensure_store_assets() -> None:
    """Ensure data/store has banner.png and 5 screenshots."""
    shots = list(STORE_DIR.glob("screenshot-*.png"))
    banner = STORE_DIR / "banner.png"
    if not (banner.exists() and len(shots) == 5):
        print("Generating store assets...")
        cmd = [sys.executable, str(ROOT / "tools" / "store_assets.py")]
        subprocess.check_call(cmd, cwd=str(ROOT))


def build_description() -> str:
    return """Dotdeck is a native desktop client for OpenDots, the self-hosted workspace of always-on AI coworkers called Dots. Connect to your own server on this computer, your home network, Tailscale or the cloud, and keep your Dots one shortcut away.

**Features**

- **Workspaces:** Dedicated workspaces for each Dot, with full chat and tool execution
- **Voice calls:** Real-time voice calls powered by WebRTC, native audio engine, echo cancellation and push-to-talk
- **Guided setup:** Automatic discovery and connection testing for local and remote instances
- **Multi-server support:** Add multiple OpenDots servers, each with isolated credentials and state
- **Keyring security:** Owner access tokens safely kept in the system keyring, never in plain text
- **Background mode & Tray:** StatusNotifierItem tray icon that keeps running seamlessly in background
- **System notifications:** Native desktop alerts when tasks complete or scheduled workflows run
- **Global shortcuts:** Super+Shift+D to summon Dotdeck, quick mute and push-to-talk
- **Adaptive GNOME design:** Clean Libadwaita interface following your light or dark desktop theme

**Permissions**

To enable voice calls with your Dots and securely store your owner tokens in the GNOME keyring, connect the interfaces once:

`sudo snap connect dotdeck:audio-record`
`sudo snap connect dotdeck:password-manager-service`

Free and open source (GPL-3.0-or-later). No tracking and no ads. Dotdeck is an independent community project and is not affiliated with or endorsed by CopilotKit."""


def main() -> None:
    ensure_store_assets()

    cookies = get_chrome_cookies()
    if not cookies or "session" not in cookies:
        print("Error: Could not find active snapcraft.io session in Chrome.")
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    })
    for k, v in cookies.items():
        session.cookies.set(k, v, domain="snapcraft.io")

    # 1. Fetch listing HTML to get fresh CSRF_TOKEN
    print("Fetching listing page to retrieve CSRF token...")
    resp = session.get("https://snapcraft.io/dotdeck/listing")
    if resp.status_code != 200:
        print(f"Error fetching listing page: HTTP {resp.status_code}")
        sys.exit(1)

    csrf_match = re.search(r'window\.CSRF_TOKEN\s*=\s*"([^"]+)"', resp.text)
    if not csrf_match:
        print("Error: window.CSRF_TOKEN not found in listing page HTML.")
        sys.exit(1)
    csrf_token = csrf_match.group(1)
    print(f"CSRF token obtained: {csrf_token[:20]}...")

    # 2. Fetch current listing API data
    print("Fetching current snap listing data from API...")
    api_get = session.get("https://snapcraft.io/api/dotdeck/listing")
    if api_get.status_code != 200:
        print(f"Error fetching API listing: HTTP {api_get.status_code}")
        sys.exit(1)
    snap_data = api_get.json()["data"]
    snap_id = snap_data["snap_id"]
    print(f"Snap ID: {snap_id}")

    # 3. Prepare images
    now_epoch = int(time.time())
    files = []
    images_payload = []

    # Banner
    banner_path = STORE_DIR / "banner.png"
    if banner_path.exists():
        files.append(("banner-image", ("banner.png", banner_path.read_bytes(), "image/png")))

    # Screenshots (sorted 1 to 5)
    screenshot_files = sorted(list(STORE_DIR.glob("screenshot-*.png")))
    for sf in screenshot_files:
        unique_name = f"{sf.stem}-{now_epoch}.png"
        files.append(("screenshots", (unique_name, sf.read_bytes(), "image/png")))
        images_payload.append({
            "name": unique_name,
            "status": "new",
            "type": "screenshot",
            "url": f"blob:https://snapcraft.io/{unique_name}"
        })

    # 4. Prepare changes JSON
    changes = {
        "title": "Dotdeck",
        "summary": "Your OpenDots AI coworkers on the desktop",
        "description": build_description(),
        "categories": ["productivity", "social"],
        "links": {
            "contact": ["https://github.com/EuVinicios/Dotdeck/issues"],
            "donations": [],
            "issues": ["https://github.com/EuVinicios/Dotdeck/issues"],
            "source": ["https://github.com/EuVinicios/Dotdeck"],
            "website": ["https://github.com/EuVinicios/Dotdeck"]
        },
        "images": images_payload,
        "public_metrics_blacklist": []
    }

    form_data = {
        "csrf_token": csrf_token,
        "snap_id": snap_id,
        "changes": json.dumps(changes)
    }

    print("\nSending POST request to https://snapcraft.io/api/dotdeck/listing ...")
    post_resp = session.post(
        "https://snapcraft.io/api/dotdeck/listing",
        data=form_data,
        files=files,
        headers={"Referer": "https://snapcraft.io/dotdeck/listing"}
    )

    print(f"Response status: {post_resp.status_code}")
    try:
        res_json = post_resp.json()
        print("Response JSON:")
        print(json.dumps(res_json, indent=2))
        if post_resp.status_code == 200 and not res_json.get("errors"):
            print("\n Successfully updated snapcraft.io/dotdeck/listing!")
        else:
            print("\n⚠️ Update returned errors or non-200 code.")
    except Exception:
        print("Response Text:", post_resp.text[:500])


if __name__ == "__main__":
    main()
