#!/usr/bin/env python3
"""Native messaging host: lets the Brave extension start the tracker app.

Brave runs this process when the popup calls
chrome.runtime.sendNativeMessage("com.jobtracker.launcher", {"cmd": "start"}).
Messages are 4-byte little-endian length + UTF-8 JSON, on stdin/stdout.

Commands:
  {"cmd": "start"}  -> start app/jobtracker.py in the background if not running
  {"cmd": "status"} -> report whether the app answers on localhost

Stopping is done by the extension over HTTP (POST /api/shutdown), not here.
Only the extension ID listed in the host manifest can talk to this script.
"""
import json
import os
import struct
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app", "jobtracker.py")
PORT = 8765


def read_message(stream):
    raw = stream.read(4)
    if len(raw) < 4:
        return None
    (n,) = struct.unpack("<I", raw)
    return json.loads(stream.read(n).decode("utf-8"))


def write_message(stream, obj):
    data = json.dumps(obj).encode("utf-8")
    stream.write(struct.pack("<I", len(data)))
    stream.write(data)
    stream.flush()


def is_running(port=PORT):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health",
                                     headers={"Host": f"127.0.0.1:{port}"})
        with urllib.request.urlopen(req, timeout=0.5) as r:
            return json.load(r).get("app") == "jobtracker"
    except Exception:
        return False


def python_no_console():
    """pythonw.exe on Windows so no console window appears."""
    exe = sys.executable
    if os.name == "nt":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe


def log_path():
    data = os.environ.get("JOBTRACKER_DATA") or os.path.join(
        os.environ.get("USERPROFILE") or os.path.expanduser("~"), "jobtracker-data")
    os.makedirs(data, exist_ok=True)
    return os.path.join(data, "server.log")


def start(port=PORT, wait=5.0):
    if is_running(port):
        return {"ok": True, "started": False, "message": "already running"}
    log = open(log_path(), "a", encoding="utf-8")
    cmd = [python_no_console(), APP, "--port", str(port)]
    kw = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, cwd=os.path.dirname(APP))
    if os.name == "nt":
        # Brave puts native hosts in a job object that is killed when the host
        # exits; break away so the app keeps running after this script ends.
        flags = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:
            subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kw)  # + CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            subprocess.Popen(cmd, creationflags=flags, **kw)
    else:
        subprocess.Popen(cmd, start_new_session=True, **kw)
    deadline = time.time() + wait
    while time.time() < deadline:
        if is_running(port):
            return {"ok": True, "started": True}
        time.sleep(0.1)
    return {"ok": False, "error": f"app did not come up within {wait:.0f}s; see {log_path()}"}


def handle(msg, port=PORT):
    cmd = (msg or {}).get("cmd")
    if cmd == "start":
        return start(port)
    if cmd == "status":
        return {"ok": True, "running": is_running(port)}
    return {"ok": False, "error": f"unknown command {cmd!r}"}


def main():
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    msg = read_message(sys.stdin.buffer)
    try:
        reply = handle(msg)
    except Exception as e:  # always answer, so the popup can show the error
        reply = {"ok": False, "error": repr(e)}
    write_message(sys.stdout.buffer, reply)


if __name__ == "__main__":
    main()
