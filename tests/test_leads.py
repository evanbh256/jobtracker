"""Leads page API tests. Run: python -m unittest discover -s tests -v"""
import csv
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_server import ServerTest, jobtracker  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "night-hunt-log.csv")


class LeadsTest(ServerTest):
    def setUp(self):
        super().setUp()
        # Never touch the real <repo>/leads folder from tests.
        self.leads_dir = os.path.join(self.tmp, "share")
        os.makedirs(self.leads_dir)
        self.httpd.RequestHandlerClass = jobtracker.make_handler(
            self.store, self.port, jobtracker.Leads(self.data, self.store, self.leads_dir))

    def drop(self, src=FIXTURE):
        dst = os.path.join(self.leads_dir, "night-hunt-log.csv")
        shutil.copy(src, dst)
        return dst

    def leads(self):
        s, body = self.req("GET", "/api/leads")
        self.assertEqual(s, 200)
        return body

    def find(self, company, title_part=""):
        return next(l for l in self.leads()["leads"] if l["company"] == company and title_part in l["title"])

    def test_missing_file(self):
        body = self.leads()
        self.assertFalse(body["found"])
        self.assertTrue(body["reachable"])
        self.assertEqual(body["path"], os.path.join(self.leads_dir, "night-hunt-log.csv"))

    def test_reads_all_rows_and_flags(self):
        self.drop()
        body = self.leads()
        self.assertEqual(len(body["leads"]), 7)
        kinds = lambda l: {f["kind"] for f in l["flags"]}
        self.assertIn("bad_link", kinds(self.find("UPS")))
        self.assertIn("bad_link", kinds(self.find("AbbVie / BioSpace")))  # bare /jobs/view/
        self.assertNotIn("bad_link", kinds(self.find("Avangrid")))
        self.assertIn("duplicate", kinds(self.find("Tevora", "SOC")))
        self.assertIn("draft_ready", kinds(self.find("Example Corp")))

    def test_missing_share(self):
        shutil.rmtree(self.leads_dir)
        body = self.leads()
        self.assertFalse(body["reachable"])
        s, _ = self.req("PATCH", "/api/leads/0123456789ab", {"notes": "x"})
        self.assertEqual(s, 503)

    def test_edit_rewrites_in_place_with_one_backup(self):
        path = self.drop()
        before = os.stat(path)
        with open(path, "rb") as f:
            raw = f.read()
        with open(path, "wb") as f:  # agent-style file: BOM + CRLF, mixed date formats
            f.write(b"\xef\xbb\xbf" + raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                    .replace(b"2026-11-01,UPS", b"11/1/2026,UPS"))
        for status in ("replied", "declined"):
            s, _ = self.req("PATCH", f"/api/leads/{self.find('Rivian')['key']}", {"reply_status": status})
            self.assertEqual(s, 200)
        after = os.stat(path)
        self.assertEqual(before.st_ino, after.st_ino)  # same file, not replaced
        with open(path, "rb") as f:
            new = f.read()
        self.assertTrue(new.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", new)
        self.assertIn(b"11/1/2026,UPS", new)  # other values untouched
        self.assertEqual(self.find("Rivian")["reply_status"], "declined")
        backups = os.listdir(os.path.join(self.leads_dir, "backups"))
        self.assertEqual(len(backups), 1)  # once per app run
        self.assertNotIn(b"declined", open(os.path.join(self.leads_dir, "backups", backups[0]), "rb").read())
        self.assertFalse([n for n in os.listdir(self.leads_dir) if n.endswith(".tmp")])

    def test_locked_file_retries_then_errors(self):
        self.drop()
        leads = jobtracker.Leads(self.data, self.store, self.leads_dir)
        leads.WRITE_TRIES, leads.WRITE_WAIT = 3, 0
        real_open, calls = open, []

        def locked(path, mode="r", *a, **k):
            if "+" in mode:
                calls.append(1)
                raise PermissionError("locked")
            return real_open(path, mode, *a, **k)
        import builtins
        builtins.open = locked
        try:
            key = jobtracker.lead_key({"date": "2026-10-31", "company": "Rivian",
                                       "title": "Cybersecurity - Summer 2027 Internships",
                                       "location": "Palo Alto, CA (on-site)"})
            with self.assertRaises(jobtracker.StoreError):
                leads.update(key, {"notes": "x"})
        finally:
            builtins.open = real_open
        self.assertEqual(len(calls), 3)

    def test_delete_and_undo(self):
        path = self.drop()
        before = os.stat(path).st_ino
        lead = self.find("Tevora", "SOC")
        s, res = self.req("DELETE", f"/api/leads/{lead['key']}")
        self.assertEqual(s, 200)
        self.assertEqual(os.stat(path).st_ino, before)  # in place
        self.assertEqual(len(self.leads()["leads"]), 6)
        with open(path, encoding="utf-8-sig") as f:
            self.assertNotIn("Irvine, CA", f.read())
        s, _ = self.req("POST", "/api/leads/restore", res)
        self.assertEqual(s, 201)
        names = [l["company"] + l["location"] for l in self.leads()["leads"]]
        self.assertEqual(len(names), 7)
        self.assertEqual(names[2], "TevoraIrvine, CA")  # back in its old spot
        s, _ = self.req("POST", "/api/leads/restore", res)  # double undo is a no-op
        self.assertEqual(len(self.leads()["leads"]), 7)
        s, _ = self.req("DELETE", "/api/leads/0123456789ab")
        self.assertEqual(s, 404)

    def test_rejects_non_editable(self):
        self.drop()
        lead = self.find("Rivian")
        s, _ = self.req("PATCH", f"/api/leads/{lead['key']}", {"company": "x"})
        self.assertEqual(s, 400)

    def test_add_to_board(self):
        path = self.drop()
        lead = self.find("Rivian")
        s, res = self.req("POST", f"/api/leads/{lead['key']}/track", {})
        self.assertEqual(s, 201)
        self.assertEqual(res["app"]["job_id"], "1000000001")
        self.assertEqual(res["app"]["status"], "Applied")
        lead = self.find("Rivian")
        self.assertEqual(lead["application_filled"], "yes")
        self.assertIn("tracked", {f["kind"] for f in lead["flags"]})
        s, res = self.req("POST", f"/api/leads/{lead['key']}/track", {})
        self.assertEqual(res["result"], "possible_repost")

    def test_leads_page_served(self):
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/leads") as r:
            self.assertIn(b"leads.js", r.read())


del ServerTest  # don't re-run the base tests from this module

if __name__ == "__main__":
    unittest.main()
