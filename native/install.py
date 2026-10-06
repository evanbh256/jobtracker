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
    print("Reload the extension in brave://extensions, then open its popup.")


if __name__ == "__main__":
    main()
