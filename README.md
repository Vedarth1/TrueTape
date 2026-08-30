# TrueTape — Loan Data Verification Copilot

TrueTape ingests messy, multi-source loan tapes, detects data-quality defects with a
deterministic rule engine, uses an AI review assistant to explain and propose fixes for
each exception, and produces **hash-chained verified records** that a downstream consumer
can trust. Built for the **Intain Campus FinTech Challenge 2026 — Full Stack Track**.

The system is built around one hard rule: **the AI never mutates data.** It only
recommends. Every correction is written by a human reviewer, and every state change is
appended to a tamper-evident audit chain.

---

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | React 19, Vite 8, React Router 7, TanStack Query 5, axios, Tailwind CSS v4 |
| Backend | Python 3.12, Flask 3, SQLAlchemy 2, Flask-Migrate (Alembic), Flask-JWT-Extended, marshmallow, psycopg 3 |
| Database | PostgreSQL 16 (two-role, least-privilege, append-only enforced) |
| Runtime | Docker Compose (db + backend + frontend) |

---

## How it works (pipeline)

```
upload CSVs ──► parse + normalise ──► rule validation ──► canonical blend ──► reviewer resolves ──► verify ──► verified record
 (operator)      stages 1–2            stages 3            stage 4            exceptions           (gated)     (hash-chained)
```

1. **Ingest** — an operator uploads source files (`POST /api/files`). Stages 1–2 (parse + normalise into versioned source records) run automatically on upload.
2. **Validate** — `flask run-pipeline` runs row-scope, dataset-scope, and cross-source rules, blends surviving values into a canonical record, and clusters the resulting exceptions.
3. **Review** — reviewers work the exception queue, ask the AI to explain/classify/draft a note, and record accept / edit / reject decisions (single or bulk).
4. **Verify** — a loan with no open *blocking* exceptions can be verified, minting a hash-chained verified record with a trust score.
5. **Consume** — the consumer dashboard browses verified loans, provenance, timelines, and exports.

---

## Prerequisites

- **Docker** and **Docker Compose v2** (`docker compose`, not `docker-compose`)
- **make** (optional but recommended — wraps the DB lifecycle commands)
- For local frontend work only: **Node 20+**

Everything runs in containers; you do not need Python or Postgres installed locally.

---

## Quick start (Docker)

From the project root:

```bash
# 1. Create the backend/compose env file from the template.
#    (The template filename has trailing spaces — the glob copies it reliably.)
cp .env.example* .env

# 2. Generate a strong JWT signing key and paste it into .env as JWT_SECRET_KEY=
python3 -c "import secrets; print(secrets.token_hex(32))"

# 3. Create the frontend env file.
cp frontend/.env.example frontend/.env

# 4. Build and start all three services.
docker compose up -d --build

# 5. Initialise the database: drop → migrate → harden (least-privilege) → seed.
#    Prints the seeded login emails when it finishes.
make db-reset
```

Then load the sample dataset and run validation:

```bash
# Upload the two provided source files through the Operator UI at
# http://localhost:5173 (log in as the operator below), OR post them directly:
#   data/seed/loan_tape.csv        (origination source)
#   data/seed/servicer_update.csv  (servicer source)

# Run the validation + canonical pipeline over the imported data:
make validate
```

Open **http://localhost:5173** and log in.

> **No `make`?** Run the wrapped command directly, e.g.
> `docker compose exec -T backend flask reset-db --yes && docker compose exec -T backend flask harden-db && docker compose exec -T backend flask seed`

---

## Service URLs & ports

| Service | URL | Container port | Host port |
|---|---|---|---|
| Frontend (Vite) | http://localhost:5173 | 5173 | 5173 |
| Backend API | http://localhost:5001/api | 5000 | 5001 |
| PostgreSQL | localhost:5433 | 5432 | 5433 |
| Health check | http://localhost:5001/api/health | — | — |

---

## Test credentials

Seeded by `flask seed`. Login is by **email + password**.

| Role | Email | Password |
|---|---|---|
| Operator | `operator@truetape.dev` | `operator123` |
| Reviewer | `reviewer@truetape.dev` | `reviewer123` |
| Consumer | `consumer@truetape.dev` | `consumer123` |

---

## Environment variables

### Backend / Compose — root `.env`

| Variable | Purpose | Example (dev default) |
|---|---|---|
| `POSTGRES_DB` | Database name (used by the db container + compose) | `truetape` |
| `POSTGRES_USER` | Owner/superuser role that **owns every table** | `truetape_owner` |
| `POSTGRES_PASSWORD` | Owner role password | `truetape_owner_pw` |
| `DATABASE_URL` | **Runtime** connection — the least-privilege *app* role (owns nothing) | `postgresql+psycopg://truetape_app:truetape_app_pw@db:5432/truetape` |
| `MIGRATION_DATABASE_URL` | **Migrations only** — the *owner* role, used by `flask db upgrade` | `postgresql+psycopg://truetape_owner:truetape_owner_pw@db:5432/truetape` |
| `FLASK_APP` | App entrypoint | `wsgi:app` |
| `FLASK_DEBUG` | Flask debug reloader | `1` |
| `APP_ENV` | `development` (default) or `production`. `production` enforces the JWT guard and blocks `reset-db`. | `development` |
| `JWT_SECRET_KEY` | Token signing key. **Must** be a strong random value; a weak/empty key is a hard boot failure when `APP_ENV=production`. | *(set via `secrets.token_hex(32)`)* |
| `CORS_ORIGINS` | Comma-separated allowed origins | `http://localhost:5173` |
| `AI_PROVIDER` | `deterministic` (the submission default — no external LLM) or `groq` | `deterministic` |
| `GROQ_API_KEY` | Only used when `AI_PROVIDER=groq` | *(empty)* |
| `GROQ_MODEL` | Groq model id (when enabled) | `openai/gpt-oss-120b` |
| `GROQ_REASONING_EFFORT` | Groq reasoning effort (when enabled) | `low` |
| `AI_TIMEOUT_SECONDS` | AI call timeout | `20` |
| `UPLOAD_DIR` | Where uploaded source files are stored | `/data/uploads` |

### Frontend — `frontend/.env`

| Variable | Purpose | Example |
|---|---|---|
| `VITE_API_URL` | Base URL the SPA calls | `http://localhost:5001/api` |

> **The AI runs offline by default.** `AI_PROVIDER=deterministic` uses a built-in
> deterministic review assistant, so the app is fully functional with **no API keys**.
> The Groq variables are optional and only read when you switch the provider.

---

## Make / CLI reference

All `make` targets execute inside the running `backend` container as the owner role.

| Command | What it does |
|---|---|
| `make db-reset` | Full clean cycle: drop → migrate → harden → seed (prints login emails) |
| `make db-bootstrap` | First-time bring-up without dropping data: upgrade → harden |
| `make upgrade` | Apply pending migrations (runs as the owner) |
| `make migrate m="msg"` | Autogenerate a new migration |
| `make harden-db` | Grant the app role least privilege; revoke UPDATE/DELETE/TRUNCATE on append-only tables |
| `make seed` | Load seed users, trust config, and validation rules |
| `make validate` | Run validation + canonical + clustering stages over imported data |

Underlying Flask CLI (if you prefer `docker compose exec backend flask <cmd>`):
`reset-db`, `harden-db`, `seed`, `run-pipeline` (`--force` to re-run), `assign-clusters`,
`reconcile-oracle` (diffs engine output against the 215-row QA oracle).

---

## API overview

All routes are under `/api`. Authenticated routes require a `Bearer <token>` header
(the token is returned by `POST /api/auth/login`).

| Blueprint | Prefix | Key endpoints |
|---|---|---|
| Auth | `/api/auth` | `POST /login`, `POST /signup`, `GET /me` |
| Files (ingest) | `/api/files` | `POST /` (multipart `file` upload → parse + normalise) |
| Pipeline | `/api/pipeline` | `POST /run` (validation + canonical stages) |
| Exceptions | `/api/exceptions` | list, detail, `POST /:id/resolve`, `POST /batch` (bulk resolve), clusters, stats |
| AI assistant | `/api` | `POST /exceptions/:id/analyze`, `/classify`, `/note`, `POST /ai/batch-summary` |
| Rules | `/api` | `POST /ai/generate-rule`, `POST /rules/preview`, `POST /rules` (publish / toggle) |
| Verified | `/api` | `POST /loans/:id/verify`, `POST /verify-batch`, `GET /verify` |
| Consumer | `/api` | `GET /loans`, `GET /loans/:id`, timeline, `GET /summary`, `GET /export` |
| Audit | `/api/audit` | hash-chained audit trail queries |
| Health | `/api` | `GET /health` |

---

## Project structure

```
Intain Challenge/
├─ backend/
│  ├─ app/
│  │  ├─ __init__.py       app factory + blueprint registration
│  │  ├─ config.py         env-driven config + production JWT guard
│  │  ├─ extensions.py     db / migrate / jwt / cors
│  │  ├─ cli.py            seed / reset-db / harden-db / run-pipeline / reconcile-oracle
│  │  ├─ auth/             login, signup, me (JWT, timing-safe)
│  │  ├─ ingestion/        upload → parse → normalise (versioned source records)
│  │  ├─ validation/       row / dataset / cross-source rule engine
│  │  ├─ services/         AI review assistant (deterministic), trust score, audit chain
│  │  ├─ exceptions/       exception queue, resolve, bulk resolve, clustering
│  │  ├─ api/              pipeline, verified, ai, rules, consumer, audit, health
│  │  └─ models/           SQLAlchemy models
│  ├─ migrations/          Alembic migrations
│  ├─ tests/               pytest suite
│  └─ Dockerfile
├─ frontend/
│  ├─ src/
│  │  ├─ pages/            Landing, Login, Signup, Operator, ReviewerQueue, Rules, Consumer
│  │  ├─ components/       AppShell (role nav), HealthBadge
│  │  └─ lib/              api.js (axios + JWT), auth.jsx, format.js
│  ├─ vite.config.js
│  └─ Dockerfile
├─ data/
│  ├─ generate_dataset.py  messy multi-source tape generator
│  ├─ check_contract.py    rule/contract sanity check
│  └─ seed/                users.json, validation_rules.json, trust_config.json,
│                          loan_tape.csv, servicer_update.csv, + QA oracle
├─ db/init/01-roles.sql    creates the least-privilege app role on first boot
├─ docs/AI_DEV_LOG.md      AI development log
├─ docker-compose.yml
├─ Makefile
└─ .env.example
```

---

## Security & data-integrity model

- **Two-role Postgres.** `truetape_owner` owns every table and runs migrations. The app
  connects as `truetape_app`, which owns nothing and is granted only SELECT/INSERT/UPDATE/DELETE.
  `make harden-db` then **revokes UPDATE/DELETE/TRUNCATE** on append-only tables (audit log,
  source records, verified records), enforced at the database level.
- **AI is advisory only.** The review assistant never writes to loan, source, canonical, or
  verified tables. Only a human ReviewerDecision path mutates canonical data.
- **Append-only audit chain.** Every state change is hashed into a tamper-evident chain;
  verified records carry their own per-loan hash chain.
- **JWT auth.** 12-hour access tokens; a weak or unset `JWT_SECRET_KEY` refuses to boot when
  `APP_ENV=production`.

---

## Local development (without Docker)

Docker is the supported path (it also provisions the two DB roles via `db/init`). If you
must run natively, you need Python 3.12 and a local Postgres with both roles created, then:

```bash
# Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' ../.env | xargs)   # or set env vars yourself
flask db upgrade && flask harden-db && flask seed
flask run --port 5001

# Frontend (separate terminal)
cd frontend
npm install
npm run dev        # http://localhost:5173
```

---

## Testing

```bash
# Backend unit/integration tests (needs the DB up)
docker compose exec -T backend pytest

# Frontend lint
cd frontend && npm run lint

# Data contract sanity check
python3 data/check_contract.py

# Diff the engine's findings against the provided 215-row QA oracle
docker compose exec -T backend flask reconcile-oracle
```

---

## Troubleshooting

- **`cp: .env.example: No such file or directory`** — the template file's name has trailing
  spaces. Use the glob: `cp .env.example* .env`.
- **Backend refuses to boot with a JWT error** — `APP_ENV=production` with an empty/weak
  `JWT_SECRET_KEY`. Set a strong key (`python3 -c "import secrets; print(secrets.token_hex(32))"`).
- **`make` targets fail with "No such container"** — the stack isn't running. `docker compose up -d` first.
- **App role missing / permission denied** — the `truetape_app` role is created by
  `db/init/01-roles.sql` on the **first** boot of a fresh volume. If you changed roles, recreate
  the volume: `docker compose down -v && docker compose up -d --build`, then `make db-reset`.
- **`npm run build` fails with `@rolldown/binding-linux-*`** — Vite 8 uses rolldown, whose
  native binary is platform-specific. Reinstall on the target OS: `rm -rf node_modules && npm install`.

---

## Deliverables & docs

- `docs/AI_DEV_LOG.md` — AI development log.
- `data/seed/generation_summary.json` — QA oracle (215 injected defects) used by `reconcile-oracle`.
- `data/generate_dataset.py` — regenerate the messy multi-source dataset.

## Deploying on AWS EC2 (Docker, free tier)

The whole stack runs in Docker Compose on a t2.micro (1 GB RAM — add a 2 GB swapfile).

```bash
# on the instance (Ubuntu 24.04): install docker + compose, clone the repo
sudo apt update && sudo apt -y install docker.io docker-compose-plugin git make
sudo usermod -aG docker ubuntu && newgrp docker
git clone <your-repo-url> ~/TrueTape && cd ~/TrueTape

# secrets: create backend/.env from your local values, but with production settings
#   APP_ENV=production  JWT_SECRET_KEY=<openssl rand -hex 32>
#   CORS_ORIGINS=http://<EC2_PUBLIC_IP>:5173
#   VITE_API_URL=http://<EC2_PUBLIC_IP>:5001/api      (read by the web container)

# open in the AWS security group: 22 (SSH, My IP), 5173, 5001 (HTTP)

docker compose up -d          # db + api + web (db/init creates both PG roles)
make db-reset                 # migrate -> harden-db -> seed (needs data/seed, committed)
```

Then load the demo data and you are live:

1. Open `http://<EC2_PUBLIC_IP>:5173` → sign in as operator (`operator@truetape.dev` / `operator123`)
2. Upload `data/seed/loan_tape.csv`, `servicer_update.csv`, `document_manifest.csv`
3. Run pipeline (dashboard button, or `docker compose exec backend flask run-pipeline`)
4. Sign in as reviewer → **Verify all eligible loans**

Gotchas that bit us:

- `data/seed/` must be committed — `flask seed` reads it from the mounted volume
- `VITE_API_URL` must point at the EC2 IP, not localhost, or the browser cannot reach the API
- Security group must expose 5173 (UI) and 5001 (API) — or put nginx in front and expose 80
- Free-tier t2.micro: add a swapfile, and expect ~50 s of latency after idle spin-down only on PaaS; EC2 stays warm

`deploy/DEPLOY_EC2.md` has the alternative no-Docker layout (native Postgres + systemd + nginx).
