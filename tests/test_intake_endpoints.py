"""Inbound webhooks, contact memory, and the admin message inbox."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="job-invoices-tests-")
os.environ["SQLITE_PATH"] = str(Path(_TMP) / "invoices.db")
os.environ.pop("DATABASE_URL", None)
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["INTAKE_WEBHOOK_TOKEN"] = "test-token"
os.environ.pop("TWILIO_AUTH_TOKEN", None)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("INTAKE_LLM_API_KEY", None)

import app as app_module  # noqa: E402
from app import app  # noqa: E402
from parsing import compute_twilio_signature  # noqa: E402

app.config["TESTING"] = True
ADMIN_PASSWORD = "test-admin-password"


class IntakeEndpointTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["SQLITE_PATH"])
        if path.exists():
            path.unlink()
        app_module.init_db()
        os.environ["INTAKE_WEBHOOK_TOKEN"] = "test-token"
        os.environ.pop("TWILIO_AUTH_TOKEN", None)
        os.environ.pop("OPENAI_API_KEY", None)
        app.config["ADMIN_PASSWORD"] = ADMIN_PASSWORD
        self.client = app.test_client()

    def login(self):
        response = self.client.post(
            "/login",
            data={"password": ADMIN_PASSWORD},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        return response

    def query(self, sql, params=()):
        db = app_module.connect_db()
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def test_health_and_public_intake_still_work(self):
        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertIn(b"ok", health.data)
        created = self.client.post(
            "/",
            data={
                "address": "500 Public Rd",
                "description": "Hung board",
                "tech_name": "Alex",
            },
        )
        self.assertEqual(created.status_code, 302)
        invoices = self.query("SELECT * FROM invoices")
        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices[0]["tech_name"], "Alex")
        self.assertEqual(invoices[0]["address"], "500 Public Rd")
        self.assertEqual(invoices[0]["invoice_number"], "INV-0001")

    def test_init_db_keeps_existing_invoices(self):
        db = app_module.connect_db()
        try:
            invoice_id = app_module.create_invoice(
                db,
                {
                    "address": "1 Old St",
                    "description": "Existing job",
                    "tech_name": "Max",
                },
            )
        finally:
            db.close()
        app_module.init_db()
        rows = self.query("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["invoice_number"], "INV-0001")
        self.assertEqual(rows[0]["description"], "Existing job")
        # A later message continues the invoice sequence.
        response = self.client.post(
            "/webhooks/sms?token=test-token",
            data={
                "From": "+13035550111",
                "Body": "Please patch the ceiling at 10 Main St, Denver CO tomorrow.",
                "MessageSid": "SM-after-existing",
            },
        )
        self.assertEqual(response.status_code, 200)
        numbers = [row["invoice_number"] for row in self.query("SELECT * FROM invoices ORDER BY id")]
        self.assertEqual(numbers, ["INV-0001", "INV-0002"])

    def test_webhook_requires_token(self):
        missing = self.client.post(
            "/webhooks/sms",
            data={"From": "+13035550100", "Body": "Patch the wall at 10 Main St, Denver CO"},
        )
        self.assertEqual(missing.status_code, 403)
        wrong = self.client.post(
            "/webhooks/sms?token=nope",
            data={"From": "+13035550100", "Body": "Patch the wall at 10 Main St, Denver CO"},
        )
        self.assertEqual(wrong.status_code, 403)
        os.environ.pop("INTAKE_WEBHOOK_TOKEN", None)
        try:
            unconfigured = self.client.post(
                "/webhooks/sms",
                data={"From": "+13035550100", "Body": "Patch the wall at 10 Main St, Denver CO"},
            )
        finally:
            os.environ["INTAKE_WEBHOOK_TOKEN"] = "test-token"
        self.assertEqual(unconfigured.status_code, 503)
        self.assertEqual(self.query("SELECT id FROM messages"), [])
        self.assertEqual(self.query("SELECT id FROM invoices"), [])

    def test_token_header_and_bearer(self):
        header = self.client.post(
            "/webhooks/email",
            data={
                "from": "sam@example.com",
                "subject": "Wall",
                "text": "Please patch the wall at 12 Oak Ave, Denver CO.",
            },
            headers={"X-Intake-Token": "test-token"},
        )
        self.assertEqual(header.status_code, 200)
        bearer = self.client.post(
            "/api/intake/messages",
            json={
                "from": "Dee <dee@example.com>",
                "text": "Need drywall in the garage at 8 Pine Rd, Denver CO Friday.",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        self.assertEqual(bearer.status_code, 200)
        body = bearer.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "created")
        self.assertTrue(body["invoice_number"].startswith("INV-"))
        self.assertEqual(len(self.query("SELECT id FROM invoices")), 2)

    def test_sms_creates_job_and_duplicate_sid_does_not(self):
        payload = {
            "From": "+13035550142",
            "Body": (
                "Hi this is Sarah Johnson. I need drywall repair in the living room. "
                "Tuesday at 2pm. 1842 S Broadway, Denver CO 80210. Call (303) 555-0142."
            ),
            "MessageSid": "SM-once",
        }
        first = self.client.post("/webhooks/sms?token=test-token", data=payload)
        self.assertEqual(first.status_code, 200)
        self.assertIn(b"<Response>", first.data)
        self.assertEqual(first.headers["X-Intake-Status"], "created")
        second = self.client.post("/webhooks/sms?token=test-token", data=payload)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.headers.get("X-Intake-Duplicate"), "1")
        invoices = self.query("SELECT * FROM invoices")
        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices[0]["tech_name"], "Inbound")
        self.assertIn("Broadway", invoices[0]["address"])
        self.assertIn("Client: Sarah Johnson", invoices[0]["description"])
        self.assertIn("Requested:", invoices[0]["description"])
        self.assertNotIn("hour", invoices[0].keys())
        messages = self.query("SELECT * FROM messages")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["source"], "sms")
        self.assertEqual(messages[0]["invoice_id"], invoices[0]["id"])

    def test_twilio_signature(self):
        os.environ["TWILIO_AUTH_TOKEN"] = "auth-token"
        payload = {
            "From": "+13035550142",
            "Body": "Please patch the ceiling at 10 Main St, Denver CO tomorrow.",
            "MessageSid": "SM-signed",
        }
        url = "http://localhost/webhooks/sms?token=test-token"
        signature = compute_twilio_signature(url, payload, "auth-token")
        bad = self.client.post(
            "/webhooks/sms?token=test-token",
            data=payload,
            headers={"X-Twilio-Signature": "not-the-signature"},
        )
        self.assertEqual(bad.status_code, 403)
        self.assertEqual(self.query("SELECT id FROM messages"), [])
        good = self.client.post(
            "/webhooks/sms?token=test-token",
            data=payload,
            headers={"X-Twilio-Signature": signature},
        )
        self.assertEqual(good.status_code, 200, good.get_data(as_text=True))
        self.assertEqual(len(self.query("SELECT id FROM invoices")), 1)

    def test_email_form_and_json(self):
        form = self.client.post(
            "/webhooks/email?token=test-token",
            data={
                "sender": "Sarah Johnson <sarah@example.com>",
                "subject": "Drywall quote",
                "stripped-text": (
                    "We have water damage in the basement ceiling. "
                    "Please come look at 900 E 17th Ave, Denver."
                ),
                "Message-Id": "<one@mail>",
            },
        )
        self.assertEqual(form.status_code, 200)
        self.assertEqual(form.get_json()["status"], "created")
        posted = self.client.post(
            "/webhooks/email",
            json={
                "From": "bob@example.com",
                "FromName": "Bob Stone",
                "Subject": "Hole in the wall",
                "TextBody": "There is a hole in the bedroom wall at 55 Main Street Apt 4, Boulder, CO 80301.",
                "MessageID": "postmark-1",
            },
            headers={"X-Intake-Token": "test-token"},
        )
        self.assertEqual(posted.status_code, 200)
        self.assertEqual(posted.get_json()["status"], "created")
        invoices = self.query("SELECT * FROM invoices ORDER BY id")
        self.assertEqual(len(invoices), 2)
        self.assertIn("17th", invoices[0]["address"])
        self.assertIn("Sarah Johnson", invoices[0]["description"])
        self.assertIn("sarah@example.com", invoices[0]["description"])
        self.assertIn("80301", invoices[1]["address"])
        self.assertIn("Bob Stone", invoices[1]["description"])

    def test_same_sender_fills_missing_address_and_links_one_job(self):
        self.client.post(
            "/webhooks/sms?token=test-token",
            data={
                "From": "+13035550142",
                "Body": "This is Sarah Johnson. I need the ceiling patched tomorrow.",
                "MessageSid": "SM-a",
            },
        )
        self.client.post(
            "/webhooks/sms?token=test-token",
            data={
                "From": "+13035550142",
                "Body": "Also tape the hallway please.",
                "MessageSid": "SM-b",
            },
        )
        self.assertEqual(self.query("SELECT id FROM invoices"), [])
        done = self.client.post(
            "/webhooks/sms?token=test-token",
            data={
                "From": "+13035550142",
                "Body": "1842 S Broadway, Denver CO 80210",
                "MessageSid": "SM-c",
            },
        )
        self.assertEqual(done.status_code, 200)
        invoices = self.query("SELECT * FROM invoices")
        self.assertEqual(len(invoices), 1)
        self.assertIn("Broadway", invoices[0]["address"])
        description = invoices[0]["description"].lower()
        self.assertIn("ceiling", description)
        self.assertIn("hallway", description)
        self.assertIn("Sarah Johnson", invoices[0]["description"])
        messages = self.query("SELECT status, invoice_id FROM messages ORDER BY id")
        self.assertEqual([row["status"] for row in messages], ["created", "created", "created"])
        self.assertEqual({row["invoice_id"] for row in messages}, {invoices[0]["id"]})

    def test_later_message_uses_saved_address(self):
        self.client.post(
            "/webhooks/sms?token=test-token",
            data={
                "From": "+13035550142",
                "Body": "This is Sarah Johnson. Patch the ceiling at 10 Main St, Denver CO today.",
                "MessageSid": "SM-first",
            },
        )
        second = self.client.post(
            "/webhooks/sms?token=test-token",
            data={
                "From": "+13035550142",
                "Body": "Please also tape the hallway.",
                "MessageSid": "SM-second",
            },
        )
        self.assertEqual(second.headers["X-Intake-Status"], "created")
        invoices = self.query("SELECT * FROM invoices ORDER BY id")
        self.assertEqual(len(invoices), 2)
        self.assertIn("Main", invoices[1]["address"])
        self.assertIn("hallway", invoices[1]["description"].lower())
        self.assertIn("Sarah Johnson", invoices[1]["description"])
        contacts = self.query("SELECT * FROM contacts")
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["phone"], "3035550142")
        self.assertEqual(contacts[0]["name"], "Sarah Johnson")
        self.assertIn("Main", contacts[0]["address"])

    def test_manual_page_requires_admin_and_can_fix_a_message(self):
        blocked = self.client.get("/admin/messages")
        self.assertEqual(blocked.status_code, 302)
        self.assertIn("/login", blocked.headers["Location"])
        self.login()
        saved = self.client.post(
            "/admin/messages",
            data={
                "sender": "+13035550199",
                "text": "This is Dana. Please patch the ceiling.",
            },
            follow_redirects=True,
        )
        page = saved.get_data(as_text=True)
        self.assertIn("Needs review", page)
        self.assertIn("No street address", page)
        self.assertEqual(self.query("SELECT id FROM invoices"), [])
        message_id = self.query("SELECT id FROM messages")[0]["id"]
        # A bad phone is rejected and does not open a job.
        invalid = self.client.post(
            f"/admin/messages/{message_id}",
            data={
                "client_name": "Dana Cole",
                "phone": "555",
                "email": "",
                "address": "10 Main St, Denver CO",
                "requested_when": "Friday morning",
                "description": "Patch the ceiling",
            },
            follow_redirects=True,
        )
        self.assertIn("10-digit", invalid.get_data(as_text=True))
        self.assertEqual(self.query("SELECT id FROM invoices"), [])
        fixed = self.client.post(
            f"/admin/messages/{message_id}",
            data={
                "client_name": "Dana Cole",
                "phone": "(303) 555-0199",
                "email": "dana@example.com",
                "address": "10 Main St, Denver CO",
                "requested_when": "Friday morning",
                "description": "Patch the ceiling",
            },
            follow_redirects=True,
        )
        text = fixed.get_data(as_text=True)
        self.assertIn("INV-0001", text)
        self.assertIn("Job created", text)
        invoices = self.query("SELECT * FROM invoices")
        self.assertEqual(len(invoices), 1)
        self.assertIn("Dana Cole", invoices[0]["description"])
        self.assertIn("Friday morning", invoices[0]["description"])
        self.assertEqual(invoices[0]["tech_name"], "Inbound")
        # Creating another job keeps the first invoice.
        again = self.client.post(
            f"/admin/messages/{message_id}",
            data={
                "client_name": "Dana Cole",
                "phone": "(303) 555-0199",
                "email": "dana@example.com",
                "address": "10 Main St, Denver CO",
                "requested_when": "Friday morning",
                "description": "Patch the ceiling and the closet",
            },
            follow_redirects=True,
        )
        self.assertIn("INV-0002", again.get_data(as_text=True))
        self.assertEqual(len(self.query("SELECT id FROM invoices")), 2)
        linked = self.query("SELECT invoice_id FROM messages WHERE id = ?", (message_id,))
        self.assertEqual(linked[0]["invoice_id"], self.query("SELECT id FROM invoices ORDER BY id DESC LIMIT 1")[0]["id"])

    def test_detail_form_prefills_saved_address(self):
        self.client.post(
            "/api/intake/messages",
            json={
                "from": "+13035550142",
                "text": "This is Sarah. Please patch the ceiling.",
                "external_id": "open-1",
            },
            headers={"X-Intake-Token": "test-token"},
        )
        self.client.post(
            "/api/intake/messages",
            json={
                "from": "+13035550142",
                "text": "Please mud the bedroom at 10 Main St, Denver CO.",
                "external_id": "bedroom-1",
            },
            headers={"X-Intake-Token": "test-token"},
        )
        invoices = self.query("SELECT * FROM invoices ORDER BY id")
        self.assertEqual(len(invoices), 1)
        self.assertIn("bedroom", invoices[0]["description"].lower())
        self.assertIn("Main", invoices[0]["address"])
        open_message = self.query(
            "SELECT id FROM messages WHERE external_id = ?",
            ("open-1",),
        )[0]
        self.login()
        detail = self.client.get(f"/admin/messages/{open_message['id']}")
        html = detail.get_data(as_text=True)
        self.assertIn("10 Main St", html)
        self.assertIn("Saved for this sender", html)
        self.assertIn("Sarah", html)

    def test_admin_json_without_webhook_token(self):
        os.environ.pop("INTAKE_WEBHOOK_TOKEN", None)
        try:
            anonymous = self.client.post(
                "/api/intake/messages",
                json={"from": "a@b.co", "text": "Patch the wall at 10 Main St, Denver CO"},
            )
            self.assertEqual(anonymous.status_code, 503)
            self.login()
            allowed = self.client.post(
                "/api/intake/messages",
                json={"from": "a@b.co", "text": "Patch the wall at 10 Main St, Denver CO"},
            )
        finally:
            os.environ["INTAKE_WEBHOOK_TOKEN"] = "test-token"
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.get_json()["status"], "created")

    def test_invoice_page_links_back_to_the_message_and_pdf(self):
        self.login()
        self.client.post(
            "/admin/messages",
            data={
                "sender": "Riley <riley@example.com>",
                "text": "Please hang drywall at 4 Cedar Ln, Denver CO on Monday.",
            },
        )
        invoice = self.query("SELECT * FROM invoices")[0]
        message = self.query("SELECT id FROM messages")[0]
        page = self.client.get(f"/invoice/{invoice['id']}")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn(f"/admin/messages/{message['id']}", html)
        self.assertIn("4 Cedar", html)
        pdf = self.client.get(f"/invoice/{invoice['id']}/pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))

    def test_messages_page_is_usable_markup_on_a_phone(self):
        self.login()
        self.client.post(
            "/admin/messages",
            data={"sender": "Pat", "text": "Need an estimate for a small patch."},
        )
        page = self.client.get("/admin/messages")
        html = page.get_data(as_text=True)
        self.assertIn("viewport", html)
        self.assertIn("Paste a message", html)
        self.assertIn("Needs review", html)
        self.assertIn('class="message-list"', html)
        self.assertIn("Review", html)
        css = (ROOT / "static" / "style.css").read_text()
        self.assertIn("@media (max-width: 640px)", css)
        self.assertIn(".message-list", css)


if __name__ == "__main__":
    unittest.main()
