#!/usr/bin/env python3
"""One-time setup (Windows): register the native host so the extension can start the app.

Writes native/host.bat and native/com.jobtracker.launcher.json, then points
Brave's and Chrome's per-user registry keys at the JSON. No admin rights needed.
Run with --uninstall to remove the registry keys.
"""
import json
import os
import sys

NAME = "com.jobtracker.launcher"
EXTENSION_ID = "ljffoebffpkpgcmehjgdjopdjoiabfhf"  # fixed by "key" in extension/manifest.json
HERE = os.path.dirname(os.path.abspath(__file__))
KEYS = [
    rf"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\{NAME}",
    rf"Software\Google\Chrome\NativeMessagingHosts\{NAME}",
]


def self_test(bat):
    """Run host.bat exactly like Brave does (framed JSON on stdin) and show the reply."""
    import struct
    import subprocess
    msg = json.dumps({"cmd": "status"}).encode()
    try:
        p = subprocess.run(["cmd", "/c", bat], input=struct.pack("<I", len(msg)) + msg,
                           capture_output=True, timeout=20)
    except Exception as e:
        print(f"SELF-TEST FAILED: could not run {bat}: {e!r}")
        return
    out = p.stdout
    try:
        (n,) = struct.unpack("<I", out[:4])
        print("Self-test OK, launcher replied:", json.loads(out[4:4 + n]))
    except Exception:
        print("SELF-TEST FAILED. Launcher output was:")
        print("  stdout:", out[:500])
        print("  stderr:", p.stderr.decode(errors="replace")[:2000])


def main():
    if os.name != "nt":
        sys.exit("This installer is for Windows.")
    import winreg
    if "--uninstall" in sys.argv:
        for k in KEYS:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)
            except FileNotFoundError:
                pass
        print("Launcher unregistered.")
        return
    bat = os.path.join(HERE, "host.bat")
    with open(bat, "w", newline="\r\n") as f:
        # Absolute path to this Python, so PATH doesn't matter at launch time.
        f.write(f'@echo off\n"{sys.executable}" -u "{os.path.join(HERE, "host.py")}" %*\n')
    manifest = os.path.join(HERE, NAME + ".json")
    with open(manifest, "w") as f:
        json.dump({
            "name": NAME,
            "description": "Starts the local Job Tracker app",
            "path": bat,
            "type": "stdio",
            "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
        }, f, indent=2)
    for k in KEYS:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, k) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, manifest)
    print(f"Launcher registered for extension {EXTENSION_ID}.")
    for k in KEYS:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, k) as key:
            print(f"  HKCU\\{k} -> {winreg.QueryValueEx(key, '')[0]}")
    self_test(bat)
    print("Reload the extension in brave://extensions, then open its popup.")


if __name__ == "__main__":
    main()
