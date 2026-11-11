# GM League Season 4

A Flask web application for running the **GM League Season 4** inter-college sports league:
player and team-owner registration, a live player auction, squad display, an admin dashboard
with Excel exports, transactional email, and a live match scorecard.

## Features

- **Registration** – players register per sport (`/register_player/<sport>`) and team owners register
  via `/team_owner_registration`; confirmation emails are sent automatically.
- **Live auction** – `/bidding/<sport>/<gender>` with Sold / Unsold / Undo actions, per-sport franchise
  wallets, bid increments and Indian-rupee formatting. Squads are shown at `/auction` and `/squads`.
- **Admin dashboard** – `/admin/login`, `/admin/dashboard`: edit/delete records, resend emails,
  college analytics (GMU, GMIT, Others/Foreign) and `.xlsx` exports.
- **Live scorecard** – a self-contained blueprint mounted at `/scores` (`scorecard/`) with per-sport
  scoring rules, a scorer console, public match pages and standings.
- **Email** – SMTP, Resend, Brevo or a Gmail webhook (see `mailer.py`).

Supported sports: Football, Basketball, Kabaddi, Badminton.

## Tech stack

Python 3.12, Flask, Flask-SQLAlchemy, Flask-Migrate, XlsxWriter, Gunicorn.
SQLite locally, PostgreSQL (e.g. Supabase) in production. Deployable to Render, Vercel or Netlify.

## Getting started

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit the values
python app.py                 # or: gunicorn --timeout 120 app:app
```

Key environment variables (see `.env.example` for the full list):

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Flask session signing key – set a long random value |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH` | Admin login |
| `DATABASE_URL` | SQLite or PostgreSQL connection string |
| `MAIL_*`, `RESEND_API_KEY`, `BREVO_API_KEY` | Email delivery |

> Always set `ADMIN_PASSWORD` (or `ADMIN_PASSWORD_HASH`) explicitly in every environment, and never
> commit real credentials.

## Project structure

```
app.py            Main Flask app: models, routes, auction logic, exports
mailer.py         Email delivery (SMTP / HTTP APIs)
pinger.py         Keep-alive pinger for Render's free tier
scorecard/        Live scorecard blueprint (/scores)
templates/        Jinja templates (public, admin, emails)
static/           Images, CSS and JS
api/, netlify/    Serverless entry points (Vercel / Netlify)
render.yaml       Render deployment blueprint
```
