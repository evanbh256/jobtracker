"""Leads page API tests. Run: python -m unittest discover -s tests -v"""
import csv
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_server import ServerTest  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "night-hunt-log.csv")


class LeadsTest(ServerTest):
    def drop(self, src=FIXTURE):
        dst = os.path.join(self.data, "leads", "night-hunt-log.csv")
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
        self.assertTrue(body["path"].endswith(os.path.join("leads", "night-hunt-log.csv")))

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

    def test_edit_writes_csv_and_survives_replacement(self):
        path = self.drop()
        lead = self.find("Rivian")
        s, _ = self.req("PATCH", f"/api/leads/{lead['key']}", {"reply_status": "replied", "reply_date": "2026-11-03"})
        self.assertEqual(s, 200)
        with open(path, newline="", encoding="utf-8-sig") as f:
            row = next(r for r in csv.DictReader(f) if r["company"] == "Rivian")
        self.assertEqual(row["reply_status"], "replied")
        # Agent's next run overwrites the file with its own copy (plus a new row).
        self.drop()
        with open(path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["2026-11-03", "NewCo", "Security Intern"] + [""] * 13)
        self.assertEqual(self.find("Rivian")["reply_status"], "replied")
        self.assertEqual(len(self.leads()["leads"]), 8)
        with open(path, newline="", encoding="utf-8-sig") as f:
            row = next(r for r in csv.DictReader(f) if r["company"] == "Rivian")
        self.assertEqual(row["reply_status"], "replied")  # written back into the new file

    def test_agent_change_beats_old_edit(self):
        path = self.drop()
        lead = self.find("Rivian")
        self.req("PATCH", f"/api/leads/{lead['key']}", {"notes": "mine"})
        with open(FIXTURE, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            if r["company"] == "Rivian":
                r["notes"] = "agent found a contact"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
        self.assertEqual(self.find("Rivian")["notes"], "agent found a contact")

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
