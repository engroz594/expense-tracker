# Expense Tracker API

An async REST API for tracking spending by category in several currencies. Every user
has a base currency, and reports are always shown in it.

**Stack:** Python 3.12, FastAPI, async SQLAlchemy 2.0 (asyncpg), Pydantic v2, Alembic,
PostgreSQL, Redis. Fully async — no blocking calls in async code (ruff's `ASYNC` rules
enforce it). Money is `Decimal` in Python and `NUMERIC` in PostgreSQL, never a float.

- [Setup and running](#setup-and-running)
- [API](#api)
- [Architecture](#architecture)
- [Design decisions](#design-decisions)
- [Indexes](#indexes)
- [Tests and coverage](#tests-and-coverage)
- [Deployment notes (AWS)](#deployment-notes-aws)
- [Short answers](#short-answers)
- [What is not finished, and how I would build it](#what-is-not-finished-and-how-i-would-build-it)
- [Use of AI tools](#use-of-ai-tools)

---

## Setup and running

PostgreSQL (13+) and Redis run outside the app — on your machine locally, RDS and
ElastiCache on AWS. Docker Compose runs only the app containers.

```bash
uv sync                                            # Python 3.12 is pinned in .python-version
cp .env.example .env                               # set DB_USER, DB_PASSWORD, DB_NAME, JWT_SECRET
uv run alembic upgrade head                        # migrations (schema only)
uv run python -m app.scripts.fill_exchange_rates   # exchange rates for the last 90 days
uv run uvicorn main:app --reload                   # http://localhost:8000/docs
```

Or in containers (they reach the host's Postgres/Redis via `host.docker.internal`):

```bash
docker compose up --build    # `migrate` runs migrations + the rate fill, then `api` starts
```

**Exchange rates.** `app/scripts/fill_exchange_rates.py` inserts one rate per currency
(PKR, AED, USD, EUR) for every day of the last 90 days, skipping rows that already exist.
It is idempotent: run it again any time to add the latest days and fill gaps. Rates are
data that grows daily, so they are filled by a re-runnable script rather than a one-time
migration. The values are realistic but generated (USD = 1, AED pegged at 3.6725, EUR
and PKR moving on a fixed daily pattern), not live market data.

**S3 (receipts).** Set `S3_BUCKET` in `.env` to a private bucket that exists. On AWS,
leave the keys and `S3_ENDPOINT_URL` unset: the app uses the ECS task's IAM role.
Locally, any S3-compatible store works, e.g. MinIO:

```bash
docker run -d -p 9000:9000 -e MINIO_ROOT_USER=minio -e MINIO_ROOT_PASSWORD=minio-secret \
  minio/minio server /data
# .env: S3_ENDPOINT_URL=http://localhost:9000, S3_ACCESS_KEY_ID=minio,
#       S3_SECRET_ACCESS_KEY=minio-secret; create the bucket in the MinIO console (:9000)
```

Pre-signed URLs are signed for `S3_ENDPOINT_URL`, so for local receipt uploads run the
API on the host (`uv run uvicorn …`), where `localhost:9000` is reachable by the client
too. Failed S3 deletes are retried by `uv run python -m app.scripts.cleanup_s3_deletions`
(run it on a schedule).

**Tests:** `uv run pytest` — or plain `pytest` inside the activated `.venv`. Needs the
local Postgres and Redis and `.env` with `DB_USER`/`DB_PASSWORD`; S3 is faked with moto
(see [Tests and coverage](#tests-and-coverage)).

**Postman:** `postman/` holds a collection for every endpoint and an environment file.

## API

All endpoints are under `/api/v1`, except the health probes.

| Method | URL | What it does | Success |
|---|---|---|---|
| GET | `/health` | Liveness: the process is up | 200 |
| GET | `/ready` | Readiness: Postgres and Redis respond (2 s timeout each) | 200 / 503 |
| POST | `/auth/register` | Create a user | 201 |
| POST | `/auth/login` | Access + refresh token | 200 |
| POST | `/auth/refresh` | Rotate the refresh token, new pair | 200 |
| POST | `/auth/logout` | Revoke the session of this refresh token | 204 |
| POST | `/categories` | Create a category (optional monthly budget) | 201 |
| GET | `/categories` | My categories with this month's spend vs budget | 200 |
| POST | `/expenses` | Create an expense (optional `Idempotency-Key`) | 201 |
| GET | `/expenses` | Filters + keyset pagination | 200 |
| GET | `/expenses/{id}` | One expense, with `ETag` | 200 |
| PATCH | `/expenses/{id}` | Partial update, needs `If-Match` | 200 |
| DELETE | `/expenses/{id}` | Delete | 204 |
| POST | `/expenses/{id}/receipt/upload-url` | Pre-signed POST to upload a receipt straight to S3 | 200 |
| POST | `/expenses/{id}/receipt/confirm` | Verify the uploaded file (exists, ≤ 5 MB, real type) and attach it | 200 |
| GET | `/expenses/{id}/receipt` | Download link, valid 15 minutes | 200 |
| GET | `/reports/monthly?year=&month=` | Totals per category in base currency, vs last month | 200 |

**Errors** always have one shape, including framework 404/405 and validation errors:

```json
{"error": {"code": "version_conflict", "message": "...", "details": []}}
```

400 bad cursor · 401 bad/missing token · 404 not found or not yours · 409 duplicate or
idempotent request in progress · 412 stale `If-Match` · 422 validation, no exchange rate,
Idempotency-Key reused with another body · 428 missing `If-Match` · 429 login rate
limit (with `Retry-After`) · 503 dependency down.

**Money** is sent as a JSON string or number and returned as a string with exactly two
decimals (`"12.50"`), so no client parses it into a float.

## Architecture

```
main.py                    create_application()
routes/api/v1.py           mounts each feature's router under /api/v1
app/controllers/           thin HTTP layer: parse, call a service, shape the response
app/services/              business logic and queries
app/models/                SQLAlchemy models (no relationship(): async can't lazy-load)
app/schemas/               Pydantic request/response models, Money types
app/core/                  config, DB engine/session, Redis, auth, security, errors, pagination
app/exceptions/            AppError hierarchy -> the error envelope
app/scripts/               maintenance scripts (exchange-rate fill)
database/migrations/       Alembic, one migration per table, async env using the app's Settings
```

A request: router → controller (validated Pydantic model, `get_current_user`, a service
built from the request's `AsyncSession`) → service (one transaction, commits once) →
response model. Errors are raised as `AppError` subclasses and converted to the envelope
by one set of handlers.

**Schema.** `users`, `categories`, `expenses`, `exchange_rates` as in the brief, plus
`refresh_tokens` (needed for rotation, reuse detection and logout) and
`pending_s3_deletions` (receipt files whose S3 delete must still happen). Idempotency
keys live in Redis, so they need no table.

**Storage.** Code that stores files depends on the `StorageClient` interface
(`app/contracts/storage_client.py`); `S3StorageClient`
(`app/clients/storage/s3_storage_client.py`, aiobotocore) is the only code that talks
to S3.

## Design decisions

### Auth
- Passwords: argon2id, run in `asyncio.to_thread` (hashing is CPU-bound and would block
  the event loop). An unknown email still runs a dummy verification and gets the same 401
  as a wrong password, so neither timing nor response reveals which emails exist.
- Access token: 15-minute HS256 JWT with `sub`, `sid`, `type`, `iat`, `exp`. The algorithm
  is pinned server-side.
- Refresh token: 256 random bits, 7 days, stored only as SHA-256. A login starts a
  **session** (a token family). Each refresh marks the presented token used and issues a
  new one in the same family.
- **Reuse = theft.** Presenting an already-used refresh token revokes every refresh token
  of the user. The revocation is committed *before* the 401 is raised, so the error
  can't roll it back.
- **Access tokens are revoked too.** Access JWTs carry their session id; `get_current_user`
  accepts them only while that session still has a live refresh token (one indexed
  query, combined with the user lookup). So "revoke all tokens" and logout take effect
  immediately instead of after up to 15 minutes. Logout ends only its own session.
- Two concurrent refreshes with the same token are serialised by `SELECT … FOR UPDATE`;
  the second sees the token used and is treated as reuse. A client that double-fires a
  refresh therefore logs itself out — the safe direction.

### Login rate limit
5 failed logins per email per 15 minutes, using `limits.aio` (moving window) on Redis
with the redis-py asyncio client. `hit()` checks and records atomically *before* the
password is verified, so parallel guesses can't slip past 5 and the limit holds across
any number of API servers. A successful login clears the counter, so in effect only
failures count; unknown emails are counted too. Over the limit: 429 with `Retry-After`.
slowapi was not used: it calls the synchronous `limits` API (blocking Redis I/O in async
code), can't key on a JSON body field, and counts every request rather than failures.
If Redis is down the limiter **fails open** (logins allowed, warning logged).

### Data access
Every query filters on `user_id`, so another user's expense or category is a 404, never
a 403. The database enforces it too: `expenses (category_id, user_id)` is a foreign key to
`categories (id, user_id)`, so an expense can never point at another user's category.

### Listing — keyset pagination
`ORDER BY spent_on DESC, id DESC`; the next page is `WHERE (spent_on, id) < (cursor)`.
The cursor is an opaque base64 token holding the last row's `(spent_on, id)`. A page
depends only on that row, not on how many rows came before it, so inserts between
requests can't shift rows into duplicates or gaps (an OFFSET would). We fetch
`limit + 1` rows to know whether a next page exists, instead of a `COUNT(*)`. `q` is a
case-insensitive substring match with `%`, `_` and `\` escaped.

### No lost updates — ETag / If-Match
`ETag: "<version>"`. PATCH requires `If-Match` (428 if missing or `*`). The check and the
write are one statement:

```sql
UPDATE expenses SET ..., version = version + 1, updated_at = now()
WHERE id = :id AND user_id = :user AND version = :expected
RETURNING *
```

Of two concurrent PATCHes with the same ETag, Postgres locks the row for the first; the
second re-evaluates its `WHERE` after the first commits, matches nothing, and gets 412.
The version is never compared in Python.

### Idempotency-Key — Redis lock
`POST /expenses` with `Idempotency-Key`:
1. `SET idempotency:{user}:{sha256(key)} {"state":"processing","hash":H} NX EX 86400`.
   `NX` is atomic, so exactly one request claims the key — however many arrive at once,
   on whichever server.
2. A request that finds the key taken never creates anything: a different body hash →
   **422**; same body, still running → **409**; same body, done → the stored first
   response is replayed (status, body, ETag, `Idempotent-Replayed: true`).
3. The winner stores the response with `KEEPTTL`, so keys expire 24 h after first use
   and Redis deletes them itself.

`H` is the SHA-256 of the canonical validated body (key order and `12.5` vs `"12.50"`
don't matter). **Why Redis rather than a DB unique constraint:** native expiry, an exact
replay of the first response, no extra schema, and it is shared by all servers. **Failure
modes, chosen so a duplicate is impossible:** a failed create releases the key; a crash
after the commit but before the response is stored leaves the key "processing", so
retries get 409 until it expires — a stuck retry, never a duplicate; if Redis is down,
requests *with* the header get 503 (fail closed), requests without it are unaffected.

### Monthly report — one SQL statement
`app/services/report_service.py`:

1. `converted` — each expense of the two months, converted to the base currency:
   `amount × rate(currency) / rate(base)`, each rate the latest with
   `valid_on <= spent_on` (`CROSS JOIN LATERAL … ORDER BY valid_on DESC LIMIT 1`), so a
   day without a rate uses the previous one.
2. Categories `CROSS JOIN` the two months, `LEFT JOIN converted`, `GROUP BY` — categories
   without spend still appear.
3. `LAG(total) OVER (PARTITION BY category ORDER BY month)` gives last month's total.
4. The final `SELECT` adds percent used and the change, and rounds **once**, at the end.

The same conversion CTE serves `GET /categories` (this month's spend vs budget). Every
expense write requires a rate on or before its date for both its currency and the
user's base currency (else 422 `no_exchange_rate`), so the inner lateral joins can never
silently drop an expense. "This month" is the UTC calendar month.

### Receipts on S3
1. **Upload URL** — `POST /expenses/{id}/receipt/upload-url` with `content_type`
   (`image/jpeg`, `image/png` or `application/pdf`). The server picks the key,
   `receipts/{user}/{expense}/{uuid}.{jpg|png|pdf}`, and returns a pre-signed **POST**
   valid 5 minutes. Its signed policy pins the key and content type and sets
   `content-length-range` 1 B – 5 MB, so S3 itself rejects a bigger file (a pre-signed
   PUT can't limit size). The client uploads straight to S3; the file never passes
   through the API.
2. **Confirm** — `POST /expenses/{id}/receipt/confirm` with the key. The key must be one
   we could have issued for *this* expense (prefix, uuid name, allowed extension), so a
   client can't attach someone else's file. `HeadObject` proves the file exists (missing
   → 404) and re-checks the size; then a ranged GET reads the first 8 bytes and checks
   the **real** type from its signature (`FF D8 FF` JPEG, `89 50 4E 47 0D 0A 1A 0A` PNG,
   `%PDF-`), which must match the extension. On a mismatch — e.g. an `.exe` renamed to
   `.png` — the object is deleted and the request fails with 422. Otherwise
   `receipt_key` is saved and `version` is bumped (a new ETag, like any change).
3. **Download** — `GET /expenses/{id}/receipt` returns a pre-signed GET valid 15 minutes.
4. **Deleting files without forgetting them** — deleting an expense (or replacing its
   receipt) inserts a `pending_s3_deletions` row **in the same transaction** that drops
   the reference. After the commit the API tries the S3 delete and removes the row on
   success. If S3 fails, the row stays with the error and a next-attempt time
   (exponential backoff, 1 min doubling up to 1 h); `python -m
   app.scripts.cleanup_s3_deletions` retries every due row (claimed with `FOR UPDATE
   SKIP LOCKED`, so concurrent runs are safe). It runs on a schedule (cron locally, an
   EventBridge-scheduled ECS task on AWS). The file is cleaned up later, never forgotten.

### Exchange-rate convention
`rate_to_usd` = USD value of one unit. Converting A → B on day d is
`amount × rate_A(d) / rate_B(d)`.

## Indexes

| Index | Query it serves |
|---|---|
| `expenses (user_id, spent_on DESC, id DESC)` | `GET /expenses`: same order as the sort, so the keyset page is an index range scan that stops after `limit + 1` rows — no sort, no offset |
| `expenses (user_id, category_id, spent_on)` | Report and category spend (user + category + month range); also the composite FK check |
| GIN trigram on `expenses.description` | `q` filter (`ILIKE '%term%'`); a B-tree can't serve a leading wildcard |
| `exchange_rates` PK `(currency, valid_on)` | One row per currency per day, and the "latest rate on or before d" lookup is one backward probe |
| unique `categories (user_id, lower(name))` | Case-insensitive duplicate names → 409, decided by the index (no race); also "list my categories" |
| unique `categories (id, user_id)` | Target of the composite FK from `expenses` |
| unique `users (email)` | Registration duplicates (409) and login lookup |
| unique `refresh_tokens (token_hash)` | Lookup on every refresh/logout |
| `refresh_tokens (user_id)` | Revoke all of a user's tokens on reuse |
| partial `refresh_tokens (family_id) WHERE used_at IS NULL AND revoked_at IS NULL` | Session check on every authenticated request; only live rows, so it stays tiny |
| unique `pending_s3_deletions (object_key)` | One pending row per file (`ON CONFLICT DO NOTHING` when staged twice); lookup when a delete succeeds |
| `pending_s3_deletions (next_attempt_at)` | The cleanup script's `WHERE next_attempt_at <= now()` |

## Tests and coverage

```bash
uv run pytest                        # everything, with a coverage report
uv run pytest --cov-fail-under=85    # with the brief's gate
```

**182 tests, 98.78 % coverage.** They run against a real PostgreSQL database
(`expense_tracker_test`, created and migrated automatically) and Redis DB 15. Every table
is truncated and Redis flushed before each test — not a rolled-back transaction, because
the concurrency tests need real, separate connections and commits. The harness refuses
to run against a database whose name doesn't end in `_test`.

The brief's required tests:

| Required test | Where |
|---|---|
| Refresh token used twice revokes all tokens | `tests/api/test_auth_tokens.py::test_using_a_refresh_token_twice_revokes_all_tokens` |
| 6th failed login → 429 | `tests/api/test_login_rate_limit.py::test_sixth_failed_login_returns_429_with_retry_after` |
| Another user's expense → 404 | `tests/api/test_expenses_crud.py::test_another_users_expense_returns_404` (+ PATCH in `test_expenses_update.py`) |
| Amount 0, negative, > 2 decimals rejected | `tests/api/test_expenses_crud.py::test_zero_negative_or_more_than_two_decimals_is_rejected` |
| No duplicate/missing rows while rows are added between pages | `tests/api/test_expenses_list.py::test_rows_added_between_pages_cause_no_duplicates_or_gaps` |
| Report right with mixed currencies and a missing rate day | `tests/api/test_reports.py::test_report_converts_mixed_currencies_with_missing_rate_days` |
| Two parallel POSTs, same Idempotency-Key → one expense | `tests/api/test_idempotency.py::test_parallel_posts_with_same_key_create_only_one_expense` |
| Two parallel PATCHes, same ETag → 200 + 412 | `tests/api/test_expenses_update.py::test_parallel_patches_with_same_etag_one_wins_one_gets_412` |
| Budget alert event processed twice → one notification | not built (see below) |
| CSV import with good and bad rows → right counts | not built (see below) |

S3 is never called in tests: they run against **moto** in server mode (a fresh bucket per
test), with real pre-signed POST uploads and downloads. The fake-file test is
`tests/api/test_receipts.py::test_exe_renamed_to_png_is_rejected_and_deleted`; a failing
S3 delete followed by the cleanup run is
`test_failed_s3_delete_is_kept_and_cleaned_up_later`.

Also: the report is proven to be a single SQL statement (an engine event listener counts
the statements), migrations are checked for drift (`alembic check`) and for clean
downgrade/upgrade, and the concurrency tests were run 20 times in a row without a flake.

## Deployment notes (AWS)

### 1. API and worker
- **ECS on Fargate**, one image for every role; only the command differs (API,
  migrations, and the background worker once built).
- **API service:** at least 2 tasks spread over 2 AZs behind an ALB. Target-tracking
  auto scaling on CPU (~60 %) and ALB requests per target; min 2, max ~10. ECS replaces
  tasks that exit or fail their container health check (`/health`).
- **S3 cleanup:** `python -m app.scripts.cleanup_s3_deletions` as an EventBridge-scheduled
  ECS task (same image) every 10 minutes; it's idempotent and safe to overlap.
- **Worker service** (for the CSV import and budget-alert relay once built): a separate
  ECS service, 1–2 tasks, scaled on queue depth / outbox backlog (a CloudWatch metric).
- **Zero-downtime release:** rolling deployment with `minimumHealthyPercent=100`,
  `maximumPercent=200`. New tasks must pass the ALB health check before old ones are
  drained (deregistration delay ~30 s); on SIGTERM uvicorn stops accepting connections,
  finishes in-flight requests, and the app's lifespan closes the DB and Redis pools.
  The ECS deployment circuit breaker rolls back automatically if new tasks don't become
  healthy.
- **Health checks:** the ALB checks `/health`, not `/ready`. `/ready` includes Redis, and
  a Redis outage would then mark *every* task unhealthy and take the whole API down —
  even though the app is designed to keep working without Redis (see 3). `/ready` is
  for deploy gating and monitoring.

### 2. Database
- **RDS PostgreSQL**, Multi-AZ, in **private subnets**, no public access, encrypted at
  rest, TLS in transit.
- **Security groups:** `rds-sg` allows inbound **TCP 5432 only from `app-sg`** (the
  security group of the API and worker tasks). No other inbound rule. `app-sg` allows
  inbound 8000 only from `alb-sg`; the ALB accepts 443 from the internet.
- **Password:** an RDS-managed secret in **Secrets Manager** (with rotation). The ECS task
  definition's `secrets` block injects it as `DB_PASSWORD`; the app reads it from the
  environment through pydantic Settings. It is never in the image, the repo or the task
  definition in plain text. The ECS *execution* role is allowed
  `secretsmanager:GetSecretValue` on that one secret. `JWT_SECRET` is handled the same way.

### 3. Redis
- **ElastiCache for Redis**, replication group with a replica in another AZ and automatic
  failover, private subnets, `redis-sg` allowing 6379 only from `app-sg`, in-transit
  encryption and an AUTH token from Secrets Manager.
- **If Redis goes down:**
  - *Login rate limit* fails **open**: logins keep working, a warning is logged and an
    alarm should fire. Brute-force protection is missing for that time — chosen over
    locking every user out.
  - *Idempotency keys* fail **closed**: `POST /expenses` *with* an `Idempotency-Key` gets
    503 until Redis is back (retries are safe to repeat later); requests without the
    header work. Without Redis we can't promise "only once", and a duplicate expense is
    worse than a rejected retry.
  - Keys lost in a failover (Redis replication is asynchronous) mean a retry that arrives
    right after could create a second expense. The replica + Multi-AZ setup makes that
    window small; the README's simple-vs-perfect answer (5) discusses the trade-off.
  - Sessions and tokens are unaffected: they live in Postgres.

### 4. S3
Private bucket: all four Block Public Access settings on, default encryption (SSE-S3 or
KMS), a bucket policy denying non-TLS requests, and a lifecycle rule to abort incomplete
multipart uploads after 1 day. The app uses the ECS
**task role** — no access keys anywhere. Smallest policy for what the app does:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AppObjects",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::expense-tracker-prod/receipts/*"
    }
  ]
}
```

No `s3:ListBucket`: the app never lists. The consequence is that `HeadObject` on a
missing key returns 403 instead of 404, which the code must treat as "not found".

### 5. Migrations during a release
- Migrations run as a **one-off ECS task** (same image, `alembic upgrade head`) in the
  pipeline **before** the service is updated; if it fails, the deploy stops. They never
  run on app start, so three API tasks can't race to migrate.
- Every migration must work with **both** the old and the new app version
  (**expand / contract**):
  - *Expand* (release N): only additive changes — new tables, new nullable columns or
    columns with defaults, `CREATE INDEX CONCURRENTLY` (in an Alembic
    `autocommit_block`). Old code ignores them; new code uses them.
  - Backfill data if needed, in batches.
  - *Contract* (release N+1, once no old task is running): drop old columns, add
    `NOT NULL`, remove fallbacks.
  - A rename is add-new → write both → backfill → read new → drop old, over several
    releases.
- All migrations in this repo are reversible and tested downgrade → upgrade.

## Short answers

**1. Why a pre-signed upload URL instead of sending the file through the API? What can go
wrong, and how do you guard against it?**
The file goes straight from the client to S3, so the API never holds the bytes: no
memory spikes, no worker tied up for the length of a slow mobile upload, no request
size or timeout limits on the load balancer, and S3 does the scaling. What can go wrong:
a leaked URL can be used by anyone until it expires → short expiry (minutes) and one
fixed, server-chosen key per URL (`receipts/{user}/{expense}/{uuid}.{ext}`) so it can't
overwrite anything else; a pre-signed PUT can't limit size → use a pre-signed **POST**
with a `content-length-range` condition; the client can lie about the type → check the
real type from the first bytes on confirm and delete the object if it doesn't match;
and the URL must be issued only after checking the expense belongs to the caller. One
gap I left open: an upload that is never confirmed stays in the bucket as an orphan. The
fix is to have clients upload to a staging prefix (`uploads/…`) with a 1-day lifecycle
expiry and to move the object to `receipts/` on confirm (or tag on confirm and expire
untagged objects).

**2. `GET /expenses` gets slow at 20 million rows. How do you find and fix it?**
Take the slow query with real parameters (from `pg_stat_statements` or the logs) and run
`EXPLAIN (ANALYZE, BUFFERS)`. Look for a `Seq Scan` on `expenses`, a `Sort` node on top of
many rows, large "Rows Removed by Filter", and high `shared read` (pages from disk) versus
`shared hit`. The fix for the main path is the composite index
`(user_id, spent_on DESC, id DESC)`: it matches the `WHERE user_id` *and* the
`ORDER BY`, so the plan becomes an index scan that stops after `limit + 1` rows,
whatever the table size. **Offset pagination** gets slow because `OFFSET 100000` still
reads and throws away 100,000 rows — every page costs more than the last, and inserts
shift the pages; keyset pagination (`WHERE (spent_on, id) < cursor`) jumps straight to the
position, so page 1,000 costs the same as page 1. Selective filters need their own
matching index (e.g. `(user_id, category_id, spent_on DESC, id DESC)` if category
filtering is common); `q` uses the trigram index, but combined with the sort the planner
may have to sort the matches — measure it. **N+1:** check the number of queries per
request, not just one plan. This API has no ORM relationships, so nothing lazy-loads per
row; if the response needed category names it would join them in the same query.
Finally, make sure statistics are fresh (`ANALYZE`, autovacuum) so the planner picks the
index.

**3. The API runs on 3 servers behind a load balancer. What breaks, and how is it made
safe?**
*Tokens:* JWTs are verified with a shared secret from Secrets Manager, and session state
(refresh tokens) is in Postgres — any server can serve any request. *Rate limits:* an
in-memory counter would give an attacker 5 tries per server; ours is in Redis and the
check-and-record is atomic, so it's 5 in total. *Idempotency:* the claim is `SET NX` in
Redis, atomic across servers. *Optimistic locking:* the version check is in the `UPDATE`
itself, so it doesn't matter which server handles which request. *Worker:* background
work belongs in a separate worker service, not in API processes, and jobs must be
idempotent because delivery is at-least-once. *File uploads:* never on a server's local
disk — S3, via pre-signed URLs. *Migrations:* run once, as a one-off task before the
deploy — never on start-up, where three servers would race. *Exchange-rate fill:*
idempotent (`ON CONFLICT DO NOTHING`), so running it twice is harmless.

**4. The outbox worker is down for 2 hours and 500,000 events pile up. What happens when
it comes back, and how do you avoid overloading the database?** *(The budget-alert outbox
isn't built — this is the design I'd implement.)*
Nothing is lost: events are rows written in the same transaction as the expenses, so they
simply wait. On restart the relay must not try to process them in one go. It claims
small batches (`SELECT … WHERE processed_at IS NULL ORDER BY id LIMIT 500 FOR UPDATE SKIP
LOCKED`, served by a partial index on unprocessed rows) and commits after each batch, so
locks are short and a crash only repeats one batch. Within a batch it **coalesces** events
to distinct (user, category, month) pairs — an alert depends on the current total, not on
each event — so 500,000 events typically become far fewer budget checks. It limits its own
pace (a pause between batches, fixed concurrency, a small DB pool) so normal traffic keeps
priority, and duplicates are harmless because notifications have a unique constraint
`(category, month, level)` with `ON CONFLICT DO NOTHING`. Backlog size and age are
metrics with alarms, so a 2-hour outage is noticed long before 2 hours.

**5. One place where I chose simple over "perfect", and why.**
Idempotency keys live only in Redis. The "perfect" version stores the key in a Postgres
table in the **same transaction** as the expense, so key and expense commit or fail
together and nothing depends on Redis. I chose Redis `SET NX EX` because it meets every
requirement — one expense under a race, the exact first response replayed, 24-hour expiry
that Redis handles itself — with no extra table, no cleanup job, and much less code to
review. The cost is two narrow windows that I accept and document: a crash between the
commit and storing the response leaves the key "processing" (retries get 409 until the key
expires — never a duplicate), and a Redis failover can lose a key and allow a late retry
to create a second expense. If duplicates ever became costly, I would move the key into
the expense transaction.

## What is not finished, and how I would build it

### 2.3 Budget alerts with events (not built)
- **Tables:** `outbox_events` (id bigint, type, payload JSONB, processed_at, attempts,
  last_error; partial index on `id WHERE processed_at IS NULL`) and `notifications`
  (user, category, month, level 80/100, spent, budget; **unique (category_id, month,
  level)**).
- **Producer:** `ExpenseService` already stages writes and commits once; create, update
  and delete would add an `expense.changed` outbox row (user, category, month — the old
  *and* new ones on a PATCH) to that same transaction.
- **Worker:** an arq cron job every few seconds claims batches with `FOR UPDATE SKIP
  LOCKED` (two workers never take the same rows), coalesces them to (user, category,
  month), recomputes the month's total with the existing conversion CTE, and for each
  level crossed inserts a notification with `ON CONFLICT DO NOTHING`, then marks the
  events processed — all in one transaction. A crash before commit repeats the batch; the
  unique constraint makes the repeat harmless. That is the "only once per category per
  month per level" guarantee.
- **Edits and deletes:** totals are recomputed from the current state, not from deltas.
  If an edit raises the total past a level, the alert fires. If an edit or delete lowers
  it, alerts already sent stay as history (a notification is a record of something that
  happened) and the same level never fires twice in a month, so a total moving around the
  threshold doesn't spam the user.
- `GET /notifications`: keyset-paginated, newest first.
- Test: process the same event twice → one notification.

### 2.4 Bulk CSV import (not built)
- `POST /expenses/import` streams the upload (hashing it as it goes) to S3 under
  `imports/{user}/{sha256}.csv` and returns 202 with a `job_id`; `GET /imports/{job_id}`
  shows status, rows done, rows failed and the first errors.
- An **arq** worker (async-native; Celery tasks are synchronous and would need a new event
  loop per task, which breaks asyncpg's loop-bound connection pool) streams the file to a
  temp file, reads it in batches of 1,000 rows in a thread, validates date, amount,
  currency and category name, inserts good rows with `ON CONFLICT DO NOTHING` and records
  bad rows with their row number and reason.
- **No duplicates:** the same file twice returns the same job (unique
  `(user, file_sha256)`), and every row carries `import_ref = sha256:row_number` with a
  unique partial index, so a retried job can't insert a row twice.
- Faster path for very large files: `COPY` into a staging table, then one
  `INSERT … SELECT … ON CONFLICT DO NOTHING`.

### Other things I would add with more time
- A GitHub Actions workflow running ruff and pytest with Postgres and Redis services.
- Live exchange rates from a provider (the fill script's rate function is the only place
  to change), run daily by the worker.
- Metrics and structured logs (request id, user id) and alarms on rate-limiter and
  idempotency fallbacks.

## Use of AI tools

I built this with **Claude Code** (Anthropic) as a pair programmer, and I am accountable
for every line.

- **Planning:** the implementation plan in `docs/PLAN.md` (slice order, schema, the
  approach to each hard feature) was drafted with Claude and revised by me.
- **Code and tests:** each slice — foundation, migrations, test harness, auth, rate limit,
  CRUD, listing, ETag, idempotency, report, storage client, receipts — was drafted by
  Claude, then reviewed by me before commit. Design choices were mine after discussing
  the options, for example: one migration per table, and no table beyond the brief's
  except `refresh_tokens` and `pending_s3_deletions`; exchange rates from a re-runnable
  script instead of a migration; session-checked access tokens instead of a
  `token_version` column; `limits` instead of hand-written Lua or slowapi; idempotency in
  Redis only; a storage interface with an S3 implementation; failed S3 deletes in a table
  retried by a script; skipping budget alerts and the CSV import to finish receipts.
- **Docs:** this README, `CLAUDE.md` and the Postman collection were drafted with Claude
  and edited by me.
- I did not use AI-generated code I couldn't explain; the request lifecycles of each slice
  were walked through before committing.