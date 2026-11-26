# Steady Drywall job board

Day-to-day jobs, materials, and invoices for Steady Drywall LLC, a small drywall crew near Missoula. Max checks it from his phone. The same admin password protects the board, the inbox, logistics, messages, and invoices. The password check is unchanged.

Workers can still report a finished job with three fields: address, description, and tech name. That becomes an invoice (`INV-0001`, `INV-0002`, …). Max can set the amount, mark it Sent or Paid, open a prefilled email, print it, download a PDF, or export CSV.

Texts, inbound email, a JSON post, or a pasted note still open an invoice when a street address is known. The technician on that invoice stays `Inbound`. The same message is logged in the inbox for review, and it is attached to the board job already at that street. A new lead is opened only when the street is new. Crew is never assigned automatically. Pay is a price for the job. The board stores calendar dates and a dollar amount. It does not store hours.

This copy is prepared for hosting on [Render](https://render.com) with a Postgres database. Without `DATABASE_URL` it uses SQLite. New tables are added with `CREATE TABLE IF NOT EXISTS`, and `job_id` is added to existing invoice and message rows only when that column is missing. Existing invoice rows are left in place.

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
| `ADMIN_PASSWORD` | unset (off) | If set, Home, Inbox, Jobs, Logistics, Messages, the invoice list, invoice pages, CSV export, and PDF download require this password. The intake form stays open. The check is the same password comparison as before. |
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

- Home (counts, this week, deliveries, unpaid, unreviewed): `/admin/home`
- Inbox: `/admin/activity`
- Job board: `/admin/jobs`
- One job: `/admin/jobs/<id>`
- New job: `/admin/jobs/new`
- Logistics: `/admin/logistics`
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
- Jobs API: `GET` and `POST /api/jobs`, `GET` and `PATCH /api/jobs/<id>`
- Material orders API: `GET` and `POST /api/material-orders`, `GET` and `PATCH /api/material-orders/<id>`
- Inbox API: `GET` and `POST /api/activity`

## Inbound messages

Every accepted message is stored: source, sender, raw text, parsed fields, received time, and either the job it opened or why it did not. The same sender is remembered as a contact. A later text from that number or email fills in a missing address, name, phone, or email. If a follow-up is only the street address, it is attached to the open requests from that sender and one job is created.

Jobs opened this way use the existing invoice record. The technician field is `Inbound`. The description includes the client, phone, email, and requested date/time when those were found. No hours field is added. The message is also written to the inbox, and the invoice is linked to the board job at that street. A new lead is created only when no job is at that street yet, with the crew left blank.

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

## Job board

Open **Jobs**. Each card is one job:

| Field | Notes |
| --- | --- |
| Customer / GC | Free text. The form suggests Five Valleys, Bushco Construction, MJL, IJD, and All Tucker Drywall, plus any customer already saved. |
| Job name | Short name, such as "Lolo hang". |
| Street and town | Town is separate so a Missoula job is not mixed up with one in Lolo. |
| Status | Lead, Scheduled, Hang, Tape/Finish, Punch, Done, Invoiced, Paid. Any status can be set from the card. The list is a pipeline, not a lock. |
| Crew | Free text, such as "Jordan, Sam". A crew list on the job page only remembers names. Nothing picks a crew. |
| Start and target finish | Calendar dates. |
| Price | Dollar amount for the whole job. |
| Notes | Whatever Max wants on the card. |

Filter with the status chips, the customer menu, and **This week**. This week is Monday through Sunday in America/Denver. A job is included when its start, its target finish, or the span between them overlaps that week.

Saving a new job at a street that is already on the board opens the existing job instead of a second card. Check "Save as a separate job anyway" for a different unit or a second visit. `Apt 2` and `Apt 4` are different streets. `120 Ridge Rd` and `120 Ridge Road` are the same street.

The job page lists material orders, equipment or dumpster needs, linked invoices, intake messages, and inbox rows.

**Logistics** lists open material orders, deliveries that have a date, and equipment or dumpsters that are not done. Earliest date first. Rows with no date sit at the bottom. Stocked materials and finished needs stay on the job and drop off this list.

**Home** shows the unreviewed count, a count for every status, the jobs that land this week, material deliveries dated today through seven days out, and invoices that are not paid.

## Inbox

Everything that comes in is stored for Max to review. A new row is **Unreviewed** until he taps **Mark reviewed**. Reviewing a row does not send a text, an email, or any other message.

The feed includes:

- SMS, email, and `POST /api/intake/messages`
- Notes pasted on the Messages page
- The public job-report form
- `POST /api/activity` from the assistant
- Job and material-order changes made through the JSON API (`Job created`, `Status changed`, `Delivery scheduled`, and the other order updates)

Each row stores the source, sender, received time, summary, optional longer text, optional address, the linked job when there is one, and the reviewed flag. The unreviewed count is on Home and in the nav.

An item with no `job_id` is linked to the newest matching job when the street matches. `Road` and `Rd` match. A different apartment or suite does not. A town on both sides has to agree. An explicit `job_id` is used as given.

The same `external_id` posted twice returns the original row and does not open a second one. Sending `"reviewed": true` on create does not skip review.

## Data model

New tables are created on boot. `invoices.job_id` and `messages.job_id` are added when missing. Invoice numbers, amounts, and statuses already in the database stay as they are.

**jobs** — `customer`, `job_name`, `street_address`, `town`, `status`, `crew`, `start_date`, `target_finish`, `amount`, `notes`, `created_at`, `updated_at`.

**material_orders** — `job_id`, `item`, `qty`, `supplier`, `order_date`, `delivery_date`, `status` (`ordered`, `delivered`, `stocked`), `notes`.

**job_needs** — equipment or dumpster rows: `kind`, `description`, `needed_date`, `status` (`needed`, `scheduled`, `on_site`, `done`), `notes`.

**crew_members** — names Max keeps on a list. A job's crew is still the text field on that job.

**activity** — `source`, `sender`, `summary`, `body`, `address`, `reviewed` (0 or 1), `job_id`, `message_id`, `external_id`, `received_at`, `reviewed_at`.

**invoices.job_id** and **messages.job_id** point at a board job. An invoice can stay unlinked. Inbound messages that create an invoice set both links. A later message at the same street reuses the board job and still creates its own invoice. The invoice technician stays `Inbound`.

Dates are `YYYY-MM-DD`. Amounts are decimal strings such as `4200.00`. There are no hour, clock, or hourly-rate columns. A request that includes `hours` or `time_in` is rejected.

## JSON API

The jobs, material-order, and activity endpoints use the same secret as intake: `X-Intake-Token`, `Authorization: Bearer`, or `?token=`. A logged-in admin session is also accepted. Until `INTAKE_WEBHOOK_TOKEN` is set they answer `503`. A wrong token answers `403`.

`PATCH` accepts the same JSON shape as `PUT`. Send only the fields to change. Dates must be `YYYY-MM-DD`.

Create a job:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $INTAKE_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"customer":"MJL","job_name":"Lolo hang","street_address":"120 Ridge Rd","town":"Lolo","status":"scheduled","crew":"Jordan, Sam","start_date":"2026-11-24","target_finish":"2026-11-26","amount":"4200"}' \
  https://job-invoices-7p8s.onrender.com/api/jobs
```

List jobs: `GET /api/jobs?status=hang&customer=MJL&week=1`.

Update a job: `PATCH /api/jobs/1` with `{"status":"punch"}`. That writes an unreviewed inbox row.

One job, including orders, needs, invoices, and inbox rows: `GET /api/jobs/1`.

Order material:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $INTAKE_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"job_id":1,"item":"1/2in board","qty":"40 sheets","supplier":"ABC Supply","order_date":"2026-11-24","delivery_date":"2026-11-26"}' \
  https://job-invoices-7p8s.onrender.com/api/material-orders
```

A delivery date is logged as `Delivery scheduled`. List open orders with `GET /api/material-orders?open=1`. Update with `PATCH /api/material-orders/1` and `{"status":"delivered"}`.

Post a note for review:

```bash
curl -sS -X POST \
  -H "X-Intake-Token: $INTAKE_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"summary":"GC asked to move the Lolo hang to Thursday","sender":"assistant","address":"120 Ridge Rd, Lolo"}' \
  https://job-invoices-7p8s.onrender.com/api/activity
```

The address is matched to a job when `job_id` is omitted. `GET /api/activity?reviewed=0` lists what is still open. The response is JSON only. The app does not text or email anyone because a row was saved.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

On the invoice page you can also use the browser’s Print dialog to save a PDF.

## Typical flow

1. Open **Home**. The inbox count is the first thing on the page. Tap it and mark each row reviewed after you have read it.
2. Add a job from **Jobs**, or let a text with a street address attach itself to the job already at that address.
3. On the job, set the status, type the crew, and add board, mud, tape, or a dumpster. **Logistics** lists what is still open, earliest date first.
4. When the work is billed, open an invoice from the job or link an existing invoice. Set the amount, use **Send email** to open a prefilled message in your mail app, then **Mark sent**.
5. When payment arrives, **Mark paid** on the invoice and set the job to Paid.
6. Export invoices with **Export CSV**, or download / print a single invoice as a PDF.

A tech can still submit the public form. That creates a draft invoice and an unreviewed inbox row. It opens a board job only when that street is already on the board, and then it links the invoice to it.

## Notes

- There is no SMTP, and the inbox does not send messages. **Send email** only opens a `mailto:` link with the invoice summary already filled in.
- Admin protection is on whenever `ADMIN_PASSWORD` is set. The intake form stays public so techs can still submit jobs.
- PDF generation uses ReportLab, which installs as a Python wheel and does not need extra system packages on Render's native Python runtime.
