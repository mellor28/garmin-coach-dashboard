#!/usr/bin/env python3
"""Encrypt the generated dashboard data and optionally publish GitHub Pages."""
import argparse
import base64
import getpass
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


BASE = Path(__file__).resolve().parent
LOCAL_DASHBOARD = BASE / "dashboard.html"
PAGES_DIR = BASE / "docs"
PAGES_INDEX = PAGES_DIR / "index.html"
ENCRYPTED_DATA = PAGES_DIR / "dashboard-data.enc.json"
PAGES_CONFIG = BASE / ".pages-config.json"
KEYCHAIN_SERVICE = "com.run-atlas.github-pages"
KEYCHAIN_ACCOUNT = getpass.getuser()
PBKDF2_ITERATIONS = 600_000

DATA_PATTERN = re.compile(
    r'(<script id="dashboard-data" type="application/json">)(.*?)(</script>)',
    re.DOTALL,
)

PWA_HEAD = """\
<meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#101210">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Run Atlas">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="unlock.css">
"""

UNLOCK_MARKUP = """\
<div class="unlock-screen" id="unlock-screen">
  <main class="unlock-card">
    <div class="unlock-mark">RA</div>
    <h1>Unlock Run Atlas</h1>
    <p>Your Garmin data is encrypted. It will be decrypted only inside this browser.</p>
    <form id="unlock-form">
      <label for="unlock-password">Dashboard password</label>
      <div class="unlock-row">
        <input id="unlock-password" type="password" autocomplete="current-password"
               autocapitalize="none" spellcheck="false" required>
        <button id="unlock-submit" type="submit">Unlock</button>
      </div>
      <div class="unlock-error" id="unlock-error" role="alert" aria-live="polite"></div>
    </form>
    <div class="unlock-privacy">
      The password and decrypted data never leave this device. Safari can save
      the password in iCloud Keychain for Face ID-assisted access.
    </div>
  </main>
</div>
"""


def run(command, *, check=True, capture_output=False):
    return subprocess.run(
        command,
        cwd=BASE,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def keychain_password(create=False):
    lookup = subprocess.run(
        [
            "security", "find-generic-password",
            "-s", KEYCHAIN_SERVICE,
            "-a", KEYCHAIN_ACCOUNT,
            "-w",
        ],
        text=True,
        capture_output=True,
    )
    if lookup.returncode == 0:
        return lookup.stdout.rstrip("\n")
    if not create:
        raise RuntimeError(
            "No dashboard encryption password is stored. "
            "Run publish_dashboard.py --setup-key in Terminal first."
        )
    if not sys.stdin.isatty():
        raise RuntimeError("Password setup requires an interactive Terminal.")

    while True:
        first = getpass.getpass("Create dashboard password (16+ characters): ")
        if len(first) < 16:
            print("Use at least 16 characters.", file=sys.stderr)
            continue
        second = getpass.getpass("Confirm dashboard password: ")
        if first != second:
            print("Passwords did not match.", file=sys.stderr)
            continue
        break

    run([
        "security", "add-generic-password", "-U",
        "-s", KEYCHAIN_SERVICE,
        "-a", KEYCHAIN_ACCOUNT,
        "-w", first,
    ])
    print("Encryption password saved in macOS Keychain.")
    return first


def extract_dashboard():
    if not LOCAL_DASHBOARD.exists():
        raise RuntimeError("dashboard.html is missing; run the refresh first.")
    html = LOCAL_DASHBOARD.read_text()
    match = DATA_PATTERN.search(html)
    if not match:
        raise RuntimeError("Embedded dashboard data was not found.")
    plaintext = match.group(2).encode("utf-8")

    shell = DATA_PATTERN.sub(r"\1\3", html, count=1)
    if "window.startDashboard = startDashboard" not in shell:
        raise RuntimeError("Dashboard template does not expose the secure startup hook.")
    shell = shell.replace("<head>", "<head>\n" + PWA_HEAD, 1)
    shell = shell.replace("<body>", '<body class="locked">\n' + UNLOCK_MARKUP, 1)
    shell = shell.replace("</body>", '<script src="unlock.js"></script>\n</body>', 1)
    return plaintext, shell


def encrypt(plaintext, passphrase):
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    ).derive(passphrase.encode("utf-8"))
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)
    return {
        "version": 1,
        "cipher": "AES-256-GCM",
        "kdf": "PBKDF2-SHA256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_secure_pages(passphrase):
    plaintext, shell = extract_dashboard()
    envelope = encrypt(plaintext, passphrase)
    PAGES_DIR.mkdir(exist_ok=True)
    PAGES_INDEX.write_text(shell)
    ENCRYPTED_DATA.write_text(json.dumps(envelope, separators=(",", ":")))

    # Verify the public shell has no embedded data and the ciphertext can be
    # decrypted with the Keychain password before anything is committed.
    if DATA_PATTERN.search(shell).group(2):
        raise RuntimeError("Public index unexpectedly contains embedded dashboard data.")
    salt = base64.b64decode(envelope["salt"])
    iv = base64.b64decode(envelope["iv"])
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=envelope["iterations"],
    ).derive(passphrase.encode("utf-8"))
    recovered = AESGCM(key).decrypt(iv, base64.b64decode(envelope["ciphertext"]), None)
    if recovered != plaintext:
        raise RuntimeError("Encrypted dashboard verification failed.")
    print(f"Encrypted dashboard written to {PAGES_DIR}")


def publish_commit():
    if not (BASE / ".git").exists():
        raise RuntimeError("Git repository is not configured yet.")
    run(["git", "add", "docs"])
    changed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=BASE,
    ).returncode != 0
    if not changed:
        print("Encrypted dashboard is already current.")
        return
    run(["git", "commit", "-m", "Update encrypted dashboard"])
    run(["git", "push"])
    print("Encrypted dashboard published to GitHub Pages.")


def publish(*, push=False, allow_key_setup=False):
    passphrase = keychain_password(create=allow_key_setup)
    write_secure_pages(passphrase)
    if push:
        publish_commit()


def is_pages_configured():
    return PAGES_CONFIG.exists()


def main():
    parser = argparse.ArgumentParser(
        description="Encrypt dashboard data and publish the GitHub Pages site."
    )
    parser.add_argument(
        "--setup-key",
        action="store_true",
        help="Create and store the encryption password in macOS Keychain",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Commit and push the encrypted Pages output",
    )
    args = parser.parse_args()

    try:
        publish(push=args.push, allow_key_setup=args.setup_key)
    except KeyboardInterrupt:
        print("\nPublishing cancelled.", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f"\nPublishing failed: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
