"""Rule-based parsing for inbound job requests.

Pulls a client name, phone, email, street address, requested date/time, and a
work description out of a text, email, or pasted note. An OpenAI-compatible
API is used when OPENAI_API_KEY is set; otherwise the rules stand on their
own. Requested date/time is job information, not hours or time tracking.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Denver")

# Directions are longest-first so "north" wins over "n".
_DIRECTION = (
    r"(?:northeast|northwest|southeast|southwest|north|south|east|west|ne|nw|se|sw|n|s|e|w)"
)
_SUFFIX = (
    r"(?:street|st|avenue|ave|boulevard|blvd|road|rd|drive|dr|lane|ln|court|ct|"
    r"way|place|pl|circle|cir|terrace|ter|parkway|pkwy|highway|hwy|trail|trl|"
    r"pike|loop|alley|square|sq)"
)
_UNIT = r"(?:apt|apartment|unit|suite|ste|\#)"
_WEEKDAY = (
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
)
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)"
)

_WITH_SUFFIX = re.compile(
    rf"""
    \b(?P<street>
        \d{{1,6}}
        \s+
        (?:{_DIRECTION}\.?\s+)?
        (?:[A-Za-z0-9.'/-]+\s+){{0,4}}
        {_SUFFIX}
        \.?
        (?:\s+{_UNIT}\s*[A-Za-z0-9-]+)?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_NO_SUFFIX = re.compile(
    rf"""
    \b(?P<num>\d{{1,6}})
    \s+
    (?P<street>
        (?:{_DIRECTION}\.?\s+)?
        [A-Za-z0-9.'/-]+
        (?:\s+[A-Za-z0-9.'/-]+){{0,3}}
    )
    \s*,\s*
    (?P<city>[A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){{0,2}})
    \s*,?\s+
    (?P<state>[A-Za-z]{{2}})
    (?:\s+(?P<zip>\d{{5}}(?:-\d{{4}})?))?
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)
# No street suffix and no comma, but a state and ZIP keep it from matching random words.
_NO_SUFFIX_LOOSE = re.compile(
    rf"""
    \b(?P<num>\d{{1,6}})
    \s+
    (?P<street>
        (?:{_DIRECTION}\.?\s+)?
        [A-Za-z0-9.'/-]+
        (?:\s+[A-Za-z0-9.'/-]+){{0,2}}
    )
    \s+
    (?P<city>[A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){{0,2}})
    \s+
    (?P<state>[A-Za-z]{{2}})
    \s+
    (?P<zip>\d{{5}}(?:-\d{{4}})?)
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PHONE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]?)?(?:\(\s*)?([2-9]\d{2})(?:\s*\))?[\s.\-]*([2-9]\d{2})[\s.\-]*(\d{4})(?!\d)"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_NAME_INTRO = re.compile(
    r"\b(?:my name is|this is|i am|i'm|it's|its)\s+"
    r"([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){0,2})",
    re.IGNORECASE,
)
WORK_RE = re.compile(
    r"\b(?:drywall|sheet\s*rock|sheetrock|mud(?:ding)?|tap(?:e|ing)|texture|"
    r"patch(?:ing)?|repair(?:s|ing)?|hang(?:ing)?|install(?:ing|ation)?|"
    r"ceilings?|walls?|water\s+damage|holes?|cracks?|skim|finish(?:ing)?|"
    r"corner\s+bead|demo|tear[\s-]?out|quotes?|estimates?|bids?|paint(?:ing)?|"
    r"insulation|basements?|bedrooms?|living\s+rooms?|kitchens?|bathrooms?|"
    r"garages?|hallways?|halls?|closets?|ceilings?)\b",
    re.IGNORECASE,
)

_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
_CITY_STOP = {
    "a", "an", "the", "and", "or", "if", "please", "call", "come", "can",
    "could", "we", "our", "my", "at", "on", "for", "to", "i", "me", "you",
    "your", "this", "that", "let", "thanks", "thank", "text", "tomorrow",
    "today", "need", "needs", "hi", "hello", "hey", "address", "is", "it's",
    "its", "am", "pm", "morning", "afternoon", "evening", "noon", "monday",
    "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "next", "week", "sometime", "looking", "want", "wanted",
}
_NAME_STOP = _CITY_STOP | {
    "regarding", "about", "just", "calling", "wondering", "interested",
    "hoping", "writing", "reaching", "texting", "here", "from", "with",
    "drywall", "job", "work", "again", "back", "available", "looking",
    "soon", "later", "asap", "yes", "yeah", "no", "ok", "okay", "maybe", "sure",
    "free", "ready", "going", "doing", "having", "scheduling", "around",
    "coming", "trying", "sending", "asking",
}
_STREET_NOISE = {
    "the", "a", "an", "on", "my", "your", "this", "that", "to", "of", "for",
    "and", "in", "at", "days", "day", "our", "we", "i", "me", "you", "is",
    "it", "be", "need", "needs",
}
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_GENERIC_SUBJECTS = {
    "re", "fw", "fwd", "hello", "hi", "hey", "question", "follow up",
    "follow-up", "fyi",
}
_SHORT_DIRECTIONS = {
    "n", "s", "e", "w", "ne", "nw", "se", "sw",
    "north", "south", "east", "west",
    "northeast", "northwest", "southeast", "southwest",
}

_LLM_SYSTEM = (
    "You extract job-request details for a drywall contractor. "
    "Reply with a JSON object and no other text. "
    "Keys: client_name, phone, email, address, requested_when, description. "
    "Use empty strings for unknown values. Do not invent facts. "
    "phone is the customer's phone if one is present. "
    "address is only a street address, never a time or a phone number. "
    "description is the work they want done, not the whole message. "
    "requested_when is when they asked to schedule the visit, as a short phrase. "
    "This is scheduling information for the job, not hourly time tracking."
)


@dataclass
class ParsedMessage:
    client_name: str = ""
    phone: str = ""
    phone_display: str = ""
    email: str = ""
    address: str = ""
    requested_when: str = ""
    description: str = ""
    filled_from_contact: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.phone and not self.phone_display:
            self.phone_display = format_phone(self.phone)
        if self.email:
            self.email = self.email.strip().lower()

    def to_dict(self) -> dict:
        return {
            "client_name": self.client_name,
            "phone": self.phone,
            "phone_display": self.phone_display,
            "email": self.email,
            "address": self.address,
            "requested_when": self.requested_when,
            "description": self.description,
            "filled_from_contact": list(self.filled_from_contact),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> ParsedMessage:
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        filled = data.get("filled_from_contact") or []
        if not isinstance(filled, list):
            filled = []
        return cls(
            client_name=str(data.get("client_name") or ""),
            phone=str(data.get("phone") or ""),
            phone_display=str(data.get("phone_display") or ""),
            email=str(data.get("email") or ""),
            address=str(data.get("address") or ""),
            requested_when=str(data.get("requested_when") or ""),
            description=str(data.get("description") or ""),
            filled_from_contact=[str(item) for item in filled],
        )


def normalize_phone(raw: str | None) -> str:
    """Return a 10-digit US number, or empty when the value is not one."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        return ""
    return digits


def format_phone(digits: str) -> str:
    if len(digits) == 10 and digits.isdigit():
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return digits


def address_usable(address: str | None) -> bool:
    value = (address or "").strip()
    if len(value) < 6 or len(value) > 300:
        return False
    if not re.search(r"\d", value) or not re.search(r"[A-Za-z]", value):
        return False
    return True


def has_work_terms(text: str | None) -> bool:
    return bool(WORK_RE.search(text or ""))


def llm_api_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip()


def compute_twilio_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    """Twilio's HMAC-SHA1 webhook signature (base64)."""
    payload = url
    for key in sorted(params):
        payload += key + (params[key] if params[key] is not None else "")
    digest = hmac.new(
        auth_token.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def twilio_signature_valid(
    url: str,
    params: dict[str, str],
    signature: str,
    auth_token: str,
) -> bool:
    if not signature or not auth_token:
        return False
    expected = compute_twilio_signature(url, params, auth_token)
    if len(expected) != len(signature):
        return False
    return hmac.compare_digest(expected, signature)


def parse_message(
    text: str,
    *,
    sender: str = "",
    subject: str = "",
    now: datetime | None = None,
) -> ParsedMessage:
    """Parse a message. Uses an LLM only when an API key is configured."""
    rules = parse_rules(text, sender=sender, subject=subject, now=now)
    if not (text or "").strip() or not llm_api_key():
        return rules
    try:
        llm = call_openai_json(_LLM_SYSTEM, _llm_user_prompt(text, sender, subject))
    except Exception:
        return rules
    if not isinstance(llm, dict):
        return rules
    return merge_llm(rules, llm)


def parse_rules(
    text: str,
    *,
    sender: str = "",
    subject: str = "",
    now: datetime | None = None,
) -> ParsedMessage:
    body = text or ""
    sender_name, sender_phone, sender_email = split_sender(sender)
    emails = find_emails(body)
    phones = find_phones(body)
    masked = _mask_contacts(body)
    address = extract_address(masked)
    name = extract_name(body) or sender_name
    phone = phones[0] if phones else sender_phone
    email = emails[0] if emails else sender_email
    # Blank the raw address span so "at 10 Main St" is not read as 10:00.
    requested = extract_when(_blank_addresses(masked), _as_local(now))
    description = extract_description(body, subject)
    return ParsedMessage(
        client_name=name,
        phone=phone,
        phone_display=format_phone(phone) if phone else "",
        email=email,
        address=address,
        requested_when=requested,
        description=description,
    )


def merge_llm(rules: ParsedMessage, llm: dict) -> ParsedMessage:
    """Prefer a non-empty, plausible LLM field, and keep the rule result otherwise."""
    name = _llm_str(llm, "client_name", 120)
    if _plausible_name(name):
        rules.client_name = _tidy_name(name)
    phone = normalize_phone(_llm_str(llm, "phone", 40))
    if phone:
        rules.phone = phone
        rules.phone_display = format_phone(phone)
    email = _llm_str(llm, "email", 200).lower()
    if email and _EMAIL.fullmatch(email):
        rules.email = email
    address = _llm_str(llm, "address", 300)
    if address_usable(address) and not normalize_phone(address):
        rules.address = re.sub(r"\s+", " ", address).strip(" ,")
    requested = _llm_str(llm, "requested_when", 200)
    if requested:
        rules.requested_when = requested
    description = _llm_str(llm, "description", 2000)
    if description:
        rules.description = description
    return rules


def call_openai_json(system: str, user: str) -> dict:
    """POST a chat completion and parse the JSON object it returns."""
    key = llm_api_key()
    if not key:
        raise RuntimeError("No LLM API key is configured.")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"LLM request failed with HTTP {exc.code}") from exc
    content = body["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM response was not a JSON object.")
    return parsed


def split_sender(sender: str | None) -> tuple[str, str, str]:
    """Return (name, phone digits, email) from a From header or Twilio From."""
    raw = (sender or "").strip()
    if not raw:
        return "", "", ""
    name = ""
    email = ""
    phone = ""
    bracket = re.search(r"<([^>]+)>", raw)
    if bracket:
        inner = bracket.group(1).strip()
        if "@" in inner:
            email = inner
        else:
            phone = normalize_phone(inner)
        name = raw[: bracket.start()].strip().strip('"').strip("'")
    elif "@" in raw:
        found = _EMAIL.search(raw)
        if found:
            email = found.group(0)
            name = raw[: found.start()].strip().strip('"').strip("'")
    else:
        phone = normalize_phone(raw)
    if not _plausible_name(name):
        name = ""
    else:
        name = _tidy_name(name)
    if email and not _EMAIL.fullmatch(email):
        email = ""
    return name, phone, email.lower() if email else ""


def find_emails(text: str) -> list[str]:
    found = []
    for match in _EMAIL.findall(text or ""):
        lowered = match.lower()
        if lowered not in found:
            found.append(lowered)
    return found


def find_phones(text: str) -> list[str]:
    found = []
    for match in _PHONE.finditer(text or ""):
        digits = normalize_phone("".join(match.groups()))
        if digits and digits not in found:
            found.append(digits)
    return found


def extract_address(text: str) -> str:
    """Return the first US street address in text, or empty."""
    for suffix in _WITH_SUFFIX.finditer(text or ""):
        street = suffix.group("street")
        if not _street_plausible(street):
            continue
        city, state, zipcode = _read_tail((text or "")[suffix.end() :])
        return _format_address(street, city, state, zipcode)
    plain = _NO_SUFFIX.search(text or "")
    if (
        plain
        and _street_plausible(f"{plain.group('num')} {plain.group('street')}")
        and _valid_city(plain.group("city"))
        and plain.group("state").upper() in _STATES
    ):
        street = f"{plain.group('num')} {plain.group('street')}"
        return _format_address(
            street,
            plain.group("city"),
            plain.group("state"),
            plain.group("zip") or "",
        )
    loose = _NO_SUFFIX_LOOSE.search(text or "")
    if (
        loose
        and _street_plausible(f"{loose.group('num')} {loose.group('street')}")
        and _valid_city(loose.group("city"))
        and loose.group("state").upper() in _STATES
    ):
        return _format_address(
            f"{loose.group('num')} {loose.group('street')}",
            loose.group("city"),
            loose.group("state"),
            loose.group("zip") or "",
        )
    return ""


def _blank_addresses(text: str) -> str:
    """Replace address matches with spaces, preserving the rest of the string."""

    def _spaces(match: re.Match[str]) -> str:
        return " " * len(match.group(0))

    blanked = _WITH_SUFFIX.sub(_spaces, text or "")
    blanked = _NO_SUFFIX.sub(_spaces, blanked)
    return _NO_SUFFIX_LOOSE.sub(_spaces, blanked)


def _street_plausible(street: str) -> bool:
    """Reject phrases like '2 days on the road' that merely end in a suffix."""
    rest = re.sub(r"^\d+\s+", "", street.strip())
    rest = re.sub(
        rf"\s+{_UNIT}\s*[A-Za-z0-9-]+$",
        "",
        rest,
        flags=re.IGNORECASE,
    )
    words = re.findall(r"[A-Za-z0-9']+", rest)
    content = []
    for word in words:
        lower = word.lower().rstrip(".")
        if lower in _STREET_NOISE or lower in _SHORT_DIRECTIONS:
            continue
        if re.fullmatch(_SUFFIX, lower, flags=re.IGNORECASE):
            continue
        content.append(word)
    return bool(content)


def extract_name(text: str) -> str:
    for match in _NAME_INTRO.finditer(text or ""):
        candidate = match.group(1).strip(" .,")
        if _plausible_name(candidate):
            return _tidy_name(candidate)
    return ""


def extract_when(text: str, now: datetime) -> str:
    """Resolve a requested date and/or time into one display string."""
    local = _as_local(now)
    date_value, _raw_date, year_explicit = _extract_date(text or "", local)
    clock, _raw_time = _extract_clock(text or "")
    if date_value is None and clock is None:
        return ""
    if date_value is not None and not year_explicit:
        # A past month/day with no year is almost always the next occurrence.
        candidate = datetime(
            date_value.year,
            date_value.month,
            date_value.day,
            tzinfo=local.tzinfo,
        )
        if candidate.date() < local.date() - timedelta(days=1):
            try:
                date_value = date_value.replace(year=date_value.year + 1)
            except ValueError:
                date_value = date_value.replace(year=date_value.year + 1, day=28)
    if date_value is not None and clock is not None:
        combined = date_value.replace(hour=clock[0], minute=clock[1])
        return _format_when(combined, with_time=True)
    if date_value is not None:
        return _format_when(date_value, with_time=False)
    assert clock is not None
    hour, minute = clock
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"


def extract_description(text: str, subject: str = "") -> str:
    sentences = _sentences(text or "")
    chosen = [sentence.strip() for sentence in sentences if WORK_RE.search(sentence)]
    if chosen:
        description = " ".join(chosen)
    else:
        description = _cleaned_fallback(text or "")
    description = re.sub(r"\s+", " ", description).strip(" -,:;")
    subject = (subject or "").strip()
    if subject and _useful_subject(subject) and not _subject_covered(subject, description):
        description = f"{subject}: {description}".strip() if description else subject
    return description[:2000]


def _llm_user_prompt(text: str, sender: str, subject: str) -> str:
    return (
        f"Sender: {sender or ''}\n"
        f"Subject: {subject or ''}\n"
        f"Message:\n{text or ''}"
    )


def _llm_str(payload: dict, key: str, limit: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _mask_contacts(text: str) -> str:
    masked = _EMAIL.sub(lambda match: " " * len(match.group(0)), text or "")

    def _phone_spaces(match: re.Match[str]) -> str:
        return " " * len(match.group(0))

    return _PHONE.sub(_phone_spaces, masked)


def _read_tail(rest: str) -> tuple[str, str, str]:
    """Parse a city / state / ZIP tail that follows a street."""
    patterns = (
        (r"^,\s*([A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){0,2})\s*,\s*([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\b", "csz"),
        (r"^,\s*([A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){0,2})\s+([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\b", "csz"),
        (r"^\s+([A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){0,2})\s*,\s*([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\b", "csz"),
        (r"^\s+([A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){0,2})\s+([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\b", "csz"),
        (r"^,\s*([A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){0,2})\s*,\s*([A-Za-z]{2})\b", "cs"),
        (r"^,\s*([A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){0,2})\s+([A-Za-z]{2})\b", "cs"),
        (r"^\s+([A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){0,2})\s+([A-Za-z]{2})\b", "cs"),
        (r"^[,\s]+([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\b", "sz"),
        (r"^[,\s]+(\d{5}(?:-\d{4})?)\b", "z"),
        (r"^,\s*([A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){0,2})\b", "c"),
    )
    for pattern, kind in patterns:
        match = re.match(pattern, rest, flags=re.IGNORECASE)
        if not match:
            continue
        if kind == "csz":
            city, state, zipcode = match.group(1), match.group(2), match.group(3)
        elif kind == "cs":
            city, state, zipcode = match.group(1), match.group(2), ""
        elif kind == "sz":
            city, state, zipcode = "", match.group(1), match.group(2)
        elif kind == "z":
            city, state, zipcode = "", "", match.group(1)
        else:
            city, state, zipcode = match.group(1), "", ""
        if city and not _valid_city(city):
            continue
        if state and state.upper() not in _STATES:
            continue
        return city, state, zipcode
    return "", "", ""


def _valid_city(city: str) -> bool:
    words = re.findall(r"[A-Za-z']+", city)
    if not words or len(words) > 3:
        return False
    return all(word.lower() not in _CITY_STOP for word in words)


def _format_address(street: str, city: str, state: str, zipcode: str) -> str:
    line = _tidy_street(street)
    city = _tidy_place(city) if city else ""
    state = state.upper() if state else ""
    zipcode = zipcode or ""
    if city and state:
        line += f", {city}, {state}"
    elif city:
        line += f", {city}"
    elif state:
        line += f", {state}"
    if zipcode:
        line += f" {zipcode}"
    return re.sub(r"\s+", " ", line).strip(" ,")


def _tidy_street(street: str) -> str:
    street = re.sub(r"\s+", " ", street).strip(" ,.")
    if not street or not (street.islower() or street.isupper()):
        return street
    words = []
    for word in street.split():
        bare = word.strip(".")
        lower = bare.lower()
        if lower in _SHORT_DIRECTIONS:
            words.append(lower.upper() if len(lower) <= 2 else lower.capitalize())
        elif re.fullmatch(r"\d+(st|nd|rd|th)", lower):
            words.append(lower)
        else:
            words.append(lower.capitalize())
    return " ".join(words)


def _tidy_place(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" ,.")
    if not value or not (value.islower() or value.isupper()):
        return value
    return " ".join(part.capitalize() for part in value.split())


def _plausible_name(name: str | None) -> bool:
    raw = (name or "").strip().strip('"').strip("'")
    if not raw or any(char.isdigit() for char in raw):
        return False
    words = raw.split()
    if not 1 <= len(words) <= 3:
        return False
    for word in words:
        if word.lower().strip(".") in _NAME_STOP:
            return False
        if not re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", word):
            return False
    return True


def _tidy_name(name: str) -> str:
    parts = []
    for word in name.split():
        if word.isupper() or word.islower():
            parts.append(word.capitalize())
        else:
            parts.append(word)
    return " ".join(parts)


def _as_local(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(LOCAL_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=LOCAL_TZ)
    return now.astimezone(LOCAL_TZ)


def _extract_date(text: str, today: datetime) -> tuple[datetime | None, str, bool]:
    checks = (
        (re.compile(r"\bday after tomorrow\b", re.IGNORECASE), "day_after"),
        (re.compile(r"\btomorrow\b", re.IGNORECASE), "tomorrow"),
        (re.compile(r"\btoday\b", re.IGNORECASE), "today"),
        (re.compile(rf"\b(next|this)\s+({_WEEKDAY})\b", re.IGNORECASE), "weekday_mod"),
        (re.compile(rf"\b({_WEEKDAY})\b", re.IGNORECASE), "weekday"),
        (
            re.compile(
                rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b",
                re.IGNORECASE,
            ),
            "monthday",
        ),
        (re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b"), "numeric_full"),
        (re.compile(r"\b(\d{1,2})[/-](\d{1,2})\b"), "numeric"),
        (re.compile(r"\bin\s+(\d{1,3})\s+days?\b", re.IGNORECASE), "in_days"),
        (re.compile(r"\bnext week\b", re.IGNORECASE), "next_week"),
    )
    for pattern, kind in checks:
        match = pattern.search(text)
        if not match:
            continue
        year_explicit = False
        if kind == "today":
            found = today
        elif kind == "tomorrow":
            found = today + timedelta(days=1)
        elif kind == "day_after":
            found = today + timedelta(days=2)
        elif kind == "next_week":
            found = today + timedelta(days=7)
        elif kind == "in_days":
            found = today + timedelta(days=int(match.group(1)))
        elif kind == "weekday":
            found = today + timedelta(days=_weekday_delta(match.group(1), today, force_next=False))
        elif kind == "weekday_mod":
            force_next = match.group(1).lower() == "next"
            found = today + timedelta(
                days=_weekday_delta(match.group(2), today, force_next=force_next)
            )
        elif kind == "monthday":
            month = _MONTHS[match.group(1).lower().rstrip(".")]
            day = int(match.group(2))
            if match.group(3):
                year = int(match.group(3))
                year_explicit = True
            else:
                year = today.year
            if not _valid_day(year, month, day):
                continue
            found = today.replace(year=year, month=month, day=day)
        elif kind == "numeric_full":
            month, day, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if year < 100:
                year += 2000
            year_explicit = True
            if not _valid_day(year, month, day):
                continue
            found = today.replace(year=year, month=month, day=day)
        else:
            month, day = int(match.group(1)), int(match.group(2))
            if not _valid_day(today.year, month, day):
                continue
            found = today.replace(month=month, day=day)
        return found, match.group(0), year_explicit
    return None, "", False


def _weekday_delta(name: str, today: datetime, *, force_next: bool) -> int:
    """Days until weekday. "next" skips today when today is that weekday."""
    target = _WEEKDAYS[name.lower()]
    delta = (target - today.weekday()) % 7
    if force_next and delta == 0:
        return 7
    return delta


def _valid_day(year: int, month: int, day: int) -> bool:
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return False
    try:
        datetime(year, month, day)
    except ValueError:
        return False
    return True


def _extract_clock(text: str) -> tuple[tuple[int, int] | None, str]:
    explicit = re.search(
        r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if explicit:
        hour = int(explicit.group(1))
        minute = int(explicit.group(2) or 0)
        pm = explicit.group(3).lower().startswith("p")
        clock = _to_24(hour, minute, pm=pm, hinted=True)
        if clock:
            return clock, explicit.group(0)
    implied = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\b", text, flags=re.IGNORECASE)
    if implied:
        hour = int(implied.group(1))
        minute = int(implied.group(2) or 0)
        if hour > 12:
            clock = _to_24(hour, minute, pm=False, hinted=False)
        else:
            # Daytime jobs: 8–11 read as morning, 12 and 1–7 as afternoon.
            pm = hour == 12 or hour <= 7
            clock = _to_24(hour, minute, pm=pm, hinted=True)
        if clock:
            return clock, implied.group(0)
    word = re.search(r"\b(morning|afternoon|evening|noon|midday)\b", text, flags=re.IGNORECASE)
    if word:
        label = word.group(1).lower()
        clocks = {
            "morning": (9, 0),
            "afternoon": (13, 0),
            "evening": (17, 0),
            "noon": (12, 0),
            "midday": (12, 0),
        }
        return clocks[label], word.group(0)
    return None, ""


def _to_24(hour: int, minute: int, *, pm: bool, hinted: bool) -> tuple[int, int] | None:
    if not 0 <= minute <= 59:
        return None
    if not hinted and 0 <= hour <= 23:
        return hour, minute
    if not 1 <= hour <= 12:
        if 0 <= hour <= 23:
            return hour, minute
        return None
    if hour == 12:
        hour = 0
    if pm:
        hour += 12
    return hour, minute


def _format_when(value: datetime, *, with_time: bool) -> str:
    date = f"{value.strftime('%a')}, {value.strftime('%b')} {value.day}, {value.year}"
    if not with_time:
        return date
    hour = value.strftime("%I").lstrip("0")
    return f"{date}, {hour}:{value.strftime('%M %p')}"


def _sentences(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _cleaned_fallback(text: str) -> str:
    cleaned = _EMAIL.sub(" ", text)
    cleaned = _PHONE.sub(" ", cleaned)
    cleaned = re.sub(
        r"\b(?:my name is|this is|i am|i'm|it's|its)\s+[A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){0,2}",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;-")
    words = re.findall(r"[A-Za-z']+", cleaned)
    filler = {
        "hi", "hello", "hey", "thanks", "thank", "you", "please", "call", "me",
        "at", "the", "a", "an", "and", "to", "for", "of", "my", "is", "address",
    }
    meaningful = [word for word in words if word.lower() not in filler]
    if len(meaningful) >= 2:
        return cleaned
    return ""


def _useful_subject(subject: str) -> bool:
    lowered = subject.strip().lower()
    lowered = re.sub(r"^(re|fw|fwd)\s*:\s*", "", lowered).strip()
    return bool(lowered) and lowered not in _GENERIC_SUBJECTS and len(lowered) >= 3


def _subject_covered(subject: str, description: str) -> bool:
    words = [
        word.lower()
        for word in re.findall(r"[A-Za-z]{4,}", subject)
        if word.lower() not in {"this", "that", "your", "from", "with", "about"}
    ]
    if not words:
        return False
    haystack = description.lower()
    return any(word in haystack for word in words)
