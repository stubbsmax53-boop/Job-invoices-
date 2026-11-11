# Job intake and invoicing

A minimal web app for reporting completed jobs and turning them into invoices.

Workers fill in three fields — job address, description of work, and tech name. Each submission is stored and becomes an invoice with an auto-incrementing number (`INV-0001`, `INV-0002`, …) and a date. Max can then open the invoice, enter the amount, mark it Sent or Paid, email a summary, print it, download a PDF, or export everything to CSV.

There is no scheduling, parts list, or user accounts.

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
| `ADMIN_PASSWORD` | unset (off) | If set, the invoice list, invoice pages, CSV export, and PDF download require this password. The intake form stays open. |
| `DATABASE_URL` | unset (SQLite) | Postgres connection URL. `postgres://` is accepted and normalized to `postgresql://`. |
| `BUSINESS_NAME` | `Max Stubbs` | Name shown on invoices |

Example with a password and SQLite:

```bash
ADMIN_PASSWORD='choose-a-password' SECRET_KEY='a-long-random-string' PORT=8080 gunicorn app:app --bind 0.0.0.0:$PORT
```

## Deploy on Render

1. Push this folder to a GitHub, GitLab, or Bitbucket repository.
2. Open the Blueprint at `https://dashboard.render.com/blueprint/new?repo=<YOUR_REPO_HTTPS_URL>`.
3. When prompted, set `ADMIN_PASSWORD` (it is not stored in this repo). `SECRET_KEY` and `DATABASE_URL` are filled in automatically.
4. Apply the Blueprint. Render creates a free web service and a free Postgres database from `render.yaml`.

The start command is `gunicorn app:app --bind 0.0.0.0:$PORT`.

**Free-tier limits to know about** (from [Render's free-instance docs](https://render.com/docs/free)):

- Free web services spin down after 15 minutes without traffic and take about a minute to wake up. They also share 750 free instance hours per workspace each month.
- Free Postgres is limited to 1 GB, one active free database per workspace, and **expires 30 days after creation**. After expiry there is a 14-day grace period to upgrade to a paid plan; then the database and its data are deleted. Free Postgres has no backups and no managed connection pooling.

If this app will hold real invoices past a trial month, upgrade the database to a paid plan before day 30.

## URLs

- Job intake form: `/`
- Admin invoice list: `/admin`
- One invoice (printable): `/invoice/<id>`
- CSV export of all invoices: `/export.csv`
- PDF download of one invoice: `/invoice/<id>/pdf`
- Send (opens a prefilled `mailto:` draft; nothing is sent automatically): `/invoice/<id>/send`
- Health check: `/healthz`

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
