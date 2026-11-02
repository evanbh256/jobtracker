#!/usr/bin/env python3
"""Local internship application tracker.

Single-file, stdlib-only server. Stores applications in a CSV and each job
description in its own text file beside it. Listens on 127.0.0.1 only.

Usage:
    python jobtracker.py [--data DIR] [--leads DIR] [--port 8765] [--open]

Data dir resolution: --data, then JOBTRACKER_DATA env var, then J:\\jobtracker-data
(the shared folder; JOBTRACKER_SHARE changes J:\\).
"""
import argparse
import csv
import difflib
import datetime as dt
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
# Shared job-search folder on the laptop, mapped as J: over Tailscale. It holds the
# agent's night-hunt-log.csv and the board's data (J:\jobtracker-data).
SHARE_DIR = os.environ.get("JOBTRACKER_SHARE") or "J:\\"
SHARE_HINT = "check that Tailscale is running and the laptop is on"
DEFAULT_PORT = 8765

FIELDS = [
    "id", "company", "title", "location", "url", "source", "job_id",
    "posted_date", "date_applied", "status", "status_changed_by",
    "repost_of", "notes", "jd_file", "updated_at",
]
CHANGE_FIELDS = ["timestamp", "app_id", "field", "old", "new", "actor"]
STATUSES = ["Applied", "Interview", "Offer", "Rejected", "Ghosted"]
GHOST_AFTER_DAYS = 90
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


# ---- repost matching ----------------------------------------------------

COMPANY_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
                    "co", "company", "plc", "gmbh", "lp", "llp", "the", "group", "holdings"}
TITLE_NOISE = re.compile(
    r"\b(summer|fall|autumn|winter|spring|20\d\d|\d{4}\s*-\s*\d{2,4}|"
    r"remote|hybrid|onsite|on-site|new grad)\b", re.I)


def norm_company(s):
    s = (s or "").lower().replace("&", " and ")
    words = re.sub(r"[^a-z0-9]+", " ", s).split()
    while words and words[-1] in COMPANY_SUFFIXES:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def norm_title(s):
    s = TITLE_NOISE.sub(" ", (s or "").lower())
    s = re.sub(r"\b(internship)\b", "intern", s)
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", s).split())


def norm_url(u):
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://(www\.)?", "", u).split("#")[0]
    return u.rstrip("/")


def similar(a, b, threshold):
    if not a or not b:
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def find_reposts(cap, rows):
    """Existing rows that look like the same posting as capture `cap`."""
    c_co, c_ti = norm_company(cap.get("company")), norm_title(cap.get("title"))
    c_url, c_id = norm_url(cap.get("url")), (cap.get("job_id") or "").strip().lower()
    out = []
    for r in rows:
        r_co = norm_company(r["company"])
        same_co = similar(c_co, r_co, 0.9)
        if c_id and c_id == r["job_id"].strip().lower() and (same_co or cap.get("source") == r["source"]):
            reason = "same job ID"
        elif c_url and c_url == norm_url(r["url"]):
            reason = "same URL"
        elif same_co and similar(c_ti, norm_title(r["title"]), 0.9):
            reason = "same company and title"
        else:
            continue
        out.append({"id": r["id"], "company": r["company"], "title": r["title"],
                    "date_applied": r["date_applied"], "status": r["status"], "reason": reason})
    return out


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
            self.sweep()
            return self._read_rows()

    def sweep(self):
        """Auto-Ghost: Applied for 90+ days moves to Ghosted, logged as actor 'auto'.

        A card you explicitly put back into Applied is left alone."""
        with self.lock:
            rows = self._read_rows()
            cutoff = self.today() - dt.timedelta(days=GHOST_AFTER_DAYS)
            moved = []
            for r in rows:
                if r["status"] != "Applied" or r["status_changed_by"] == "user":
                    continue
                try:
                    applied = dt.date.fromisoformat(r["date_applied"])
                except ValueError:
                    continue
                if applied <= cutoff:
                    r["status"], r["status_changed_by"], r["updated_at"] = "Ghosted", "auto", now_iso()
                    moved.append(r["id"])
            if moved:
                self._write_rows(rows)
                for app_id in moved:
                    self._log(app_id, "status", "Applied", "Ghosted", "auto")
            return moved

    def search(self, q):
        """Ids whose company/title/location/notes/JD contain every term (case-insensitive)."""
        terms = [t for t in q.lower().split() if t]
        hits = []
        for r in self.list():
            hay = " ".join([r["company"], r["title"], r["location"], r["notes"]]).lower()
            if not all(t in hay for t in terms):
                hay += " " + self.read_jd_row(r).lower()
            if all(t in hay for t in terms):
                hits.append(r["id"])
        return hits

    def get(self, app_id):
        for r in self.list():
            if r["id"] == app_id:
                return r
        raise StoreError("not found", HTTPStatus.NOT_FOUND)

    def read_jd(self, app_id):
        return self.read_jd_row(self.get(app_id))

    def read_jd_row(self, row):
        if not row["jd_file"]:
            return ""
        try:
            with open(self._jd_abs(row["jd_file"]), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def add(self, data):
        """Create an application from a capture payload.

        Returns {"result": "possible_repost", "matches": [...]} without writing
        anything when it looks like an existing entry, unless data["force"].
        Otherwise {"result": "added", "app": row}."""
        company = (data.get("company") or "").strip()
        title = (data.get("title") or "").strip()
        if not company and not title:
            raise StoreError("capture needs at least a company or a title")
        with self.lock:
            rows = self._read_rows()
            matches = find_reposts(data, rows)
            if matches and not data.get("force"):
                return {"result": "possible_repost", "matches": matches}
            app_id = uuid.uuid4().hex[:10]
            applied = str(data.get("date_applied") or "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", applied):
                applied = self.today().isoformat()
            row = {k: "" for k in FIELDS}
            for k in ("location", "url", "source", "job_id", "posted_date", "notes"):
                row[k] = str(data.get(k) or "").strip()
            row["repost_of"] = matches[0]["id"] if matches else ""
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
            if matches:
                self._log(app_id, "repost_of", "", matches[0]["id"], "user")
            return {"result": "added", "app": row}

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


# ---- leads (night-hunt agent CSV) ---------------------------------------

LEAD_FIELDS = [
    "date", "company", "title", "location", "job_link", "contact_name", "contact_school",
    "contact_linkedin_url", "contact_email_or_blank", "draft_created", "draft_subject",
    "notes", "reply_status", "reply_date", "followup_draft", "application_filled",
]
LEAD_EDITABLE = {"location", "job_link", "contact_name", "contact_school", "contact_linkedin_url",
                 "contact_email_or_blank", "draft_created", "draft_subject", "notes",
                 "reply_status", "reply_date", "followup_draft", "application_filled"}
LEADS_FILE = "night-hunt-log.csv"
REAL_JOB_LINK = re.compile(r"linkedin\.com/jobs/view/\d+", re.I)


def lead_key(r):
    """Stable id for a lead: the agent appends rows, so these four columns don't change."""
    raw = "|".join((r.get(k) or "").strip().lower() for k in ("date", "company", "title", "location"))
    return uuid.uuid5(uuid.NAMESPACE_URL, raw).hex[:12]


class Leads:
    """The shared night-hunt-log.csv, read fresh on every request.

    It is the single source of truth (the agent appends to it, Filebrowser and
    Excel edit it), so it is only ever rewritten in place: never replaced via a
    temp file, which would break the laptop's symlink and the file's
    owner/permissions. The first write in each app run makes a backup."""

    WRITE_TRIES, WRITE_WAIT = 10, 0.5  # ~5 s of retrying while Excel holds the file

    def __init__(self, data_dir, store, leads_dir=SHARE_DIR):
        self.dir = os.path.abspath(leads_dir)
        self.path = os.path.join(self.dir, LEADS_FILE)
        self.backup_dir = os.path.join(self.dir, "backups")
        self.backed_up = False
        self.store = store
        self.lock = threading.RLock()

    def reachable(self):
        return os.path.isdir(self.dir)

    def _read(self):
        if not os.path.exists(self.path):
            return None, []
        with open(self.path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            missing = [k for k in ("date", "company", "title") if k not in header]
            if missing:
                raise StoreError(f"{LEADS_FILE} is missing columns: {', '.join(missing)}")
            rows = [{k: (r.get(k) or "") for k in header} for r in reader]
        return header, [r for r in rows if any(v.strip() for v in r.values())]

    def _backup_once(self):
        if self.backed_up:
            return
        os.makedirs(self.backup_dir, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copyfile(self.path, os.path.join(self.backup_dir, f"night-hunt-log-{stamp}.csv"))
        self.backed_up = True

    def _write(self, header, rows):
        """Rewrite the existing file in place, keeping its BOM and line endings."""
        with open(self.path, "rb") as f:
            raw = f.read()
        bom = raw.startswith(b"\xef\xbb\xbf")
        eol = "\r\n" if b"\r\n" in raw else "\n"
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore", lineterminator=eol)
        w.writeheader()
        w.writerows(rows)
        data = (b"\xef\xbb\xbf" if bom else b"") + buf.getvalue().encode("utf-8")
        self._backup_once()
        for attempt in range(self.WRITE_TRIES):
            try:
                with open(self.path, "r+b") as f:
                    f.write(data)
                    f.truncate()
                return
            except PermissionError:
                if attempt + 1 < self.WRITE_TRIES:
                    time.sleep(self.WRITE_WAIT)
        raise StoreError(f"{LEADS_FILE} is locked (open in Excel?). Close it and retry.",
                         HTTPStatus.SERVICE_UNAVAILABLE)

    def _load(self):
        if not self.reachable():
            raise StoreError(f"{self.dir} not reachable: {SHARE_HINT}.", HTTPStatus.SERVICE_UNAVAILABLE)
        return self._read()

    def _flags(self, rows):
        apps = self.store.list()
        out = []
        for i, r in enumerate(rows):
            flags = []
            link = r.get("job_link", "")
            if not REAL_JOB_LINK.search(link):
                flags.append({"kind": "bad_link", "text": "search link only, find the posting"})
            co, ti = norm_company(r["company"]), norm_title(r["title"])
            for j, o in enumerate(rows):
                oti = norm_title(o["title"])
                same_ti = similar(ti, oti, 0.8) or (ti and oti and (
                    set(ti.split()) <= set(oti.split()) or set(oti.split()) <= set(ti.split())))
                if j != i and same_ti and similar(co, norm_company(o["company"]), 0.9):
                    flags.append({"kind": "duplicate",
                                  "text": f"also logged {o['date']} ({o.get('location', '')})"})
                    break
            m = find_reposts({"company": r["company"], "title": r["title"],
                              "url": link if REAL_JOB_LINK.search(link) else ""}, apps)
            if m:
                flags.append({"kind": "tracked", "app_id": m[0]["id"],
                              "text": f"in tracker: {m[0]['status']} since {m[0]['date_applied']}"})
            if r.get("draft_created", "").lower() == "yes" and r.get("reply_status", "none") in ("", "none"):
                flags.append({"kind": "draft_ready", "text": "draft ready to send"})
            out.append(flags)
        return out

    def list(self):
        if not self.reachable():
            return {"found": False, "reachable": False, "path": self.path, "hint": SHARE_HINT, "leads": []}
        with self.lock:
            header, rows = self._load()
        if header is None:
            return {"found": False, "reachable": True, "path": self.path, "leads": []}
        flags = self._flags(rows)
        leads = [dict(r, key=lead_key(r), flags=f) for r, f in zip(rows, flags)]
        return {"found": True, "reachable": True, "path": self.path, "columns": header, "leads": leads,
                "modified": dt.datetime.fromtimestamp(os.path.getmtime(self.path)).replace(microsecond=0).isoformat()}

    def update(self, key, changes):
        bad = set(changes) - LEAD_EDITABLE
        if bad:
            raise StoreError(f"not editable: {', '.join(sorted(bad))}")
        with self.lock:
            header, rows = self._load()
            row = next((r for r in rows if lead_key(r) == key), None)
            if row is None:
                raise StoreError("no such lead", HTTPStatus.NOT_FOUND)
            for field, value in changes.items():
                if field not in header:
                    raise StoreError(f"{LEADS_FILE} has no {field} column")
                row[field] = str(value)
            self._write(header, rows)
            return dict(row, key=key)

    def to_tracker(self, key, force=False):
        """Log the lead as an application on the board and mark it filled in the CSV."""
        lead = next((r for r in self.list()["leads"] if r["key"] == key), None)
        if lead is None:
            raise StoreError("no such lead", HTTPStatus.NOT_FOUND)
        link = lead.get("job_link", "")
        m = re.search(r"/jobs/view/(\d+)", link)
        res = self.store.add({
            "company": lead["company"], "title": lead["title"], "location": lead.get("location", ""),
            "url": link, "source": "linkedin" if "linkedin.com" in link else "",
            "job_id": m.group(1) if m else "", "notes": f"From night-hunt lead {lead['date']}",
            "force": force})
        if res["result"] == "added" and "application_filled" in lead:
            self.update(key, {"application_filled": "yes"})
        return res


# ---- HTTP --------------------------------------------------------------

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def make_handler(store, port, leads=None):
    leads = leads or Leads(store.data_dir, store)
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
            if getattr(self, "stop_after_reply", False):
                # Only after the reply is written; stopping earlier could let the
                # process exit before the caller hears back.
                self.wfile.flush()
                threading.Thread(target=self.server.shutdown, daemon=True).start()

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
            lm = re.fullmatch(r"/api/leads/([0-9a-f]{12})(/track)?", path)
            if method == "GET":
                if path == "/api/health":
                    return 200, {"ok": True, "app": "jobtracker"}
                if path == "/api/search":
                    q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
                    return 200, {"ids": store.search(q)}
                if path == "/api/apps":
                    return 200, {"apps": store.list(), "statuses": STATUSES}
                if path == "/api/leads":
                    return 200, leads.list()
                if path == "/leads":
                    return self.static("/leads.html")
                if m and m.group(2):
                    return 200, {"text": store.read_jd(m.group(1))}
                return self.static(path)
            if method == "POST" and path == "/api/capture":
                res = store.add(self._json_body())
                return (201 if res["result"] == "added" else 200), res
            if method == "POST" and path == "/api/shutdown":
                self.stop_after_reply = True  # see _dispatch: reply first, then stop
                return 200, {"ok": True, "stopping": True}
            if method == "POST" and path == "/api/restore":
                return 201, {"app": store.restore(self._json_body())}
            if method == "PATCH" and lm and not lm.group(2):
                return 200, {"lead": leads.update(lm.group(1), self._json_body())}
            if method == "POST" and lm and lm.group(2):
                res = leads.to_tracker(lm.group(1), force=bool(self._json_body().get("force")))
                return (201 if res["result"] == "added" else 200), res
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
    return os.environ.get("JOBTRACKER_DATA") or os.path.join(SHARE_DIR, "jobtracker-data")


def make_server(data_dir, port=DEFAULT_PORT, today_fn=today, leads_dir=SHARE_DIR):
    store = Store(data_dir, today_fn=today_fn)
    leads = Leads(data_dir, store, leads_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, port, leads))
    httpd.leads = leads
    return httpd, store


def main(argv=None):
    p = argparse.ArgumentParser(description="Local internship application tracker")
    p.add_argument("--data", default=default_data_dir(), help="data folder (CSV + jd/)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--leads", default=SHARE_DIR,
                   help="folder holding the agent's night-hunt-log.csv (default: J:\\)")
    p.add_argument("--open", action="store_true", help="open the board in the browser")
    args = p.parse_args(argv)
    parent = os.path.dirname(os.path.abspath(args.data).rstrip("\\/")) or args.data
    if not os.path.isdir(parent):
        print(f"Data folder {args.data} is not reachable: {SHARE_HINT}, then start again.")
        return 1
    try:
        httpd, store = make_server(args.data, args.port, leads_dir=args.leads)
        store.sweep()
    except OSError as e:
        print(f"Could not listen on 127.0.0.1:{args.port} ({e}). Already running?")
        if args.open:
            webbrowser.open(f"http://localhost:{args.port}/")
        return 1
    url = f"http://localhost:{args.port}/"
    print(f"jobtracker running at {url}\ndata: {store.data_dir}\n"
          f"leads: drop the agent CSV at {httpd.leads.path}\n"
          f"Ctrl+C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
    print("jobtracker stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
