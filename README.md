# Finlify

A local-first personal finance dashboard. Dark, minimal interface, hand-drawn
canvas charts, no external services and no CDN dependencies. Your data lives in
a single SQLite file on your machine and never leaves it.

## Features

- **Stat cards** — net balance, income, expenses and savings rate, each with a
  sparkline and month-over-month deltas that count up on load.
- **Cash flow** — income vs expenses area chart with series toggles and a hover
  crosshair. 3M / 6M / 12M ranges.
- **Spending mix** — donut chart with a synced hover legend.
- **Daily pulse** — net flow per day for the last 30 days, split around a zero
  baseline.
- **Budgets** — per-category monthly limits with ok / warn / over states.
- **Insights & forecast** — an automatic read on your biggest category, biggest
  month-over-month jump, savings rate, spend projection and runway.
- **Categories** — create, rename, recolour, archive and delete. Renaming a
  category rewrites the transactions that use it, and deletion is blocked while a
  category is still in use so history is never silently orphaned.
- **Ledger** — paginated table with search, type and category filters and
  sorting. Click any row to edit, or use the bin to delete.
- **Backup** — export everything to a versioned JSON file, and import it back.
  Imports are idempotent, so re-importing the same file cannot duplicate money.

## Requirements

- Python 3.11 or newer.

## Run it

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn main:app --port 8000
```

Open <http://127.0.0.1:8000>. Interactive API docs are at
<http://127.0.0.1:8000/docs>.

The database and static assets resolve relative to the project directory, so the
server can be launched from anywhere:

```bash
python3 -m uvicorn main:app --app-dir /path/to/finlify --port 8000
```

Set `FINLIFY_DATA_DIR` to store the database somewhere other than the project
directory:

```bash
FINLIFY_DATA_DIR=~/.finlify python3 -m uvicorn main:app --port 8000
```

## Data model

- Money is stored and aggregated exclusively as **integer cents**
  (`amount_cents`, `limit_cents`). Input in major units is converted once at the
  API boundary, rounding half-up. Floats appear only in responses, for display.
- `type` must be `income` or `expense`. A row with any other value is still shown
  in the ledger but is excluded from every total; the dashboard reports how many
  such rows exist. This is how the five placeholder rows in the starter database
  behave.
- Categories are user-owned and stored in their own table. Transactions reference
  a category by name, validated against that table, so history survives a rename
  or an archive.
- On first run the database is created and seeded with a set of starter
  categories. Existing databases are migrated automatically and idempotently.

## Layout

```
main.py         FastAPI app, routes and analytics
models.py       Transaction, Category, Budget, AppMeta
money.py        exact cents conversion (to_cents / from_cents)
migrations.py   idempotent schema migrations and starter categories
database.py     SQLite engine/session (cwd-independent, FINLIFY_DATA_DIR aware)
templates/
  index.html
static/
  style.css     theme
  charts.js     canvas chart engine (area, donut, bar, sparkline)
  app.js        data loading, rendering and interaction
  favicon.svg
finlify.db      your data (git-ignored)
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | status, database path, row counts |
| GET | `/api/summary` | headline metrics, month deltas, insights, budgets |
| GET | `/api/timeseries?months=` | monthly income / expense / net buckets |
| GET | `/api/breakdown?months=&type=` | per-category totals, share and colour |
| GET | `/api/daily?days=` | per-day income / expense / net |
| GET | `/api/transactions` | list with `type`, `category`, `search`, `date_from`, `date_to`, `sort`, `order`, `limit`, `offset` |
| POST | `/api/transactions` | create |
| PUT | `/api/transactions/{id}` | update |
| DELETE | `/api/transactions/{id}` | delete |
| GET | `/api/categories` | list (`include_archived` optional) |
| POST | `/api/categories` | create |
| PUT | `/api/categories/{id}` | rename, recolour, archive |
| DELETE | `/api/categories/{id}` | delete when unused |
| GET | `/api/budgets` | current-month budget status |
| PUT | `/api/budgets/{category}` | set a limit |
| DELETE | `/api/budgets/{category}` | clear a limit |
| GET | `/api/export` | download a JSON backup |
| POST | `/api/import` | restore or merge a JSON backup |

Legacy `GET /dashboard` remains available for older callers.

## Privacy

Finlify is designed to keep your data on your machine. `finlify.db` and any
`finlify-backup-*.json` files are git-ignored so a real ledger is never committed
by accident.
