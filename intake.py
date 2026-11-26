"""Store inbound messages, remember senders, and open jobs from them.

Jobs go through the app's existing invoice insert. A requested date/time is
copied onto the job as text. Nothing here tracks hours.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from parsing import (
    ParsedMessage,
    address_usable,
    find_phones,
    format_phone,
    has_work_terms,
    normalize_phone,
    parse_message,
    split_sender,
)

INBOUND_TECH_NAME = "Inbound"
NO_ADDRESS_ERROR = (
    "No street address yet. Add one on this message, or send another message from this sender."
)
EMPTY_ERROR = "Message was empty."
_PRIOR_DAYS = 30
_EMAIL_EXACT = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

CreateInvoice = Callable[[Any, dict[str, str]], int]


@dataclass
class IntakeResult:
    message_id: int
    status: str
    invoice_id: int | None = None
    invoice_number: str | None = None
    duplicate: bool = False
    error: str | None = None


@dataclass
class CorrectionResult:
    ok: bool
    errors: dict[str, str]
    values: dict[str, str]
    status: str
    invoice_id: int | None = None
    invoice_number: str | None = None
    message: str = ""


def ingest_inbound(
    db: Any,
    create_invoice: CreateInvoice,
    *,
    source: str,
    sender: str,
    raw_text: str,
    subject: str = "",
    external_id: str | None = None,
) -> IntakeResult:
    """Save one inbound message and open a job when it has a street address."""
    if source not in {"sms", "email", "api", "manual"}:
        raise ValueError(f"Unknown message source: {source}")
    sender = (sender or "").strip()[:500]
    subject = (subject or "").strip()[:300]
    raw_text = raw_text or ""
    if len(raw_text) > 50000:
        raw_text = raw_text[:50000]
    external_id = (external_id or "").strip()[:255] or None

    if external_id:
        existing = db.execute(
            "SELECT * FROM messages WHERE external_id = ?",
            (external_id,),
        ).fetchone()
        if existing:
            return _result_from_row(db, existing, duplicate=True)

    explicit = parse_message(raw_text, sender=sender, subject=subject)
    _sender_name, sender_phone, sender_email = split_sender(sender)
    phones: list[str] = []
    for candidate in (sender_phone, explicit.phone, *find_phones(raw_text)):
        if candidate and candidate not in phones:
            phones.append(candidate)
    email = (explicit.email or sender_email or "").lower()
    contact = find_contact(db, phones, email)
    filled = fill_from_contact(explicit, contact)

    open_messages: list[Any] = []
    if contact is not None and _address_only(raw_text, explicit):
        open_messages = _open_messages(db, int(contact["id"]))
    link_prior = bool(open_messages)

    contact_id = upsert_contact(
        db,
        contact,
        explicit,
        sender_phone=sender_phone or (phones[0] if phones else ""),
    )

    job_parsed: ParsedMessage | None = None
    note = ""
    error: str | None = None
    if not raw_text.strip() and not subject.strip():
        status = "failed"
        error = EMPTY_ERROR
    elif link_prior:
        status = "needs_review"
        job_parsed = _merge_open_messages(open_messages, explicit, contact)
        note = (
            "Address added to the earlier messages from this sender."
            if len(open_messages) > 1
            else "Address added to an earlier message from this sender."
        )
    elif address_usable(filled.address):
        status = "needs_review"
        job_parsed = filled
        if "address" in filled.filled_from_contact:
            note = "Address filled in from an earlier message from this sender."
    else:
        status = "needs_review"
        error = NO_ADDRESS_ERROR

    try:
        message_id = _insert_message(
            db,
            source=source,
            sender=sender,
            subject=subject,
            raw_text=raw_text,
            parsed=filled,
            status=status,
            error=error,
            note=note if job_parsed is None else "",
            external_id=external_id,
            contact_id=contact_id,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        if external_id and _is_unique_violation(exc):
            existing = db.execute(
                "SELECT * FROM messages WHERE external_id = ?",
                (external_id,),
            ).fetchone()
            if existing:
                return _result_from_row(db, existing, duplicate=True)
        raise

    if job_parsed is None or not address_usable(job_parsed.address):
        return IntakeResult(
            message_id=message_id,
            status=status,
            error=error,
        )

    try:
        invoice_id = create_invoice(db, job_fields(job_parsed))
    except Exception as exc:
        db.rollback()
        failure = f"Could not create the job ({exc.__class__.__name__})."
        db.execute(
            "UPDATE messages SET status = 'needs_review', error = ?, note = ? WHERE id = ?",
            (failure, "", message_id),
        )
        db.commit()
        return IntakeResult(message_id=message_id, status="needs_review", error=failure)

    invoice_number = _invoice_number(db, invoice_id)
    db.execute(
        """
        UPDATE messages
        SET status = 'created', invoice_id = ?, error = NULL, note = ?, contact_id = ?
        WHERE id = ?
        """,
        (invoice_id, note, contact_id, message_id),
    )
    if link_prior and job_parsed is not None:
        for prior in open_messages:
            prior_parsed = ParsedMessage.from_json(prior["parsed_json"])
            prior_parsed.address = explicit.address
            db.execute(
                """
                UPDATE messages
                SET status = 'created', invoice_id = ?, error = NULL,
                    parsed_json = ?, note = ?
                WHERE id = ?
                """,
                (
                    invoice_id,
                    prior_parsed.to_json(),
                    "Address supplied by a later message.",
                    int(prior["id"]),
                ),
            )
    db.commit()
    return IntakeResult(
        message_id=message_id,
        status="created",
        invoice_id=invoice_id,
        invoice_number=invoice_number,
        error=None,
    )


def apply_corrections(
    db: Any,
    create_invoice: CreateInvoice,
    message_id: int,
    form: dict[str, Any],
) -> CorrectionResult | None:
    """Save edited fields and open a job. An existing job is left in place."""
    message = db.execute(
        "SELECT * FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    if message is None:
        return None

    values = {
        "client_name": _clean(form.get("client_name"), 120),
        "phone": _clean(form.get("phone"), 40),
        "email": _clean(form.get("email"), 200).lower(),
        "address": re.sub(r"\s+", " ", _clean(form.get("address"), 300)),
        "requested_when": _clean(form.get("requested_when"), 200),
        "description": _clean(form.get("description"), 4000),
    }
    errors: dict[str, str] = {}
    phone = ""
    if values["phone"]:
        phone = normalize_phone(values["phone"])
        if not phone:
            errors["phone"] = "Enter a 10-digit US phone number, or leave phone blank."
    if values["email"] and not _EMAIL_EXACT.fullmatch(values["email"]):
        errors["email"] = "Enter a valid email, or leave it blank."
    if not address_usable(values["address"]):
        errors["address"] = "Enter a street address, for example 10 Main St, Denver CO."
    if not values["description"]:
        errors["description"] = "Enter what the customer wants done."
    if errors:
        return CorrectionResult(
            ok=False,
            errors=errors,
            values=values,
            status=message["status"],
        )

    parsed = ParsedMessage(
        client_name=values["client_name"],
        phone=phone,
        phone_display=format_phone(phone) if phone else "",
        email=values["email"],
        address=values["address"],
        requested_when=values["requested_when"],
        description=values["description"],
    )
    contact = None
    if message["contact_id"]:
        contact = db.execute(
            "SELECT * FROM contacts WHERE id = ?",
            (message["contact_id"],),
        ).fetchone()
    if contact is None:
        contact = find_contact(db, [phone] if phone else [], values["email"])
    contact_id = save_contact_overwrite(db, contact, parsed)
    db.execute(
        """
        UPDATE messages
        SET parsed_json = ?, contact_id = ?, error = NULL
        WHERE id = ?
        """,
        (parsed.to_json(), contact_id, message_id),
    )
    db.commit()

    old_invoice_id = message["invoice_id"]
    try:
        invoice_id = create_invoice(db, job_fields(parsed))
    except Exception as exc:
        db.rollback()
        db.execute(
            "UPDATE messages SET status = 'needs_review', error = ? WHERE id = ?",
            (f"Could not create the job ({exc.__class__.__name__}).", message_id),
        )
        db.commit()
        return CorrectionResult(
            ok=False,
            errors={"form": "The message was saved, but the job could not be created."},
            values=values,
            status="needs_review",
            message=str(exc.__class__.__name__),
        )

    note = message["note"] or ""
    if old_invoice_id:
        note = (
            "A new job was created from the corrected details. "
            "The earlier job is still in the invoice list."
        )
    db.execute(
        """
        UPDATE messages
        SET status = 'created', invoice_id = ?, error = NULL, note = ?,
            parsed_json = ?, contact_id = ?
        WHERE id = ?
        """,
        (invoice_id, note, parsed.to_json(), contact_id, message_id),
    )
    db.commit()
    return CorrectionResult(
        ok=True,
        errors={},
        values=values,
        status="created",
        invoice_id=invoice_id,
        invoice_number=_invoice_number(db, invoice_id),
        message="created",
    )


def prefill_form(message: Any, contact: Any) -> dict[str, Any]:
    """Form defaults, using this sender's saved details when the message is missing them."""
    parsed = ParsedMessage.from_json(message["parsed_json"])
    values: dict[str, Any] = {
        "client_name": parsed.client_name,
        "phone": parsed.phone_display or parsed.phone,
        "email": parsed.email,
        "address": parsed.address,
        "requested_when": parsed.requested_when,
        "description": parsed.description,
        "hints": [],
    }
    hints: list[str] = []
    if contact is not None:
        if not values["client_name"] and contact["name"]:
            values["client_name"] = contact["name"]
            hints.append("name")
        if not values["phone"] and contact["phone"]:
            values["phone"] = format_phone(contact["phone"])
            hints.append("phone")
        if not values["email"] and contact["email"]:
            values["email"] = contact["email"]
            hints.append("email")
        if not values["address"] and contact["address"]:
            values["address"] = contact["address"]
            hints.append("address")
    if not values["description"]:
        values["description"] = (message["raw_text"] or "").strip()
    values["hints"] = hints
    return values


def find_contact(db: Any, phones: list[str], email: str):
    for phone in phones:
        if not phone:
            continue
        row = db.execute(
            "SELECT * FROM contacts WHERE phone = ?",
            (phone,),
        ).fetchone()
        if row is not None:
            return row
    if email:
        row = db.execute(
            "SELECT * FROM contacts WHERE email = ?",
            (email.lower(),),
        ).fetchone()
        if row is not None:
            return row
    return None


def fill_from_contact(parsed: ParsedMessage, contact: Any) -> ParsedMessage:
    result = ParsedMessage.from_json(parsed.to_json())
    if contact is None:
        return result
    filled = list(result.filled_from_contact)
    if not result.client_name and contact["name"]:
        result.client_name = contact["name"]
        filled.append("client_name")
    if not result.phone and contact["phone"]:
        result.phone = contact["phone"]
        result.phone_display = format_phone(contact["phone"])
        filled.append("phone")
    if not result.email and contact["email"]:
        result.email = contact["email"]
        filled.append("email")
    if not result.address and contact["address"]:
        result.address = contact["address"]
        filled.append("address")
    result.filled_from_contact = filled
    return result


def upsert_contact(db: Any, contact: Any, explicit: ParsedMessage, *, sender_phone: str) -> int | None:
    """Remember new details. A blank field on this message does not erase a saved one."""
    now = _now_iso()
    phone = sender_phone or explicit.phone or ""
    email = explicit.email or ""
    if contact is None:
        if not any((explicit.client_name, phone, email, explicit.address)):
            return None
        row = db.execute(
            """
            INSERT INTO contacts (name, phone, email, address, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                explicit.client_name or None,
                phone or None,
                email or None,
                explicit.address or None,
                now,
                now,
            ),
        ).fetchone()
        return int(row["id"])

    name = contact["name"] or ""
    if explicit.client_name and (
        not name or len(explicit.client_name.split()) > len(name.split())
    ):
        name = explicit.client_name
    saved_phone = contact["phone"] or phone or None
    saved_email = contact["email"] or email or None
    saved_address = explicit.address or contact["address"] or None
    db.execute(
        """
        UPDATE contacts
        SET name = ?, phone = ?, email = ?, address = ?, updated_at = ?
        WHERE id = ?
        """,
        (name or None, saved_phone, saved_email, saved_address, now, int(contact["id"])),
    )
    return int(contact["id"])


def save_contact_overwrite(db: Any, contact: Any, parsed: ParsedMessage) -> int | None:
    """Manual corrections replace whatever the form actually filled in."""
    now = _now_iso()
    if contact is None:
        if not any((parsed.client_name, parsed.phone, parsed.email, parsed.address)):
            return None
        row = db.execute(
            """
            INSERT INTO contacts (name, phone, email, address, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                parsed.client_name or None,
                parsed.phone or None,
                parsed.email or None,
                parsed.address or None,
                now,
                now,
            ),
        ).fetchone()
        return int(row["id"])
    db.execute(
        """
        UPDATE contacts
        SET name = ?, phone = ?, email = ?, address = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            parsed.client_name or contact["name"] or None,
            parsed.phone or contact["phone"] or None,
            parsed.email or contact["email"] or None,
            parsed.address or contact["address"] or None,
            now,
            int(contact["id"]),
        ),
    )
    return int(contact["id"])


def job_fields(parsed: ParsedMessage) -> dict[str, str]:
    return {
        "address": parsed.address.strip(),
        "description": compose_description(parsed),
        "tech_name": INBOUND_TECH_NAME,
    }


def compose_description(parsed: ParsedMessage) -> str:
    body = (parsed.description or "").strip()
    meta: list[str] = []
    if parsed.client_name and f"Client: {parsed.client_name}" not in body:
        meta.append(f"Client: {parsed.client_name}")
    phone = parsed.phone_display or (format_phone(parsed.phone) if parsed.phone else "")
    if phone and f"Phone: {phone}" not in body:
        meta.append(f"Phone: {phone}")
    if parsed.email and f"Email: {parsed.email}" not in body:
        meta.append(f"Email: {parsed.email}")
    if parsed.requested_when and f"Requested: {parsed.requested_when}" not in body:
        meta.append(f"Requested: {parsed.requested_when}")
    if not body:
        body = "Job request from an inbound message."
    if meta:
        return body + "\n\n" + "\n".join(meta)
    return body


def fields_from_mapping(
    data: dict | None,
    channel: str,
    headers: dict | None = None,
) -> dict[str, str | None]:
    """Normalize Twilio, inbound-email, and JSON bodies into one shape."""
    payload = _flatten_email_payload(data or {}) if channel == "email" else dict(data or {})
    # Drop nested values that pick() cannot read, but keep flat scalars.
    payload = {
        key: value
        for key, value in payload.items()
        if not isinstance(value, (dict, list)) and value is not None
    }
    subject = ""
    if channel == "sms":
        sender = pick(payload, "from", "sender")
        text = pick(payload, "body", "text")
        external_id = pick(payload, "messagesid", "message_sid", "smsmessagesid", "smsid")
    elif channel == "email":
        sender = pick(payload, "from", "sender")
        from_name = pick(payload, "fromname", "from_name", "sendername")
        if from_name and sender and "<" not in sender and "@" in sender:
            sender = f"{from_name} <{sender}>"
        elif from_name and not sender:
            sender = from_name
        subject = pick(payload, "subject")
        text = pick(
            payload,
            "stripped-text",
            "stripped_text",
            "text",
            "body-plain",
            "body_plain",
            "plain",
            "textbody",
            "body",
        )
        if not text:
            text = strip_html(
                pick(payload, "html", "htmlbody", "body-html", "stripped-html")
            )
        external_id = pick(
            payload, "message-id", "messageid", "message_id", "external_id"
        )
    else:
        sender = pick(payload, "from", "sender")
        subject = pick(payload, "subject")
        text = pick(payload, "text", "body", "message")
        external_id = pick(
            payload, "external_id", "message-id", "messageid", "messagesid"
        )
    if not external_id and headers:
        external_id = pick(dict(headers), "message-id", "x-message-id")
    return {
        "sender": sender,
        "subject": subject,
        "text": text,
        "external_id": external_id or None,
    }


def pick(data: dict, *names: str) -> str:
    folded: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, (dict, list)) or value is None:
            continue
        folded[str(key).strip().lower()] = str(value).strip()
    for name in names:
        found = folded.get(name.lower(), "")
        if found:
            return found
    return ""


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


def _flatten_email_payload(data: dict) -> dict:
    flat: dict[str, Any] = {}
    nested: list[tuple[Any, dict]] = []
    for key, value in data.items():
        if isinstance(value, dict):
            nested.append((key, value))
            continue
        if isinstance(value, list) or value is None:
            continue
        flat[key] = value
    for key, value in nested:
        if str(key).lower() not in {"fromfull", "from_full"}:
            continue
        email = str(value.get("Email") or value.get("email") or "").strip()
        name = str(value.get("Name") or value.get("name") or "").strip()
        if email and not pick(flat, "from", "sender"):
            flat["from"] = email
        if name and not pick(flat, "fromname", "from_name"):
            flat["fromname"] = name
    return flat


def _address_only(raw_text: str, explicit: ParsedMessage) -> bool:
    if not address_usable(explicit.address):
        return False
    return not has_work_terms(raw_text) and not has_work_terms(explicit.description)


def _merge_open_messages(rows: list[Any], explicit: ParsedMessage, contact: Any) -> ParsedMessage:
    """One job for a short thread: keep every open description and use the new address."""
    combined = ParsedMessage()
    descriptions: list[str] = []
    for row in reversed(rows):
        parsed = ParsedMessage.from_json(row["parsed_json"])
        if not combined.client_name and parsed.client_name:
            combined.client_name = parsed.client_name
        if not combined.phone and parsed.phone:
            combined.phone = parsed.phone
            combined.phone_display = parsed.phone_display or format_phone(parsed.phone)
        if not combined.email and parsed.email:
            combined.email = parsed.email
        if not combined.requested_when and parsed.requested_when:
            combined.requested_when = parsed.requested_when
        if parsed.description and parsed.description not in descriptions:
            descriptions.append(parsed.description)
    if not combined.client_name and explicit.client_name:
        combined.client_name = explicit.client_name
    if not combined.phone and explicit.phone:
        combined.phone = explicit.phone
        combined.phone_display = explicit.phone_display or format_phone(explicit.phone)
    if not combined.email and explicit.email:
        combined.email = explicit.email
    if explicit.requested_when and not combined.requested_when:
        combined.requested_when = explicit.requested_when
    combined.address = explicit.address
    combined.description = " ".join(descriptions)
    return fill_from_contact(combined, contact)


def _open_messages(db: Any, contact_id: int) -> list[Any]:
    rows = db.execute(
        """
        SELECT * FROM messages
        WHERE contact_id = ? AND status = 'needs_review' AND invoice_id IS NULL
        ORDER BY id DESC
        LIMIT 8
        """,
        (contact_id,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    return [row for row in rows if _within_days(row["received_at"], now, _PRIOR_DAYS)]


def _within_days(iso_value: str | None, now: datetime, days: int) -> bool:
    if not iso_value:
        return False
    try:
        then = datetime.fromisoformat(iso_value)
    except ValueError:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return now - then <= timedelta(days=days)


def _insert_message(
    db: Any,
    *,
    source: str,
    sender: str,
    subject: str,
    raw_text: str,
    parsed: ParsedMessage,
    status: str,
    error: str | None,
    note: str,
    external_id: str | None,
    contact_id: int | None,
) -> int:
    row = db.execute(
        """
        INSERT INTO messages (
            source, sender, subject, raw_text, parsed_json, status, error, note,
            external_id, contact_id, invoice_id, received_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        RETURNING id
        """,
        (
            source,
            sender or None,
            subject or None,
            raw_text,
            parsed.to_json(),
            status,
            error,
            note or None,
            external_id,
            contact_id,
            _now_iso(),
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("Failed to store the inbound message.")
    return int(row["id"])


def _result_from_row(db: Any, row: Any, *, duplicate: bool) -> IntakeResult:
    invoice_id = row["invoice_id"]
    return IntakeResult(
        message_id=int(row["id"]),
        status=row["status"],
        invoice_id=int(invoice_id) if invoice_id else None,
        invoice_number=_invoice_number(db, int(invoice_id)) if invoice_id else None,
        duplicate=duplicate,
        error=row["error"],
    )


def _invoice_number(db: Any, invoice_id: int) -> str | None:
    row = db.execute(
        "SELECT invoice_number FROM invoices WHERE id = ?",
        (invoice_id,),
    ).fetchone()
    if row is None:
        return None
    return row["invoice_number"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _is_unique_violation(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.__class__.__name__ in {"IntegrityError", "UniqueViolation"}:
            return True
        current = current.__cause__
    return False
