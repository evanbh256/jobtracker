#!/usr/bin/env python3
"""Local internship application tracker.

Single-file, stdlib-only server. Stores applications in a CSV and each job
description in its own text file beside it. Listens on 127.0.0.1 only.

Usage:
    python jobtracker.py [--data DIR] [--port 8765] [--open]

Data dir resolution: --data, then JOBTRACKER_DATA env var, then
%USERPROFILE%\\jobtracker-data (or ~/jobtracker-data).
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import sys
import threading
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEFAULT_PORT = 8765

FIELDS = [
    "id", "company", "title", "location", "url", "source", "job_id",
    "posted_date", "date_applied", "status", "status_changed_by",
    "repost_of", "notes", "jd_file", "updated_at",
]
CHANGE_FIELDS = ["timestamp", "app_id", "field", "old", "new", "actor"]
STATUSES = ["Applied", "Interview", "Offer", "Rejected", "Ghosted"]
EDITABLE = {"company", "title", "location", "url", "source", "job_id",
            "posted_date", "date_applied", "status", "notes"}


class StoreError(Exception):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def now_iso():
    return dt.datetime.now().replace(microsecond=0).isoformat()


def today():
    return dt.date.today()


def slug(text, maxlen=40):
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return s[:maxlen].strip("-") or "x"


class Store:
    """CSV-backed store. All access goes through one lock; writes are atomic."""

    def __init__(self, data_dir, today_fn=today):
        self.data_dir = os.path.abspath(data_dir)
        self.csv_path = os.path.join(self.data_dir, "applications.csv")
        self.changes_path = os.path.join(self.data_dir, "changes.csv")
        self.jd_dir = os.path.join(self.data_dir, "jd")
        self.trash_dir = os.path.join(self.data_dir, "trash")
        self.today = today_fn
        self.lock = threading.RLock()
        os.makedirs(self.jd_dir, exist_ok=True)
        os.makedirs(self.trash_dir, exist_ok=True)
        if not os.path.exists(self.csv_path):
            self._write_rows([])
        else:
            self._check_header()
        if not os.path.exists(self.changes_path):
            with open(self.changes_path, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(CHANGE_FIELDS)

    # ---- file IO -------------------------------------------------------
    def _check_header(self):
        with open(self.csv_path, newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f), [])
        if header != FIELDS:
            raise SystemExit(
                f"{self.csv_path} exists but is not a jobtracker CSV "
                f"(header {header!r}). Refusing to touch it; pick another --data dir.")

    def _read_rows(self):
        with open(self.csv_path, newline="", encoding="utf-8-sig") as f:
            return [{k: (r.get(k) or "") for k in FIELDS} for r in csv.DictReader(f)]

    def _write_rows(self, rows):
        tmp = self.csv_path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, quoting=csv.QUOTE_MINIMAL)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in FIELDS})
        try:
            os.replace(tmp, self.csv_path)
        except PermissionError:
            os.remove(tmp)
            raise StoreError("applications.csv is locked (open in Excel?). Close it and retry.",
                             HTTPStatus.SERVICE_UNAVAILABLE)

    def _log(self, app_id, field, old, new, actor):
        with open(self.changes_path, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow([now_iso(), app_id, field, old, new, actor])

    def _jd_abs(self, rel):
        path = os.path.normpath(os.path.join(self.data_dir, rel))
        if not path.startswith(self.data_dir + os.sep):
            raise StoreError("bad jd path")
        return path

    # ---- operations ----------------------------------------------------
    def list(self):
        with self.lock:
            return self._read_rows()

    def get(self, app_id):
        for r in self.list():
            if r["id"] == app_id:
                return r
        raise StoreError("not found", HTTPStatus.NOT_FOUND)

    def read_jd(self, app_id):
        row = self.get(app_id)
        if not row["jd_file"]:
            return ""
        try:
            with open(self._jd_abs(row["jd_file"]), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def add(self, data):
        """Create an application from a capture payload. Returns the new row."""
        company = (data.get("company") or "").strip()
        title = (data.get("title") or "").strip()
        if not company and not title:
            raise StoreError("capture needs at least a company or a title")
        with self.lock:
            rows = self._read_rows()
            app_id = uuid.uuid4().hex[:10]
            applied = str(data.get("date_applied") or "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", applied):
                applied = self.today().isoformat()
            row = {k: "" for k in FIELDS}
            for k in ("location", "url", "source", "job_id", "posted_date", "notes", "repost_of"):
                row[k] = str(data.get(k) or "").strip()
            row.update(id=app_id, company=company, title=title, date_applied=applied,
                       status="Applied", status_changed_by="capture", updated_at=now_iso())
            jd = data.get("jd_text") or ""
            if jd.strip():
                rel = f"jd/{applied}_{slug(company, 30)}_{slug(title)}_{app_id}.txt"
                with open(self._jd_abs(rel), "w", encoding="utf-8", newline="\n") as f:
                    f.write(jd)
                row["jd_file"] = rel
            rows.append(row)
            try:
                self._write_rows(rows)
            except StoreError:
                if row["jd_file"]:
                    os.remove(self._jd_abs(row["jd_file"]))
                raise
            self._log(app_id, "created", "", f"{company} | {title}", "capture")
            return row

    def update(self, app_id, changes, actor="user"):
        bad = set(changes) - EDITABLE
        if bad:
            raise StoreError(f"not editable: {', '.join(sorted(bad))}")
        if "status" in changes and changes["status"] not in STATUSES:
            raise StoreError(f"unknown status {changes['status']!r}")
        with self.lock:
            rows = self._read_rows()
            row = next((r for r in rows if r["id"] == app_id), None)
            if row is None:
                raise StoreError("not found", HTTPStatus.NOT_FOUND)
            log = []
            for k, v in changes.items():
                v = "" if v is None else str(v)
                if row[k] != v:
                    log.append((k, row[k], v))
                    row[k] = v
            if any(k == "status" for k, _, _ in log):
                row["status_changed_by"] = actor
            if log:
                row["updated_at"] = now_iso()
                self._write_rows(rows)
                for k, old, new in log:
                    self._log(app_id, k, old, new, actor)
            return row

    def delete(self, app_id):
        with self.lock:
            rows = self._read_rows()
            row = next((r for r in rows if r["id"] == app_id), None)
            if row is None:
                raise StoreError("not found", HTTPStatus.NOT_FOUND)
            self._write_rows([r for r in rows if r["id"] != app_id])
            if row["jd_file"]:
                src = self._jd_abs(row["jd_file"])
                if os.path.exists(src):
                    shutil.move(src, os.path.join(self.trash_dir, os.path.basename(src)))
            self._log(app_id, "deleted", f"{row['company']} | {row['title']}", "", "user")
            return row

    def restore(self, row):
        """Undo a delete: re-insert the exact row and move its JD back."""
        row = {k: str(row.get(k) or "") for k in FIELDS}
        if not re.fullmatch(r"[0-9a-f]{10}", row["id"]):
            raise StoreError("bad id")
        with self.lock:
            rows = self._read_rows()
            if any(r["id"] == row["id"] for r in rows):
                raise StoreError("already exists", HTTPStatus.CONFLICT)
            if row["jd_file"]:
                dst = self._jd_abs(row["jd_file"])
                src = os.path.join(self.trash_dir, os.path.basename(dst))
                if os.path.exists(src):
                    shutil.move(src, dst)
            rows.append(row)
            self._write_rows(rows)
            self._log(row["id"], "restored", "", f"{row['company']} | {row['title']}", "user")
            return row


# ---- HTTP --------------------------------------------------------------

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def make_handler(store, port):
    allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    allowed_origins = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "jobtracker"

        def log_message(self, fmt, *args):
            pass

        # -- guards: block random websites and DNS rebinding from poking the API
        def _origin_ok(self):
            if self.headers.get("Host", "") not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            return origin in allowed_origins or origin.startswith("chrome-extension://")

        def _send(self, status, body, ctype="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            origin = self.headers.get("Origin")
            if origin and origin.startswith("chrome-extension://"):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 5_000_000:
                raise StoreError("payload too large")
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                raise StoreError("invalid JSON")

        def do_OPTIONS(self):
            if not self._origin_ok():
                return self._send(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            self.send_response(HTTPStatus.NO_CONTENT)
            origin = self.headers.get("Origin", "")
            if origin.startswith("chrome-extension://"):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _dispatch(self, method):
            if not self._origin_ok():
                return self._send(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            path = urlparse(self.path).path
            try:
                result = self.route(method, path)
            except StoreError as e:
                return self._send(e.status, {"error": str(e)})
            except Exception as e:  # keep the server alive, report the error
                return self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": repr(e)})
            if result is not None:
                self._send(*result)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def route(self, method, path):
            m = re.fullmatch(r"/api/apps/([0-9a-f]{10})(/jd)?", path)
            if method == "GET":
                if path == "/api/health":
                    return 200, {"ok": True, "app": "jobtracker"}
                if path == "/api/apps":
                    return 200, {"apps": store.list(), "statuses": STATUSES}
                if m and m.group(2):
                    return 200, {"text": store.read_jd(m.group(1))}
                return self.static(path)
            if method == "POST" and path == "/api/capture":
                return 201, {"result": "added", "app": store.add(self._json_body())}
            if method == "POST" and path == "/api/restore":
                return 201, {"app": store.restore(self._json_body())}
            if method == "PATCH" and m and not m.group(2):
                return 200, {"app": store.update(m.group(1), self._json_body())}
            if method == "DELETE" and m and not m.group(2):
                return 200, {"app": store.delete(m.group(1))}
            raise StoreError("no such route", HTTPStatus.NOT_FOUND)

        def static(self, path):
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC_DIR, name))
            if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
                raise StoreError("not found", HTTPStatus.NOT_FOUND)
            with open(full, "rb") as f:
                body = f.read()
            ctype = STATIC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
            return 200, body, ctype

    return Handler


def default_data_dir():
    env = os.environ.get("JOBTRACKER_DATA")
    if env:
        return env
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "jobtracker-data")


def make_server(data_dir, port=DEFAULT_PORT, today_fn=today):
    store = Store(data_dir, today_fn=today_fn)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, port))
    return httpd, store


def main(argv=None):
    p = argparse.ArgumentParser(description="Local internship application tracker")
    p.add_argument("--data", default=default_data_dir(), help="data folder (CSV + jd/)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--open", action="store_true", help="open the board in the browser")
    args = p.parse_args(argv)
    try:
        httpd, store = make_server(args.data, args.port)
    except OSError as e:
        print(f"Could not listen on 127.0.0.1:{args.port} ({e}). Already running?")
        if args.open:
            webbrowser.open(f"http://localhost:{args.port}/")
        return 1
    url = f"http://localhost:{args.port}/"
    print(f"jobtracker running at {url}\ndata: {store.data_dir}\nCtrl+C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
