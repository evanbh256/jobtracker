"""Server API tests. Run: python -m unittest discover -s tests -v"""
import csv
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import jobtracker  # noqa: E402


class Clock:
    def __init__(self, d):
        self.d = d

    def __call__(self):
        return self.d


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.clock = Clock(dt.date(2026, 10, 5))
        self.httpd, self.store = jobtracker.make_server(self.data, port=0, today_fn=self.clock)
        self.port = self.httpd.server_address[1]
        # Handler checks Host against the configured port; rebuild with the real one.
        self.httpd.RequestHandlerClass = jobtracker.make_handler(self.store, self.port)
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp)

    def req(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                   headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def capture(self, **kw):
        body = {"company": "Acme", "title": "Software Intern", "location": "Austin, TX",
                "url": "https://jobs.lever.co/acme/1", "source": "lever", "job_id": "1",
                "jd_text": "Do things.\nMore things."}
        body.update(kw)
        return self.req("POST", "/api/capture", body)

    def csv_rows(self):
        with open(os.path.join(self.data, "applications.csv"), newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    # ---- v1 ------------------------------------------------------------
    def test_fresh_tracker_is_empty(self):
        self.assertEqual(self.req("GET", "/api/apps")[1]["apps"], [])
        self.assertTrue(os.path.exists(os.path.join(self.data, "applications.csv")))

    def test_capture_writes_csv_and_jd(self):
        code, body = self.capture()
        self.assertEqual(code, 201)
        app = body["app"]
        self.assertEqual(app["status"], "Applied")
        self.assertEqual(app["date_applied"], "2026-10-05")
        self.assertTrue(app["jd_file"].startswith("jd/2026-10-05_acme_software-intern_"))
        with open(os.path.join(self.data, app["jd_file"]), encoding="utf-8") as f:
            self.assertEqual(f.read(), "Do things.\nMore things.")
        rows = self.csv_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["jd_file"], app["jd_file"])
        self.assertEqual(self.req("GET", f"/api/apps/{app['id']}/jd")[1]["text"], "Do things.\nMore things.")

    def test_csv_round_trip_with_commas_quotes_newlines(self):
        nasty = 'Intern, "Security"\nTeam, Ops'
        code, body = self.capture(company='Smith, Jones & "Co"', title=nasty, location="Remote,\nUS")
        self.assertEqual(code, 201)
        rows = self.csv_rows()
        self.assertEqual(rows[0]["title"], nasty)
        self.assertEqual(rows[0]["company"], 'Smith, Jones & "Co"')
        self.assertEqual(rows[0]["location"], "Remote,\nUS")
        # Survives a reload through a fresh Store (what a restart does).
        again = jobtracker.Store(self.data).list()
        self.assertEqual(again[0]["title"], nasty)
        # And an edit rewrites the file without corrupting neighbours.
        self.capture(company="Other", title="Plain", url="https://o.example/9", job_id="9")
        self.req("PATCH", f"/api/apps/{body['app']['id']}", {"notes": 'said "hi",\nok'})
        rows = self.csv_rows()
        self.assertEqual([r["company"] for r in rows], ['Smith, Jones & "Co"', "Other"])
        self.assertEqual(rows[0]["notes"], 'said "hi",\nok')

    def test_queued_capture_keeps_its_capture_date(self):
        self.assertEqual(self.capture(date_applied="2026-09-30")[1]["app"]["date_applied"], "2026-09-30")
        self.assertEqual(self.capture(date_applied="../../x", company="B", title="C", url="", job_id="")[1]["app"]["date_applied"], "2026-10-05")

    def test_unicode(self):
        self.capture(company="Société Générale", title="Stagiaire — Données 数据")
        self.assertEqual(self.csv_rows()[0]["company"], "Société Générale")

    def test_status_change_and_log(self):
        app = self.capture()[1]["app"]
        code, body = self.req("PATCH", f"/api/apps/{app['id']}", {"status": "Interview"})
        self.assertEqual(code, 200)
        self.assertEqual(body["app"]["status"], "Interview")
        self.assertEqual(body["app"]["status_changed_by"], "user")
        with open(os.path.join(self.data, "changes.csv"), newline="", encoding="utf-8-sig") as f:
            log = list(csv.DictReader(f))
        self.assertEqual(log[-1]["field"], "status")
        self.assertEqual((log[-1]["old"], log[-1]["new"], log[-1]["actor"]), ("Applied", "Interview", "user"))

    def test_bad_status_and_field_rejected(self):
        app = self.capture()[1]["app"]
        self.assertEqual(self.req("PATCH", f"/api/apps/{app['id']}", {"status": "Hired"})[0], 400)
        self.assertEqual(self.req("PATCH", f"/api/apps/{app['id']}", {"id": "x"})[0], 400)
        self.assertEqual(self.req("PATCH", "/api/apps/0123456789", {"status": "Interview"})[0], 404)

    def test_inline_edit_any_field(self):
        app = self.capture()[1]["app"]
        for field, value in [("company", "Acme Corp"), ("title", "SWE Intern"), ("location", "NYC"),
                             ("date_applied", "2026-09-01"), ("job_id", "77")]:
            self.assertEqual(self.req("PATCH", f"/api/apps/{app['id']}", {field: value})[1]["app"][field], value)

    def test_delete_and_undo(self):
        app = self.capture()[1]["app"]
        jd = os.path.join(self.data, app["jd_file"])
        code, body = self.req("DELETE", f"/api/apps/{app['id']}")
        self.assertEqual(code, 200)
        self.assertEqual(self.csv_rows(), [])
        self.assertFalse(os.path.exists(jd))
        code, _ = self.req("POST", "/api/restore", body["app"])
        self.assertEqual(code, 201)
        self.assertEqual(self.csv_rows()[0]["id"], app["id"])
        self.assertTrue(os.path.exists(jd))
        self.assertEqual(self.req("POST", "/api/restore", body["app"])[0], 409)

    def test_capture_requires_company_or_title(self):
        self.assertEqual(self.capture(company="", title="")[0], 400)

    def test_foreign_origin_blocked(self):
        code, _ = self.req("POST", "/api/capture", {"company": "x"}, {"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        code, _ = self.req("GET", "/api/apps", headers={"Origin": "chrome-extension://abcdef"})
        self.assertEqual(code, 200)

    def test_refuses_foreign_csv(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        with open(os.path.join(other, "applications.csv"), "w") as f:
            f.write("Company,Role\nX,Y\n")
        with self.assertRaises(SystemExit):
            jobtracker.Store(other)
        with open(os.path.join(other, "applications.csv")) as f:
            self.assertEqual(f.read(), "Company,Role\nX,Y\n")

    def test_static_board(self):
        r = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/")
        self.assertIn(b"Job Tracker", r.read())
        self.assertEqual(self.req("GET", "/../jobtracker.py")[0], 404)


    # ---- v2: repost detection -------------------------------------------
    def test_repost_by_job_id(self):
        first = self.capture()[1]["app"]
        code, body = self.capture(url="https://jobs.lever.co/acme/1?other", title="Different")
        self.assertEqual(code, 200)
        self.assertEqual(body["result"], "possible_repost")
        self.assertEqual(body["matches"][0]["id"], first["id"])
        self.assertEqual(body["matches"][0]["reason"], "same job ID")
        self.assertEqual(len(self.csv_rows()), 1, "a flagged capture must not be written")

    def test_repost_by_url(self):
        self.capture(job_id="")
        body = self.capture(job_id="", company="Totally Else", title="Other",
                            url="HTTPS://www.jobs.lever.co/acme/1/")[1]
        self.assertEqual(body["matches"][0]["reason"], "same URL")

    def test_repost_by_fuzzy_company_title(self):
        first = self.capture(company="Acme, Inc.", title="Software Engineer Intern - Summer 2026",
                             url="https://a.example/1", job_id="")[1]["app"]
        body = self.capture(company="ACME", title="Software Engineer Internship (Summer 2027)",
                            url="https://b.example/2", job_id="")[1]
        self.assertEqual(body["result"], "possible_repost")
        self.assertEqual(body["matches"][0]["id"], first["id"])
        self.assertEqual(body["matches"][0]["reason"], "same company and title")

    def test_not_a_repost(self):
        self.capture(company="Acme", title="Software Engineer Intern", url="https://a.example/1", job_id="")
        for kw in [dict(company="Acme", title="Hardware Engineer Intern"),
                   dict(company="Acme", title="Data Science Intern"),
                   dict(company="Acmeville Bank", title="Software Engineer Intern")]:
            body = self.capture(url="https://z.example/" + kw["title"], job_id="", **kw)[1]
            self.assertEqual(body["result"], "added", kw)

    def test_same_job_id_different_company_is_not_a_repost(self):
        self.capture(company="Acme", job_id="12345", source="icims", url="https://a.example/1")
        body = self.capture(company="Globex", title="Other", job_id="12345", source="workday",
                            url="https://b.example/2")[1]
        self.assertEqual(body["result"], "added")

    def test_add_anyway_sets_repost_of(self):
        first = self.capture()[1]["app"]
        code, body = self.capture(force=True)
        self.assertEqual(code, 201)
        self.assertEqual(body["app"]["repost_of"], first["id"])
        self.assertEqual(len(self.csv_rows()), 2)

    def test_normalizers(self):
        self.assertEqual(jobtracker.norm_company("The Acme Co."), "acme")
        self.assertEqual(jobtracker.norm_company("AT&T Inc"), "at and t")
        self.assertEqual(jobtracker.norm_title("SWE Intern (Summer 2027) - Remote"), "swe intern")
        self.assertEqual(jobtracker.norm_title("C++ Developer Internship, Fall 2026"), "c++ developer intern")

    # ---- v2: auto-Ghosted ------------------------------------------------
    def test_auto_ghost_after_90_days(self):
        old = self.capture(date_applied="2026-07-07")[1]["app"]       # exactly 90 days before 2026-10-05
        young = self.capture(company="B", title="B", url="", job_id="", date_applied="2026-07-08")[1]["app"]
        interviewing = self.capture(company="C", title="C", url="", job_id="", date_applied="2026-01-01")[1]["app"]
        self.req("PATCH", f"/api/apps/{interviewing['id']}", {"status": "Interview"})
        apps = {a["id"]: a for a in self.req("GET", "/api/apps")[1]["apps"]}
        self.assertEqual(apps[old["id"]]["status"], "Ghosted")
        self.assertEqual(apps[old["id"]]["status_changed_by"], "auto")
        self.assertEqual(apps[young["id"]]["status"], "Applied")
        self.assertEqual(apps[interviewing["id"]]["status"], "Interview")
        with open(os.path.join(self.data, "changes.csv"), newline="", encoding="utf-8-sig") as f:
            auto = [r for r in csv.DictReader(f) if r["actor"] == "auto"]
        self.assertEqual([(r["app_id"], r["old"], r["new"]) for r in auto], [(old["id"], "Applied", "Ghosted")])
        # Tomorrow the younger one goes too; the sweep is idempotent for the first.
        self.clock.d = dt.date(2026, 10, 6)
        self.store.sweep()
        self.assertEqual(self.store.get(young["id"])["status"], "Ghosted")
        with open(os.path.join(self.data, "changes.csv"), newline="", encoding="utf-8-sig") as f:
            self.assertEqual(sum(r["actor"] == "auto" for r in csv.DictReader(f)), 2)

    def test_user_move_back_to_applied_is_respected(self):
        app = self.capture(date_applied="2026-01-01")[1]["app"]
        self.req("GET", "/api/apps")
        self.assertEqual(self.store.get(app["id"])["status"], "Ghosted")
        self.req("PATCH", f"/api/apps/{app['id']}", {"status": "Applied"})
        self.req("GET", "/api/apps")
        self.assertEqual(self.store.get(app["id"])["status"], "Applied")

    # ---- v2: search ------------------------------------------------------
    def test_search_company_title_and_jd(self):
        a = self.capture(company="Acme", title="Security Intern", url="", job_id="",
                         jd_text="Work on Kubernetes hardening.")[1]["app"]
        b = self.capture(company="Globex", title="Data Intern", url="", job_id="",
                         jd_text="SQL, dashboards, kubernetes optional")[1]["app"]
        q = lambda s: set(self.req("GET", "/api/search?q=" + urllib.request.quote(s))[1]["ids"])
        self.assertEqual(q("acme"), {a["id"]})
        self.assertEqual(q("intern"), {a["id"], b["id"]})
        self.assertEqual(q("KUBERNETES"), {a["id"], b["id"]})
        self.assertEqual(q("kubernetes dashboards"), {b["id"]})
        self.assertEqual(q("nothing-matches"), set())


if __name__ == "__main__":
    unittest.main()
