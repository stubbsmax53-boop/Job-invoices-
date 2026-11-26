"""Rule-based parser, Twilio signature, and webhook field normalization."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("INTAKE_LLM_API_KEY", None)

from intake import fields_from_mapping  # noqa: E402
from parsing import (  # noqa: E402
    compute_twilio_signature,
    parse_message,
    twilio_signature_valid,
)

NOW = datetime(2026, 11, 26, 15, 0, tzinfo=ZoneInfo("America/Denver"))


class ParserTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("INTAKE_LLM_API_KEY", None)

    def test_full_text_message(self):
        parsed = parse_message(
            "Hi this is Sarah Johnson. I need drywall repair in the living room. "
            "Can you come Tuesday at 2pm? Address is 1842 S Broadway, Denver CO 80210. "
            "Call me at (303) 555-0142.",
            now=NOW,
        )
        self.assertEqual(parsed.client_name, "Sarah Johnson")
        self.assertEqual(parsed.phone, "3035550142")
        self.assertEqual(parsed.phone_display, "(303) 555-0142")
        self.assertIn("1842", parsed.address)
        self.assertIn("Broadway", parsed.address)
        self.assertIn("80210", parsed.address)
        self.assertIn("Denver", parsed.address)
        self.assertIn("Dec 1, 2026", parsed.requested_when)
        self.assertIn("2:00 PM", parsed.requested_when)
        self.assertIn("drywall", parsed.description.lower())
        self.assertIn("living room", parsed.description.lower())

    def test_address_variants(self):
        samples = {
            "900 E 17th Ave, Denver": ("17th", "Denver"),
            "55 Main Street Apt 4, Boulder, CO 80301": ("Main", "80301"),
            "1842 S Broadway Denver CO 80210": ("Broadway", "80210"),
            "10 main st, denver co": ("Main", "Denver"),
        }
        for text, (street, tail) in samples.items():
            parsed = parse_message(f"Please patch the wall at {text}.", now=NOW)
            self.assertIn(street, parsed.address, text)
            self.assertIn(tail, parsed.address, text)

    def test_phone_formats_and_not_used_as_address(self):
        for raw in ("(303) 555-0199", "303-555-0199", "+1 303 555 0199", "303.555.0199"):
            parsed = parse_message(f"Call me at {raw} about the ceiling patch.", now=NOW)
            self.assertEqual(parsed.phone, "3035550199", raw)
            self.assertEqual(parsed.address, "", raw)
            self.assertIn("ceiling", parsed.description.lower())

    def test_relative_dates(self):
        tomorrow = parse_message("Can you come tomorrow morning?", now=NOW)
        self.assertIn("Nov 27, 2026", tomorrow.requested_when)
        self.assertIn("9:00 AM", tomorrow.requested_when)

        afternoon = parse_message("December 15 at 10:30 am works.", now=NOW)
        self.assertIn("Dec 15, 2026", afternoon.requested_when)
        self.assertIn("10:30 AM", afternoon.requested_when)

        bare = parse_message("Come at 2.", now=NOW)
        self.assertEqual(bare.requested_when, "2:00 PM")
        morning_hour = parse_message("Come at 10.", now=NOW)
        self.assertEqual(morning_hour.requested_when, "10:00 AM")

        rolled = parse_message("Can you come March 15?", now=NOW)
        self.assertIn("Mar 15, 2027", rolled.requested_when)

        in_days = parse_message("We need the repair in 3 days.", now=NOW)
        self.assertIn("Nov 29, 2026", in_days.requested_when)

    def test_street_number_is_not_a_clock_time(self):
        parsed = parse_message(
            "Please patch the wall at 10 Main St, Denver CO tomorrow at 2pm.",
            now=NOW,
        )
        self.assertIn("Main", parsed.address)
        self.assertIn("2:00 PM", parsed.requested_when)
        self.assertNotIn("10:00", parsed.requested_when)

    def test_email_sender_and_subject(self):
        parsed = parse_message(
            "We have water damage in the basement ceiling. Sometime next Friday morning works.",
            sender="Sarah Johnson <sarah.j@example.com>",
            subject="Drywall quote",
            now=NOW,
        )
        self.assertEqual(parsed.client_name, "Sarah Johnson")
        self.assertEqual(parsed.email, "sarah.j@example.com")
        self.assertIn("water damage", parsed.description.lower())
        self.assertIn("Nov 27, 2026", parsed.requested_when)
        self.assertIn("9:00 AM", parsed.requested_when)

    def test_does_not_invent_an_address(self):
        parsed = parse_message("I need 2 days on the road and a quote.", now=NOW)
        self.assertEqual(parsed.address, "")

    def test_llm_is_not_called_without_a_key(self):
        with mock.patch("parsing.call_openai_json") as call:
            parse_message("Need drywall at 10 Main St, Denver CO tomorrow", now=NOW)
        call.assert_not_called()

    def test_llm_failure_falls_back_to_rules(self):
        os.environ["OPENAI_API_KEY"] = "test-key"
        try:
            with mock.patch("parsing.call_openai_json", side_effect=RuntimeError("down")):
                parsed = parse_message(
                    "Need drywall at 10 Main St, Denver CO tomorrow",
                    now=NOW,
                )
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
        self.assertIn("Main", parsed.address)
        self.assertIn("Nov 27, 2026", parsed.requested_when)

    def test_llm_fields_override_when_plausible(self):
        os.environ["OPENAI_API_KEY"] = "test-key"
        payload = {
            "client_name": "Sarah Johnson",
            "phone": "(303) 555-0142",
            "email": "sarah@example.com",
            "address": "10 Main St, Denver, CO 80202",
            "requested_when": "tomorrow afternoon",
            "description": "Patch the hallway",
        }
        try:
            with mock.patch("parsing.call_openai_json", return_value=payload):
                parsed = parse_message("messy note", now=NOW)
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
        self.assertEqual(parsed.client_name, "Sarah Johnson")
        self.assertEqual(parsed.phone, "3035550142")
        self.assertEqual(parsed.email, "sarah@example.com")
        self.assertEqual(parsed.address, "10 Main St, Denver, CO 80202")
        self.assertEqual(parsed.description, "Patch the hallway")
        self.assertEqual(parsed.requested_when, "tomorrow afternoon")

    def test_llm_does_not_wipe_rules_with_empty_or_junk(self):
        os.environ["OPENAI_API_KEY"] = "test-key"
        try:
            with mock.patch(
                "parsing.call_openai_json",
                return_value={
                    "client_name": "soon",
                    "phone": "",
                    "email": "not-an-email",
                    "address": "tomorrow",
                    "requested_when": "",
                    "description": "",
                },
            ):
                parsed = parse_message(
                    "This is Sara Lane. Patch the ceiling at 10 Main St, Denver CO. Call 303-555-0100.",
                    now=NOW,
                )
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
        self.assertEqual(parsed.client_name, "Sara Lane")
        self.assertIn("Main", parsed.address)
        self.assertEqual(parsed.phone, "3035550100")
        self.assertIn("ceiling", parsed.description.lower())


class TwilioSignatureTests(unittest.TestCase):
    def test_round_trip(self):
        url = "https://job-invoices-7p8s.onrender.com/webhooks/sms?token=abc"
        params = {"Body": "Hi", "From": "+13035550100"}
        signature = compute_twilio_signature(url, params, "auth-token")
        self.assertTrue(twilio_signature_valid(url, params, signature, "auth-token"))
        self.assertFalse(twilio_signature_valid(url, params, signature, "other-token"))
        self.assertFalse(
            twilio_signature_valid(url, {"Body": "Ho", "From": "+13035550100"}, signature, "auth-token")
        )
        self.assertFalse(twilio_signature_valid(url, params, "", "auth-token"))


class PayloadTests(unittest.TestCase):
    def test_mailgun_style_form(self):
        fields = fields_from_mapping(
            {
                "sender": "sarah@example.com",
                "subject": "Ceiling",
                "stripped-text": "Water damage at 10 Main St, Denver CO",
                "Message-Id": "<abc@mail>",
            },
            "email",
        )
        self.assertEqual(fields["sender"], "sarah@example.com")
        self.assertEqual(fields["subject"], "Ceiling")
        self.assertTrue(str(fields["text"]).startswith("Water damage"))
        self.assertEqual(fields["external_id"], "<abc@mail>")

    def test_postmark_style_json(self):
        fields = fields_from_mapping(
            {
                "From": "bob@example.com",
                "FromName": "Bob Stone",
                "Subject": "Hole",
                "TextBody": "Hole in the wall at 10 Main St.",
                "MessageID": "postmark-1",
            },
            "email",
        )
        self.assertEqual(fields["sender"], "Bob Stone <bob@example.com>")
        self.assertEqual(fields["text"], "Hole in the wall at 10 Main St.")
        self.assertEqual(fields["external_id"], "postmark-1")

    def test_html_only_email(self):
        fields = fields_from_mapping(
            {"from": "a@b.co", "html": "<p>Patch the <b>ceiling</b> at 10 Main St.</p>"},
            "email",
        )
        self.assertIn("Patch the", fields["text"])
        self.assertIn("ceiling", fields["text"])
        self.assertNotIn("<p>", fields["text"])

    def test_twilio_form_and_json_api(self):
        sms = fields_from_mapping(
            {"From": "+13035550100", "Body": "Need drywall", "MessageSid": "SM123"},
            "sms",
        )
        self.assertEqual(sms["sender"], "+13035550100")
        self.assertEqual(sms["text"], "Need drywall")
        self.assertEqual(sms["external_id"], "SM123")
        api = fields_from_mapping(
            {"from": "Sam", "text": "Tomorrow", "external_id": "abc"},
            "api",
        )
        self.assertEqual(api["external_id"], "abc")
        self.assertEqual(api["text"], "Tomorrow")


if __name__ == "__main__":
    unittest.main()
