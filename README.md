# Job intake and invoicing

A minimal web app for reporting completed jobs and turning them into invoices.

Workers fill in three fields — job address, description of work, and tech name. Each submission is stored and becomes an invoice with an auto-incrementing number (`INV-0001`, `INV-0002`, …) and a date. Max can then open the invoice, enter the amount, mark it Sent or Paid, email a summary, print it, download a PDF, or export everything to CSV.

Texts, inbound email, a JSON post, or a note pasted on the Messages page can open the same kind of job. The app reads the client name, phone, email, street address, requested date and time, and what they want done. Crew pay is by the job: a requested date and time is stored on the job, and there is no hourly time tracking.

There is no crew calendar, parts list, or user accounts. The admin password is unchanged.

This copy is prepared for hosting on [Render](https://render.com) with a Postgres database. The original local SQLite app is unchanged.

## Requirements

- Python 3.11 or 3.12 (3.12 is pinned for Render in `.python-version`)
- A virtualenv is recommended for local use

## How to run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PORT=8080 python3 app.py
```

Or with the production server:

```bash
PORT=8080 gunicorn app:app --bind 0.0.0.0:$PORT
```

Without `DATABASE_URL`, data is stored in `invoices.db` in this directory (SQLite).

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | Bind address when running `python3 app.py` |
| `PORT` | `8080` | Port (`gunicorn` reads `$PORT` from the start command) |
| `SECRET_KEY` | a dev default | Flask session signing key — required in production (Render generates one) |
| `ADMIN_PASSWORD` | unset (off) | If set, the invoice list, invoice pages, Messages inbox, CSV export, and PDF download require this password. The intake form stays open. Messaging uses this same password. |
| `DATABASE_URL` | unset (SQLite) | Postgres connection URL. `postgres://` is accepted and normalized to `postgresql://`. |
| `BUSINESS_NAME` | `Max Stubbs` | Name shown on invoices |
| `INTAKE_WEBHOOK_TOKEN` | unset | Shared secret for the SMS, email, and JSON intake endpoints. The app boots without it; those endpoints stay closed until it is set. Put it in the query string (`?token=`) or send `X-Intake-Token` / `Authorization: Bearer`. |
| `TWILIO_AUTH_TOKEN` | unset | When set, `POST /webhooks/sms` must also include a valid Twilio `X-Twilio-Signature`. Leave it unset and the shared intake token is enough. |
| `OPENAI_API_KEY` | unset | Optional. When set, parsing asks an OpenAI-compatible chat model and falls back to the built-in rules if the call fails. `OPENAI_MODEL` defaults to `gpt-4o-mini`. `OPENAI_BASE_URL` defaults to `https://api.openai.com/v1`. |
| `SQLITE_PATH` | `invoices.db` in this folder | Optional local override. Ignored when `DATABASE_URL` is set. |

Example with a password and SQLite:

```bash
ADMIN_PASSWORD='choose-a-password' SECRET_KEY='a-long-random-string' PORT=8080 gunicorn app:app --bind 0.0.0.0:$PORT
```

## Deploy on Render

1. Push this folder to a GitHub, GitLab, or Bitbucket repository.
2. Open the Blueprint at `https://dashboard.render.com/blueprint/new?repo=<YOUR_REPO_HTTPS_URL>`.
3. When prompted, set `ADMIN_PASSWORD` (it is not stored in this repo). `SECRET_KEY` and `DATABASE_URL` are filled in automatically.
4. Apply the Blueprint. Render creates a free web service and a free Postgres database from `render.yaml`.
5. After this repo gains intake settings, sync the Blueprint (or add them on the service by hand). `INTAKE_WEBHOOK_TOKEN`, `TWILIO_AUTH_TOKEN`, and `OPENAI_API_KEY` are `sync: false`, so a sync will not change `ADMIN_PASSWORD` or any value already saved in the dashboard. New keys show up empty until you fill them in. The service still boots if they are empty.

The start command is `gunicorn app:app --bind 0.0.0.0:$PORT`.

**Free-tier limits to know about** (from [Render's free-instance docs](https://render.com/docs/free)):

- Free web services spin down after 15 minutes without traffic and take about a minute to wake up. They also share 750 free instance hours per workspace each month.
- Free Postgres is limited to 1 GB, one active free database per workspace, and **expires 30 days after creation**. After expiry there is a 14-day grace period to upgrade to a paid plan; then the database and its data are deleted. Free Postgres has no backups and no managed connection pooling.

If this app will hold real invoices past a trial month, upgrade the database to a paid plan before day 30.

## URLs

Production host: `https://job-invoices-7p8s.onrender.com`

- Job intake form: `/`
- Admin invoice list: `/admin`
- Messages inbox: `/admin/messages`
- One message (correct it or create a job): `/admin/messages/<id>`
- One invoice (printable): `/invoice/<id>`
- CSV export of all invoices: `/export.csv`
- PDF download of one invoice: `/invoice/<id>/pdf`
- Send (opens a prefilled `mailto:` draft; nothing is sent automatically): `/invoice/<id>/send`
- Health check: `/healthz`
- Twilio-style SMS webhook: `POST /webhooks/sms`
- Inbound email webhook: `POST /webhooks/email`
- JSON intake: `POST /api/intake/messages`

## Inbound messages

Every accepted message is stored: source, sender, raw text, parsed fields, received time, and either the job it opened or why it did not. The same sender is remembered as a contact. A later text from that number or email fills in a missing address, name, phone, or email. If a follow-up is only the street address, it is attached to the open requests from that sender and one job is created.

Jobs opened this way use the existing invoice record. The technician field is `Inbound`. The description includes the client, phone, email, and requested date/time when those were found. No hours field is added.

Parsing is rule-based (US phone numbers, street addresses, and dates). Set `OPENAI_API_KEY` if you want a model to take a pass; a failed or empty model response still uses the rules.

### Token

Generate a long random token and set it as `INTAKE_WEBHOOK_TOKEN` on the Render service (the Blueprint declares it with `sync: false`, so it is not stored in git):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Send it as `?token=...`, the `X-Intake-Token` header, or `Authorization: Bearer ...`. Until the variable is set, the three endpoints above answer `503` and store nothing. A wrong token answers `403`.

The Messages page does not use this token. It uses the existing admin password.

### Twilio SMS

1. In the [Twilio console](https://console.twilio.com), buy or choose a phone number.
2. Under that number’s Messaging settings, set “A message comes in” to **Webhook**, method **HTTP POST**, and this URL (use the token from the step above):

   `https://job-invoices-7p8s.onrender.com/webhooks/sms?token=YOUR_INTAKE_WEBHOOK_TOKEN`

3. On the Render service, set `TWILIO_AUTH_TOKEN` to the account Auth Token (not the Account SID). When that variable is set, the app checks Twilio’s `X-Twilio-Signature` against the exact URL Twilio called, including the `token` query parameter. Leave `TWILIO_AUTH_TOKEN` unset only for a private test; a public number should set it.
4. Text the number. A successful post returns empty TwiML so Twilio does not retry. The message shows up under **Messages**, and a job is opened when a street address is known.

Twilio posts form fields `From`, `Body`, and `MessageSid`. A repeated `MessageSid` does not open a second job.

### Inbound email

Point SendGrid Inbound Parse, Mailgun routes, or Postmark inbound at:

`https://job-invoices-7p8s.onrender.com/webhooks/email?token=YOUR_INTAKE_WEBHOOK_TOKEN`

Use the provider’s form post or JSON. The app reads any of these fields:

| Meaning | Accepted names |
| --- | --- |
| Sender | `from`, `sender`, `FromName` plus `From` |
| Subject | `subject` |
| Body | `stripped-text`, `text`, `body-plain`, `plain`, `TextBody`, `body` |
| HTML fallback | `html`, `HtmlBody` |
| Id (dedupes) | `Message-Id`, `MessageID` |

The provider’s own MX / inbound-domain setup is still required; this app only receives the webhook.

### JSON

```bash
curl -sS -X POST \
  -H "Authorization: Bearer YOUR_INTAKE_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"from":"Sarah <sarah@example.com>","text":"Need the ceiling patched at 10 Main St, Denver CO tomorrow at 2pm."}' \
  https://job-invoices-7p8s.onrender.com/api/intake/messages
```

`from`, `text` (or `body`), `subject`, and `external_id` are optional except `text`. An admin who is already logged in can call this endpoint without the token.

### Paste a message

Log in and open `/admin/messages`. Paste the text, and optionally who it was from. On a phone, each message is a card with the parsed fields, a link to the job, and **Review** to correct the fields and create or recreate a job. Recreating leaves the earlier invoice in the list.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

On the invoice page you can also use the browser’s Print dialog to save a PDF.

## Typical flow

1. A tech submits the intake form. They get a confirmation with the invoice number. The invoice starts as **Draft**, with no amount.
2. Open **Invoices**, click the invoice, enter the dollar amount, and save it.
3. Use **Send email** to open a prefilled message in your mail app, then **Mark sent**.
4. When payment arrives, **Mark paid**.
5. Export everything with **Export CSV**, or download / print a single invoice as a PDF.

## Notes

- There is no SMTP. **Send email** only opens a `mailto:` link with the invoice summary already filled in.
- Admin protection is on whenever `ADMIN_PASSWORD` is set. The intake form stays public so techs can still submit jobs.
- PDF generation uses ReportLab, which installs as a Python wheel and does not need extra system packages on Render's native Python runtime.
