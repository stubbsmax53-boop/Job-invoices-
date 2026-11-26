"""Job board, logistics, inbox, and the token API."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="job-board-tests-")
os.environ["SQLITE_PATH"] = str(Path(_TMP) / "invoices.db")
os.environ.pop("DATABASE_URL", None)
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["INTAKE_WEBHOOK_TOKEN"] = "test-token"
os.environ.pop("TWILIO_AUTH_TOKEN", None)
os.environ.pop("OPENAI_API_KEY", None)

import app as app_module  # noqa: E402
import jobs  # noqa: E402
from app import app  # noqa: E402

app.config["TESTING"] = True
ADMIN_PASSWORD = "test-admin-password"
TOKEN = {"Authorization": "Bearer test-token"}
BANNED = {
    "hour",
    "hours",
    "hourly",
    "hourly_rate",
    "time",
    "time_in",
    "time_out",
    "start_time",
    "end_time",
    "duration",
    "minutes",
}


class JobBoardTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["SQLITE_PATH"])
        if path.exists():
            path.unlink()
        app_module.init_db()
        os.environ["INTAKE_WEBHOOK_TOKEN"] = "test-token"
        app.config["ADMIN_PASSWORD"] = ADMIN_PASSWORD
        self.client = app.test_client()

    def login(self):
        response = self.client.post(
            "/login",
            data={"password": ADMIN_PASSWORD},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return response

    def query(self, sql, params=()):
        db = app_module.connect_db()
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def job_payload(self, **overrides):
        data = {
            "customer": "MJL",
            "job_name": "Lolo hang",
            "street_address": "120 Ridge Rd",
            "town": "Lolo",
            "status": "scheduled",
            "crew": "Jordan, Sam",
            "start_date": jobs.week_bounds()[0],
            "target_finish": jobs.week_bounds()[1],
            "amount": "4200",
            "notes": "Hang and tape the main floor.",
        }
        data.update(overrides)
        return data

    def test_migration_keeps_existing_invoices_and_adds_links(self):
        path = Path(os.environ["SQLITE_PATH"])
        path.unlink()
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL UNIQUE,
                address TEXT NOT NULL,
                description TEXT NOT NULL,
                tech_name TEXT NOT NULL,
                amount TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO invoices (
                invoice_number, address, description, tech_name, amount, status, created_at, updated_at
            ) VALUES ('INV-0007', '1 Old St', 'Existing job', 'Max', '100.00', 'sent', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """
        )
        conn.commit()
        conn.close()
        app_module.init_db()
        rows = self.query("SELECT * FROM invoices")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["invoice_number"], "INV-0007")
        self.assertEqual(rows[0]["description"], "Existing job")
        self.assertEqual(rows[0]["tech_name"], "Max")
        self.assertIsNone(rows[0]["job_id"])
        self.assertTrue(self.query("SELECT name FROM sqlite_master WHERE name = 'jobs'"))
        self.assertTrue(self.query("SELECT name FROM sqlite_master WHERE name = 'activity'"))
        columns = {row["name"] for row in self.query("PRAGMA table_info(jobs)")}
        self.assertTrue(BANNED.isdisjoint(columns))
        order_columns = {row["name"] for row in self.query("PRAGMA table_info(material_orders)")}
        self.assertTrue(BANNED.isdisjoint(order_columns))

    def test_board_requires_login_and_filters(self):
        blocked = self.client.get("/admin/jobs")
        self.assertEqual(blocked.status_code, 302)
        self.assertIn("/login", blocked.headers["Location"])
        self.login()
        created = self.client.post("/api/jobs", json=self.job_payload(), headers=TOKEN)
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        other = self.client.post(
            "/api/jobs",
            json=self.job_payload(
                customer="Bushco Construction",
                job_name="Office patch",
                street_address="8 Front St",
                town="Missoula",
                status="hang",
                start_date="2020-01-01",
                target_finish="2020-01-02",
                crew="",
            ),
            headers=TOKEN,
        )
        self.assertEqual(other.status_code, 201)
        page = self.client.get("/admin/jobs")
        html = page.get_data(as_text=True)
        self.assertIn("Lolo hang", html)
        self.assertIn("Office patch", html)
        self.assertIn('name="viewport"', html)
        self.assertIn('class="message-list"', html)
        mj = self.client.get("/admin/jobs?customer=MJL")
        self.assertIn("Lolo hang", mj.get_data(as_text=True))
        self.assertNotIn("Office patch", mj.get_data(as_text=True))
        hang = self.client.get("/admin/jobs?status=hang")
        self.assertIn("Office patch", hang.get_data(as_text=True))
        self.assertNotIn("Lolo hang", hang.get_data(as_text=True))
        week = self.client.get("/admin/jobs?week=1")
        week_html = week.get_data(as_text=True)
        self.assertIn("Lolo hang", week_html)
        self.assertNotIn("Office patch", week_html)
        self.assertIn("This week", week_html)

    def test_job_form_reuses_a_matching_street_and_keeps_crew_manual(self):
        self.login()
        first = self.client.post(
            "/admin/jobs/new",
            data=self.job_payload(crew=""),
            follow_redirects=True,
        )
        self.assertEqual(first.status_code, 200)
        self.assertIn("Lolo hang", first.get_data(as_text=True))
        self.assertEqual(self.query("SELECT crew FROM jobs")[0]["crew"], "")
        again = self.client.post(
            "/admin/jobs/new",
            data=self.job_payload(job_name="Second pass", crew="Alex"),
            follow_redirects=True,
        )
        body = again.get_data(as_text=True)
        self.assertIn("already on the board", body)
        self.assertEqual(len(self.query("SELECT id FROM jobs")), 1)
        forced = self.client.post(
            "/admin/jobs/new",
            data={**self.job_payload(job_name="Unit B", street_address="120 Ridge Rd Apt 4"), "force_new": "1"},
            follow_redirects=True,
        )
        self.assertEqual(forced.status_code, 200)
        self.assertEqual(len(self.query("SELECT id FROM jobs")), 2)

    def test_material_orders_logistics_and_needs_sort_by_date(self):
        self.login()
        job_id = self.client.post("/api/jobs", json=self.job_payload(), headers=TOKEN).get_json()["job"]["id"]
        later = "2026-12-20"
        sooner = "2026-12-02"
        self.client.post(
            f"/admin/jobs/{job_id}/orders",
            data={
                "item": "corner bead",
                "qty": "12",
                "supplier": "Yard",
                "order_date": "2026-11-20",
                "delivery_date": later,
                "status": "ordered",
            },
        )
        self.client.post(
            f"/admin/jobs/{job_id}/orders",
            data={
                "item": "1/2in board",
                "qty": "40 sheets",
                "supplier": "ABC Supply",
                "delivery_date": sooner,
                "status": "ordered",
            },
        )
        self.client.post(
            f"/admin/jobs/{job_id}/needs",
            data={"kind": "dumpster", "description": "20-yard dumpster", "needed_date": "2026-12-01"},
        )
        page = self.client.get("/admin/logistics")
        html = page.get_data(as_text=True)
        self.assertLess(html.index("20-yard dumpster"), html.index("1/2in board"))
        self.assertLess(html.index("1/2in board"), html.index("corner bead"))
        self.assertIn("ABC Supply", html)
        stocked = self.query("SELECT id FROM material_orders WHERE item = 'corner bead'")[0]["id"]
        saved = self.client.post(
            f"/admin/orders/{stocked}",
            data={
                "item": "corner bead",
                "qty": "12",
                "supplier": "Yard",
                "order_date": "2026-11-20",
                "delivery_date": later,
                "status": "stocked",
            },
            follow_redirects=True,
        )
        self.assertIn("Material order saved", saved.get_data(as_text=True))
        after = self.client.get("/admin/logistics").get_data(as_text=True)
        self.assertNotIn("corner bead", after)
        self.assertIn("1/2in board", after)

    def test_dashboard_counts_week_deliveries_unpaid_and_inbox(self):
        self.login()
        self.client.post("/api/jobs", json=self.job_payload(status="hang"), headers=TOKEN)
        job = self.client.post(
            "/api/jobs",
            json=self.job_payload(
                customer="IJD",
                job_name="Shop",
                street_address="9 Cedar Ln",
                town="Missoula",
                status="lead",
                start_date="2020-01-01",
                target_finish="2020-01-02",
            ),
            headers=TOKEN,
        ).get_json()["job"]
        soon = (jobs.today_local() + timedelta(days=2)).isoformat()
        self.client.post(
            "/api/material-orders",
            json={"job_id": job["id"], "item": "mud", "qty": "6 boxes", "delivery_date": soon},
            headers=TOKEN,
        )
        self.client.post(
            "/",
            data={"address": "500 Public Rd", "description": "Hung board", "tech_name": "Alex"},
        )
        page = self.client.get("/admin/home")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("This week", html)
        self.assertIn("Lolo hang", html)
        self.assertIn("Deliveries in the next 7 days", html)
        self.assertIn("mud", html)
        self.assertIn("Unpaid invoices", html)
        self.assertIn("INV-0001", html)
        self.assertIn('class="badge"', html)
        self.assertIn("waiting for review", html)
        self.assertGreater(html.index("Hang"), 0)

    def test_invoice_links_to_a_job_and_pdf_still_builds(self):
        self.login()
        job_id = self.client.post("/api/jobs", json=self.job_payload(), headers=TOKEN).get_json()["job"]["id"]
        opened = self.client.post(f"/admin/jobs/{job_id}/invoice", follow_redirects=True)
        self.assertIn("INV-0001", opened.get_data(as_text=True))
        invoice = self.query("SELECT * FROM invoices")[0]
        self.assertEqual(invoice["job_id"], job_id)
        self.assertEqual(invoice["amount"], "4200.00")
        self.assertIn("120 Ridge Rd", invoice["address"])
        self.assertIn("Lolo", invoice["address"])
        pdf = self.client.get(f"/invoice/{invoice['id']}/pdf")
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))
        cleared = self.client.post(f"/invoice/{invoice['id']}/job", data={"job_id": ""}, follow_redirects=True)
        self.assertIn("Job link saved", cleared.get_data(as_text=True))
        self.assertIsNone(self.query("SELECT job_id FROM invoices")[0]["job_id"])
        linked = self.client.post(
            f"/invoice/{invoice['id']}/job",
            data={"job_id": str(job_id)},
            follow_redirects=True,
        )
        self.assertIn("MJL", linked.get_data(as_text=True))
        self.assertEqual(self.query("SELECT job_id FROM invoices")[0]["job_id"], job_id)

    def test_api_auth_crud_and_rejects_hours(self):
        missing = self.client.get("/api/jobs")
        self.assertEqual(missing.status_code, 403)
        os.environ.pop("INTAKE_WEBHOOK_TOKEN", None)
        try:
            closed = self.client.post("/api/jobs", json=self.job_payload())
        finally:
            os.environ["INTAKE_WEBHOOK_TOKEN"] = "test-token"
        self.assertEqual(closed.status_code, 503)
        header = self.client.post(
            "/api/jobs",
            json=self.job_payload(customer="Five Valleys", street_address="4 Main St"),
            headers={"X-Intake-Token": "test-token"},
        )
        self.assertEqual(header.status_code, 201)
        job_id = header.get_json()["job"]["id"]
        self.assertEqual(header.get_json()["job"]["crew"], "Jordan, Sam")
        self.assertTrue(BANNED.isdisjoint(header.get_json()["job"]))
        hours = self.client.post(
            "/api/jobs",
            json={**self.job_payload(), "hours": 8},
            headers=TOKEN,
        )
        self.assertEqual(hours.status_code, 400)
        self.assertIn("per job", hours.get_json()["error"])
        self.assertEqual(len(self.query("SELECT id FROM jobs")), 1)
        patched = self.client.patch(
            f"/api/jobs/{job_id}",
            json={"status": "tape_finish", "amount": "4500"},
            headers=TOKEN,
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.get_json()["job"]["status"], "tape_finish")
        self.assertEqual(patched.get_json()["job"]["amount"], "4500.00")
        listed = self.client.get("/api/jobs?status=tape_finish", headers=TOKEN)
        self.assertEqual(listed.get_json()["count"], 1)
        bad_date = self.client.patch(
            f"/api/jobs/{job_id}",
            json={"start_date": "2026-11-26T15:00:00"},
            headers=TOKEN,
        )
        self.assertEqual(bad_date.status_code, 400)
        order = self.client.post(
            "/api/material-orders",
            json={
                "job_id": job_id,
                "item": "tape",
                "qty": "10",
                "supplier": "ABC Supply",
                "delivery_date": "2026-12-01",
            },
            headers=TOKEN,
        )
        self.assertEqual(order.status_code, 201, order.get_data(as_text=True))
        order_id = order.get_json()["order"]["id"]
        self.assertTrue(BANNED.isdisjoint(order.get_json()["order"]))
        updated = self.client.patch(
            f"/api/material-orders/{order_id}",
            json={"status": "delivered"},
            headers=TOKEN,
        )
        self.assertEqual(updated.get_json()["order"]["status"], "delivered")
        self.assertEqual(updated.get_json()["order"]["delivery_date"], "2026-12-01")
        open_orders = self.client.get("/api/material-orders?open=1", headers=TOKEN)
        self.assertEqual(len(open_orders.get_json()["orders"]), 1)
        self.login()
        session_call = self.client.get("/api/jobs")
        self.assertEqual(session_call.status_code, 200)

    def test_inbound_sms_logs_activity_and_reuses_a_matching_job(self):
        self.client.post(
            "/api/jobs",
            json=self.job_payload(
                customer="Five Valleys",
                job_name="Broadway repair",
                street_address="1842 S Broadway",
                town="Denver",
                crew="Jordan",
                status="hang",
            ),
            headers=TOKEN,
        )
        before = len(self.query("SELECT id FROM jobs"))
        payload = {
            "From": "+13035550142",
            "Body": "This is Sarah Johnson. Please patch the ceiling at 1842 S Broadway, Denver CO tomorrow.",
            "MessageSid": "SM-board",
        }
        first = self.client.post("/webhooks/sms?token=test-token", data=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(self.query("SELECT id FROM jobs")), before)
        self.assertEqual(len(self.query("SELECT id FROM invoices")), 1)
        self.assertEqual(self.query("SELECT tech_name FROM invoices")[0]["tech_name"], "Inbound")
        job = self.query("SELECT * FROM jobs WHERE street_address = '1842 S Broadway'")[0]
        self.assertEqual(job["crew"], "Jordan")
        self.assertEqual(job["status"], "hang")
        invoice = self.query("SELECT * FROM invoices")[0]
        self.assertEqual(invoice["job_id"], job["id"])
        activity = self.query("SELECT * FROM activity WHERE source = 'sms'")
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["reviewed"], 0)
        self.assertEqual(activity[0]["job_id"], job["id"])
        self.assertIn("Sarah", activity[0]["summary"])
        again = self.client.post("/webhooks/sms?token=test-token", data=payload)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.query("SELECT id FROM activity WHERE source = 'sms'")), 1)
        self.assertEqual(len(self.query("SELECT id FROM invoices")), 1)
        other = self.client.post(
            "/webhooks/email",
            json={
                "from": "sam@example.com",
                "text": "Need tape at 10 Main St Apt 4, Missoula MT.",
            },
            headers={"X-Intake-Token": "test-token"},
        )
        self.assertEqual(other.status_code, 200)
        self.client.post(
            "/webhooks/email",
            json={
                "from": "sam@example.com",
                "text": "Different unit at 10 Main St Apt 2, Missoula MT. Please hang board.",
            },
            headers={"X-Intake-Token": "test-token"},
        )
        streets = [row["street_address"] for row in self.query("SELECT street_address FROM jobs ORDER BY id")]
        self.assertIn("10 Main St Apt 4", " ".join(streets))
        self.assertEqual(len([street for street in streets if "Main" in street]), 2)

    def test_public_form_is_logged_and_does_not_open_a_board_job(self):
        created = self.client.post(
            "/",
            data={"address": "500 Public Rd", "description": "Hung board", "tech_name": "Alex"},
        )
        self.assertEqual(created.status_code, 302)
        self.assertEqual(self.query("SELECT id FROM jobs"), [])
        self.assertEqual(len(self.query("SELECT id FROM invoices")), 1)
        activity = self.query("SELECT * FROM activity")
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["source"], "form")
        self.assertEqual(activity[0]["reviewed"], 0)
        self.assertEqual(activity[0]["sender"], "Alex")
        self.assertIsNone(activity[0]["job_id"])
        self.client.post("/api/jobs", json=self.job_payload(street_address="500 Public Rd", town=""), headers=TOKEN)
        self.client.post(
            "/",
            data={"address": "500 Public Rd", "description": "Second day", "tech_name": "Alex"},
        )
        self.assertEqual(len(self.query("SELECT id FROM jobs")), 1)
        linked = self.query("SELECT job_id FROM invoices ORDER BY id DESC")[0]
        self.assertEqual(linked["job_id"], self.query("SELECT id FROM jobs")[0]["id"])

    def test_activity_api_defaults_unreviewed_and_matches_address(self):
        job_id = self.client.post("/api/jobs", json=self.job_payload(), headers=TOKEN).get_json()["job"]["id"]
        forced = self.client.post(
            "/api/activity",
            json={
                "summary": "Called the GC about the Lolo hang",
                "sender": "assistant",
                "reviewed": True,
                "address": "120 Ridge Road, Lolo MT",
                "external_id": "note-1",
            },
            headers=TOKEN,
        )
        self.assertEqual(forced.status_code, 201, forced.get_data(as_text=True))
        body = forced.get_json()
        self.assertFalse(body["activity"]["reviewed"])
        self.assertEqual(body["activity"]["job_id"], job_id)
        self.assertFalse(body["activity"]["duplicate"])
        repeat = self.client.post(
            "/api/activity",
            json={"summary": "Called the GC about the Lolo hang", "external_id": "note-1"},
            headers=TOKEN,
        )
        self.assertEqual(repeat.status_code, 200)
        self.assertTrue(repeat.get_json()["activity"]["duplicate"])
        self.assertEqual(len(self.query("SELECT id FROM activity WHERE external_id = 'note-1'")), 1)
        denied = self.client.post("/api/activity", json={"summary": "Nope"})
        self.assertEqual(denied.status_code, 403)
        self.assertNotIn("mailto:", forced.headers.get("Location", ""))
        self.login()
        inbox = self.client.get("/admin/activity")
        html = inbox.get_data(as_text=True)
        self.assertIn("Called the GC", html)
        self.assertIn("Mark reviewed", html)
        self.assertIn('class="badge"', html)
        activity_id = body["activity"]["id"]
        reviewed = self.client.post(
            f"/admin/activity/{activity_id}/review",
            data={"reviewed": "1", "show": "unreviewed"},
            follow_redirects=True,
        )
        reviewed_html = reviewed.get_data(as_text=True)
        self.assertNotIn("Called the GC", reviewed_html)
        self.assertIn("Marked reviewed", reviewed_html)
        self.assertEqual(self.query("SELECT reviewed FROM activity WHERE id = ?", (activity_id,))[0]["reviewed"], 1)
        events = self.query(
            "SELECT summary FROM activity WHERE summary LIKE 'Status changed%' OR summary LIKE 'Job created%' OR summary LIKE 'Delivery scheduled%' OR summary LIKE 'Material %'"
        )
        summaries = [row["summary"] for row in events]
        self.assertTrue(any(text.startswith("Job created:") for text in summaries))

    def test_status_and_delivery_events_hit_the_inbox(self):
        created = self.client.post("/api/jobs", json=self.job_payload(status="lead"), headers=TOKEN)
        job_id = created.get_json()["job"]["id"]
        self.client.patch(f"/api/jobs/{job_id}", json={"status": "punch"}, headers=TOKEN)
        self.client.post(
            "/api/material-orders",
            json={"job_id": job_id, "item": "1/2in board", "qty": "40", "delivery_date": "2026-12-03"},
            headers=TOKEN,
        )
        summaries = [row["summary"] for row in self.query("SELECT summary FROM activity ORDER BY id")]
        self.assertTrue(any("Status changed" in text and "Punch" in text for text in summaries))
        self.assertTrue(any(text.startswith("Delivery scheduled:") and "2026-12-03" in text for text in summaries))
        self.assertTrue(all(row["reviewed"] == 0 for row in self.query("SELECT reviewed FROM activity")))
        self.login()
        page = self.client.get("/admin/activity")
        self.assertIn("Mark all reviewed", page.get_data(as_text=True))
        cleared = self.client.post("/admin/activity/mark-reviewed", follow_redirects=True)
        self.assertIn("Nothing waiting for review", cleared.get_data(as_text=True))
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM activity WHERE reviewed = 0")[0]["n"], 0)

    def test_address_matching_ignores_a_different_unit(self):
        self.assertTrue(jobs.streets_match("120 Ridge Road", "120 Ridge Rd"))
        self.assertTrue(jobs.streets_match("120 N Ridge Rd", "120 North Ridge Road"))
        self.assertFalse(jobs.streets_match("10 Main St Apt 2", "10 Main St Apt 4"))
        self.assertFalse(jobs.streets_match("10 Main St", "1010 Main St"))
        self.assertTrue(jobs.streets_match("10 Main St", "10 Main Street Apt 2"))

    def test_manual_message_is_in_the_inbox(self):
        self.login()
        self.client.post(
            "/admin/messages",
            data={"sender": "Pat", "text": "Need an estimate for a small patch."},
        )
        rows = self.query("SELECT * FROM activity")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "manual")
        self.assertEqual(rows[0]["reviewed"], 0)
        self.assertIsNone(rows[0]["job_id"])
        css = (ROOT / "static" / "style.css").read_text()
        self.assertIn(".message-list", css)
        self.assertIn("@media (max-width: 640px)", css)


if __name__ == "__main__":
    unittest.main()
