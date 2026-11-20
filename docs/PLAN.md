s# Expense Tracker API — Implementation Plan

## Context

This is the Ahya 48-hour backend assessment. The spec is in `~/Downloads/Expense_tracker.pdf`. We are building an async FastAPI expense tracker that handles multiple currencies and has five hard features:

- idempotency
- optimistic locking
- outbox-based budget alerts
- streaming CSV import
- S3 receipts

The brief also requires at least 30 pytest tests with 85% or more coverage, a Postman collection, and a README with AWS deployment notes and short answers. Reviewers grade quality over completeness, then hold a live review call where the user must explain every line.

**How we work:** one slice at a time. Each slice is code, its tests, a walkthrough of the request lifecycle, then the user's review, then a commit. Nothing moves to the next slice without that review.

**Starting point:** an empty PyCharm project with no application code.

**Decisions already made:**
- uv for dependencies, with Python 3.12 pinned.
- Layered layout: controllers → services → models, fully async.
- arq for background workers.
- argon2 for password hashing.
- This plan is copied to `docs/PLAN.md` in the repo.
- AI use is disclosed in the README.
- The author writes every commit, after reviewing each slice.

## Conventions

- `main.py` calls `create_application()` in `app/core/application.py`. It registers exception handlers and routers, mounted at `/api/v1` from `routes/api/v1.py`.
- Controllers live in `app/controllers/api/v1/<feature>_controller.py`, one `APIRouter` each, and stay thin. They call service classes in `app/services/<feature>_service.py`, which call SQLAlchemy models in `app/models/`.
- Pydantic models go in `app/schemas/<feature>_schemas.py`. Settings live in `app/core/config.py`. Migrations live in `database/migrations/`.
- Exceptions share one `AppError` root (`app/exceptions/base.py`).

**Technology choices, to explain in reviews:**
- `AsyncSession` with asyncpg, not a sync driver: the brief requires a fully async app.
- arq rather than Celery: arq jobs are `async def` and reuse the async data layer.
- argon2-cffi for password hashing (maintained, argon2id).
- aiobotocore rather than boto3: async S3 calls.
- Successful responses are **not** wrapped in an `ApiResponse` envelope, because the brief fixes the response shapes (for example `{"items", "next_cursor"}`). Only errors use the shared envelope.

## Target layout

```
main.py                         # app = create_application()
app/core/        config.py, database.py (async engine, get_db), redis.py, security.py (jwt/argon2),
                 auth.py (get_current_user), errors.py (handlers), application.py
app/exceptions/  base.py (AppError + NotFound/Conflict/PreconditionFailed/PreconditionRequired/
                 Unauthorized/RateLimited/BadRequest)
app/models/      user, refresh_token, category, expense, exchange_rate, idempotency_key, outbox_event,
                 notification, import_job, import_error, pending_s3_deletion
app/schemas/     auth_, category_, expense_, report_, import_, notification_, receipt_schemas.py
app/services/    auth, rate_limit, category, expense, idempotency, currency (shared conversion SQL),
                 report, budget_alert, csv_import, receipt _service.py
app/contracts/   storage_client.py   # StorageClient ABC (the storage interface)
app/clients/storage/ s3_storage_client.py  # S3StorageClient: ALL S3 code (brief requirement)
app/workers/     settings.py (arq WorkerSettings + cron), outbox_relay.py, csv_import.py, s3_cleanup.py
app/controllers/api/v1/  auth, category, expense, report, notification, import, receipt _controller.py
routes/api/v1.py
database/migrations/  (alembic init -t async)
tests/           conftest.py, api/…, workers/…, unit/…
docker-compose.yml, Dockerfile, .env.example, postman/, docs/PLAN.md, README.md
```

## Cross-cutting designs (fixed once and reused)

- **Error envelope:** every error is `{"error": {"code", "message", "details": []}}`.
  - Handlers cover `AppError`, `RequestValidationError` (422, with field details), **Starlette's** `HTTPException` (not FastAPI's, so 404s on unknown routes and 405s also get the envelope), and a generic 500 that doesn't leak internals.
  - `RateLimited` sets `Retry-After`.
- **Money:** amounts are `Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]` and stored as `NUMERIC(14,2)`. They are serialized as strings. Exchange rates are `NUMERIC(20,10)`.
  - **Verify early:** check how Pydantic v2 parses a JSON number like `10.123` into `Decimal`, and test both JSON numbers and strings.
- **Currencies:** a `Currency` StrEnum of PKR, AED, USD and EUR, matching the seeded rates.
- **IDs:** UUID primary keys, so IDs can't be guessed. The keyset tiebreak on `id` still works.
- **Data access:** every query filters on `user_id`, so another user's rows simply don't exist for the caller and the API returns 404.
- **Sessions:** `get_db` yields an `AsyncSession` with `expire_on_commit=False`. Each request runs one transaction, and the service commits.
- **Single write path for expenses:** create, update, delete and import all go through `ExpenseService`. In one transaction it can also write the outbox event (slice 10) and the pending S3 deletion (slice 13), so those plug into this path instead of rewriting it. Idempotency (slice 8) wraps the create from outside, in Redis.
- **Rate availability rule:** an expense is accepted (through the API or an import) only if a rate exists on or before `spent_on` for both its currency and the user's base currency. Otherwise it returns 422 `no_exchange_rate`. This keeps every expense convertible, so the report's inner `LATERAL` joins can never silently drop rows.
- **Currency conversion:** `currency_service` holds one SQL fragment. It uses a `LATERAL` join to find the latest rate *on or before* `spent_on`, for both the expense currency and the base currency. The amount in the base currency is `amount × rate_src / rate_base`, where `rate_to_usd` is how many USD one unit of the currency is worth. The report, `GET /categories` and the budget-alert worker all reuse it.

## Slices

Every slice ends the same way: `uv run pytest` passes, ruff is clean, its Postman requests are added to `postman/expense-tracker.postman_collection.json`, any notes are added to the README (including AI use), then the slice is reviewed and committed.

### 0. Repo hygiene
1. Run `git init` and set a repo-local `user.name` and `user.email`.
2. Add a `.gitignore` covering `.env*` (but not `.env.example`), `.idea/`, `.venv/`, `__pycache__`, `.pytest_cache`, `.coverage` and `htmlcov/`.
3. Add this plan as `docs/PLAN.md`.
4. Make the first commit and check its author with `git log --format='%an <%ae>'`.
5. The PyCharm sample `main.py` is replaced in slice 1.

### 1. Foundation
- `uv init` and `.python-version` (3.12).
- Dependencies: fastapi, `uvicorn[standard]`, pydantic, pydantic-settings, email-validator, `sqlalchemy[asyncio]`, asyncpg and redis.
- Dev dependencies: ruff.
- `config.py`, `database.py`, `redis.py`, `errors.py` and `application.py`.
- `/health` for liveness and `/ready`, which runs `SELECT 1` and a Redis `PING`.
- `docker-compose.yml` runs only the app containers. Postgres and Redis run on the host, reached through `host.docker.internal`, so Settings take connection parts (`DB_HOST`, `REDIS_HOST`, …) and compose overrides only the hosts.
- A multi-stage `Dockerfile` (uv sync, slim image, non-root user) and `.env.example`.
- Update CLAUDE.md with the real commands.

### 2. Models and migrations
- `alembic init -t async`. `env.py` reads the URL from `Settings`.
- **One migration per table**, for only the four tables in the brief's Data section, chained in foreign-key order: `users` → `categories` → `expenses` (which also enables pg_trgm) → `exchange_rates`. Migrations hold schema only.
- **Every other table arrives in its own migration, in the slice whose code uses it**, so the schema grows feature by feature in the git history:

  | Slice | Migration adds | Brief requirement |
  |---|---|---|
  | 4 | `refresh_tokens` | Rotation, reuse detection, logout; access tokens are checked against their session |
  | 10 | `outbox_events`, `notifications` | Both named in 2.3 |
  | 12 | `import_jobs`, `import_errors`, `expenses.import_ref` | Job status/counts, errors list, no duplicates on re-upload |
  | 13 | `pending_s3_deletions` | Failed S3 deletes "cleaned up later, not forgotten" |

- **Indexes in this slice, each with its query and reason written in the README:**

  | Index | Serves |
  |---|---|
  | `expenses (user_id, spent_on DESC, id DESC)` | Keyset listing and month ranges |
  | `expenses (user_id, category_id, spent_on)` | Report and budget totals |
  | GIN trigram on `expenses.description` | The `q` ILIKE search |
  | Primary key `exchange_rates (currency, valid_on)` | The "latest rate on or before" lookup |
  | Unique `categories (user_id, lower(name))` | Returns 409 on duplicates |
  | Unique `categories (id, user_id)` + composite FK from `expenses` | The database itself refuses an expense pointing at another user's category |

  Later slices' indexes are listed with their migrations.
- `migrate` service in Compose (same image, runs `alembic upgrade head`); `api` starts only after it succeeds.

- **Exchange rates come from a script, not a migration:** `uv run python -m app.scripts.fill_exchange_rates` fills every missing (currency, day) for PKR, AED, USD and EUR across the last 90 days, as the brief asks. A migration runs once per database, but rates grow daily. The script is idempotent (`ON CONFLICT DO NOTHING`), so re-running it fills in the latest days and any gaps without touching existing rows. It uses deterministic generated values with USD = 1. The Compose `migrate` service runs it after `alembic upgrade head`, and slice 10 can schedule the same function as a daily arq job. Missing-rate days exist only in test fixtures.
- `app/scripts/` holds maintenance and data scripts like this one, each run as `python -m app.scripts.<name>`.

### 3. Test harness
This comes after slice 2 because the harness needs the migrations and tables.
- Dev dependencies: pytest, pytest-asyncio, pytest-cov and httpx.
- Configure pytest in `pyproject.toml`:
  - `asyncio_mode=auto` and session loop scope.
  - `addopts = --cov=app --cov-report=term-missing`.
  - The 85% threshold goes in `[tool.coverage.report] fail_under`, or is passed as `--cov-fail-under=85` in CI. It is **not** in addopts, so `pytest -k …` runs don't fail on coverage.
- `conftest.py` setup:
  - Create the `expense_tracker_test` database if it's missing.
  - Run Alembic `upgrade head` once per session from a **sync** session fixture, or through `asyncio.to_thread`. The async `env.py` calls `asyncio.run()`, which fails inside a running loop. Running it also tests the migrations.
  - Use an engine with `NullPool`.
  - **TRUNCATE every table except `alembic_version` before each test**, then `FLUSHDB` on Redis db 15. We truncate instead of rolling back a shared transaction because the `asyncio.gather` race tests need real, separate connections.
  - An `httpx.AsyncClient(transport=ASGITransport(app))` client.
  - Factory fixtures: `user`, `auth_headers`, `category`, `expense` and `rates`. `rates` takes explicit days, so a test can leave gaps.
- The first tests: `/health`, `/ready`, the error-envelope shape (including the 404 for an unknown route), and that migrations apply cleanly.

### 4. Auth and login rate limit
- Dependencies: pyjwt, argon2-cffi and `limits`.
- **Migration:** `refresh_tokens` only. It has a unique `token_hash`, an index on `user_id` for revoke-all, and a partial index on `family_id WHERE used_at IS NULL AND revoked_at IS NULL` for the session check. **No column is added to `users`.**
- **Passwords:** argon2id, run in `asyncio.to_thread` because hashing is CPU-bound and would block the event loop. An unknown email still runs a dummy verification, so timing doesn't reveal which emails exist.
- **Refresh token:** an opaque random string (256 bits) valid for 7 days. Only its SHA-256 is stored, along with `family_id`, `expires_at`, `used_at` and `revoked_at`.
- **Sessions:** a login starts a session, which is a refresh-token family. Each rotation adds a row to the same family, so exactly one row per family is live (not used, not revoked).
- **Access token:** a 15-minute JWT with `sub`, `sid` (the family), `type=access`, `iat` and `exp`. `get_current_user` accepts it only if the user exists **and** its family still has a live refresh token, in one query. That is what makes "revoke all of that user's tokens" reach the stateless access tokens immediately, without a `users.token_version` column.
- **Refresh:**
  1. `SELECT … FOR UPDATE` on the token hash.
  2. If the token was **already used**, it is a reuse, i.e. theft: revoke every refresh token the user has. With no live family left, all of their access tokens die too. **This is committed before the 401 is raised**, so the error doesn't roll the revocation back.
  3. A revoked or expired token gets a plain 401, with no cascade.
  4. Otherwise mark the token used and issue a new pair in the same family.

  Because of the row lock, two concurrent refreshes with the same token behave like a reuse. That trade-off goes in the README.
- **Logout:** revokes the presented token's whole family and returns 204. That session's access token stops working immediately; other sessions are untouched. It's idempotent, and an unknown token returns 401.
- **Rate limit (`rate_limit_service`, on `limits.aio`):**
  - slowapi isn't used. It calls the synchronous `limits` API (blocking Redis calls in async code), can't key on a JSON body field, and counts every request, not only failures.
  - `MovingWindowRateLimiter` on `limits`' async Redis storage, with `implementation="redispy"`, so it reuses our redis-py instead of adding coredis. Limit: 5 per 900 s, keyed by `sha256(lower(email))`.
  - **Before** the password is checked, `hit()` checks and records the attempt atomically, so parallel guesses can't exceed 5, and it stays correct with several API servers. Over the limit returns 429 with `Retry-After` from the window stats.
  - Attempts count even for unknown emails. A successful login calls `clear()`, so in effect only failures count.
  - If Redis is down, `StorageError` is caught and the limiter fails open with a warning.
- ✅ Must-have tests: *refresh token used twice → all tokens revoked, including every session's access token*; *6th failed login → 429 with Retry-After*.
- Other tests: duplicate email → 409 (case-insensitive), invalid input → 422, wrong password and unknown email give identical 401s, rotation, expired or unknown refresh token → 401, logout ends only its own session, forged/expired/wrong-type access token → 401, rate limit per email, reset on success, parallel guesses, fail-open.

### 5. Categories and expense CRUD
- `POST /categories`; duplicate names return 409.
- `POST /expenses`, `GET /expenses/{id}` and `DELETE /expenses/{id}`.
- A `category_id` the user doesn't own returns 404.
- `GET /categories` only lists categories in this slice; the spend figures come in slice 9.
- ✅ Must-have tests: *another user's expense → 404* on GET and DELETE (slice 7 adds PATCH); *amount 0, negative, or 3 decimal places → 422*.
- Other tests: no exchange rate for `spent_on` → 422.

### 6. Listing with keyset pagination
- Filters: `category_id`, `date_from`, `date_to`, `min_amount`, `max_amount`, `currency` and `q` (ILIKE, served by the trigram index).
- The cursor is base64url JSON of `{spent_on, id}`. The query is `WHERE (spent_on, id) < (:d, :id) ORDER BY spent_on DESC, id DESC LIMIT n+1`.
- `next_cursor` is `null` on the last page. A malformed cursor returns 400.
- ✅ Must-have test: page 1 → insert rows that sort both before and after the cursor → page 2. No original row is duplicated or missing.

### 7. ETag and If-Match (2.2)
- `GET /expenses/{id}` returns `ETag: "<version>"`.
- `PATCH` requires `If-Match`; a missing header returns 428.
- The update is one statement: `UPDATE … SET …, version = version + 1, updated_at = now() WHERE id = :id AND user_id = :u AND version = :v RETURNING *`.
- If no row comes back, an existence check decides between 404 and 412. The write itself is still one atomic statement.
- The response carries the new ETag.
- ✅ Must-have test: *two parallel PATCHes with the same ETag via `asyncio.gather` → exactly one 200 and one 412*.
- Other tests: PATCH on another user's expense → 404, missing `If-Match` → 428.

### 8. Idempotency-Key (2.1), Redis only
- **No table and no new columns.** One Redis key per (user, Idempotency-Key), `idempotency:{user_id}:{sha256(key)}`, holding JSON: `{"state":"processing","hash":H}`, which becomes `{"state":"done","hash":H,"status","body","headers"}` once the request succeeds.
- **The race is handled by a Redis lock:** `SET … NX EX 86400`. `NX` is atomic, so exactly one request claims a key, across every API server. A request that finds the key taken never runs the create:
  - **different body hash** → 422 `idempotency_key_reused`
  - **same hash, still processing** → 409 `request_in_progress`
  - **same hash, done** → replays the stored first response (status, body, ETag) with `Idempotent-Replayed: true`

  These are the codes in the IETF draft on the `Idempotency-Key` header.
- **Expiry:** `EX 86400` on the claim, and the completion uses `KEEPTTL`, so the 24 hours run from first use and Redis deletes the key itself. No cleanup job.
- **Request hash:** SHA-256 of the canonical validated body. Key order doesn't matter, and the amount is normalised to 2 decimal places, so `12.5` and `"12.50"` count as the same request.
- **Failure handling, designed so a duplicate expense is impossible:**
  - If the create fails (for example a 404 category), the claim is released and the client can retry.
  - If the process crashes after the commit but before `done` is stored, the key stays `processing` and retries get 409 until it expires. That's a stuck retry, never a duplicate.
  - If Redis is down, requests **with** the header get 503 (fail closed); requests without it are unaffected.
- **README:** why Redis rather than a DB unique constraint. Native TTL expiry, the exact first response replayed, no extra schema, and it's the lock the brief itself offers. The trade-offs are the crash window and the dependency on Redis.
- ✅ Must-have test: *two parallel POSTs with the same key via `asyncio.gather` → one expense*. Also tested with 10 in parallel.
- Other tests: replay, an equivalent body counts as the same, a different body → 422, keys scoped per user, no header → no idempotency, a failed request releases its key, the 24-hour TTL then the key is reusable, in progress → 409, Redis down → 503 only when the header is sent, invalid header → 422.

### 9. Monthly report and category spend
- `GET /reports/monthly?year&month` runs **one SQL statement**:
  1. A `conv` CTE uses the shared `LATERAL` rate lookups.
  2. Categories are cross-joined with the previous and current month, then `LEFT JOIN conv` and `GROUP BY`.
  3. `LAG(total) OVER (PARTITION BY category ORDER BY month)` gives last month's total.
  4. The output has, per category: total, budget, `percent_used` and the change from last month (absolute and %). Values are rounded to 2 decimal places only at the end.
- `GET /categories` adds this month's spend vs budget through the same conversion.
- ✅ Must-have test: expenses in mixed currencies, plus one dated on a day with no rate, so it falls back to the previous day's rate. Totals are checked against Decimal values computed by hand.
- Other tests: the month-over-month change, and that a 404 is never returned for categories the user has no spend in.

### 10. Budget alerts through the outbox (2.3)
- Dependency: arq.
- **Migration:** `outbox_events` (partial index `(id) WHERE processed_at IS NULL` for polling) and `notifications` (unique `(category_id, period, level)` for exactly-once alerts).
- **Producer:** `ExpenseService` writes `outbox_events(type='expense.changed', payload={user_id, category_ids, months})` in the same transaction as every create, update or delete. Old and new categories and months are included on a PATCH.
- **Relay (`app/workers/outbox_relay.py`, an arq cron every 2s):**
  1. Claim a batch with `SELECT … WHERE processed_at IS NULL ORDER BY id LIMIT 500 FOR UPDATE SKIP LOCKED`, so two workers never take the same rows.
  2. Coalesce the batch to distinct (user, category, month) pairs.
  3. Compute the current total through the shared conversion.
  4. For each level crossed (80, 100), run `INSERT INTO notifications … ON CONFLICT DO NOTHING`.
  5. Mark the events processed and commit.

  A crash before commit means the batch runs again, and the unique constraint makes that harmless.
- `GET /notifications` lists the user's notifications, newest first, with keyset pagination.
- **README policy for edits and deletes:** totals are recomputed from current state, not from deltas. If an edit lowers the total, alerts already sent stay as history and aren't retracted. The same level never fires twice in a month. If an edit raises the total past a level, the alert fires.
- The relay function is called in-process in tests, which covers it for coverage.
- ✅ Must-have test: *the same event processed twice → one notification*.
- Other tests: crossing 80% only, crossing 100%, two relays running concurrently, and a category with no budget producing no alert.

### 11. S3 storage module
This comes before CSV import because the import uploads to S3.
- Dependencies: aiobotocore (aioboto3 dropped: the only release compatible with the pinned botocore was from 2019), and `moto[server]` for dev.
- **Checked:** aiobotocore works against moto's `ThreadedMotoServer` via `endpoint_url` (upload a file object, stream it back in chunks, delete). In-process `mock_aws` is unreliable with aiobotocore.
- An interface plus an implementation: the `StorageClient` ABC in `app/contracts/storage_client.py` (`upload_fileobj`, `download_to_file`, `delete`, `ObjectNotFound`); `S3StorageClient` in `app/clients/storage/s3_storage_client.py` is the only code that touches S3; callers get it via the `get_storage_client` dependency. Receipts would add `presign_post`, `head`, `read_first_bytes` and `presign_get` to the contract.
- Settings: `S3_ENDPOINT_URL` for server-side calls, plus a separate `S3_PUBLIC_ENDPOINT_URL` used only for signing. A URL signed against an internal host (e.g. `host.docker.internal`) wouldn't work from a client.
- A moto server fixture in `conftest.py`, and unit tests for each storage function.

### 12. CSV import (2.4)
- **Migration:** `import_jobs` (unique `(user_id, file_sha256)`), `import_errors` (index `(job_id, row_number)`), and `expenses.import_ref` with a unique partial index `(user_id, import_ref) WHERE import_ref IS NOT NULL`.
- **Upload:** `POST /expenses/import` takes multipart. It streams the file in chunks while computing SHA-256, then calls `storage.upload_fileobj` to put it at `imports/{user_id}/{job_id}.csv` in S3.
  - The file goes to S3 rather than local disk because, with several servers, the worker may run on a different machine.
  - The import job is inserted with a unique `(user_id, file_sha256)`. Uploading the same file again returns the existing `job_id`.
  - The endpoint returns 202, and an arq job is enqueued.
- **Worker:**
  1. Stream the object to a temporary file in chunks.
  2. Iterate a `csv.reader` in batches of 1,000 rows, each batch read through `asyncio.to_thread`. The file is never fully in memory.
  3. Validate each row: date, amount (same Decimal rules), currency, and a category name looked up in a preloaded name → id dict.
  4. Insert good rows in batches with `ON CONFLICT (user_id, import_ref) DO NOTHING`, where `import_ref = sha256:row_no`. That makes a crashed job safe to retry.
  5. Bad rows go into `import_errors(job_id, row_number, reason)`.
  6. After each batch, update `rows_done` and `rows_failed` and write one outbox event per affected category-month.
  7. Stop at 100,000 rows and mark the job failed.
- `GET /imports/{job_id}` returns status, counts and the first N errors.
- The API stays fast because all parsing runs in the worker process.
- The README notes the upgrade path: `COPY` into a staging table, then `INSERT … SELECT … ON CONFLICT`.
- ✅ Must-have test: *a mix of good and bad rows gives the right counts and errors*.
- Other tests: the same file twice creates no duplicates; retrying a job is idempotent; an unknown category is reported as an error.

### 13. Receipts on S3 (2.5) — built
- `StorageClient` and `S3StorageClient` gained `presign_upload` (a pre-signed POST), `head`, `read_first_bytes` and `presign_download`. Custom endpoints use path-style addressing and SigV4.
- **Upload URL:** a pre-signed POST, valid 5 minutes, for the server-chosen key `receipts/{user_id}/{expense_id}/{uuid}.{jpg|png|pdf}`. The policy pins the key and content type and sets `content-length-range` [1 B, 5 MB].
- **Confirm:**
  1. The key must match this user and expense, with a uuid name and an allowed extension. Otherwise 422 `invalid_receipt_key`.
  2. `head`: a missing object is 404 `receipt_not_uploaded`; over 5 MB is deleted and returns 422.
  3. Read the first 8 bytes and match the magic bytes against the extension. On a mismatch, delete the object and return 422 `invalid_receipt_type`.
  4. Save `receipt_key` and bump `version`. A replaced receipt is staged for deletion.
- **Download:** a pre-signed GET valid 15 minutes. With no receipt it's 404 `no_receipt`.
- **Deletion, decided: table + script rather than an arq cron.**
  - `pending_s3_deletions` (its own migration) is written in the same transaction as the expense delete or receipt replacement. After the commit the API tries the S3 delete and removes the row on success.
  - On failure the row keeps the attempt count, the last error and the next attempt time (backoff from 1 minute, doubling up to 1 hour).
  - `python -m app.scripts.cleanup_s3_deletions` retries due rows with `FOR UPDATE SKIP LOCKED`. It runs on a schedule: cron locally, an EventBridge-scheduled ECS task on AWS.
- ✅ Tests (21) against moto, with real pre-signed POST uploads and downloads:
  - an `.exe` renamed to `.png` → 422 and the object is deleted
  - a PDF uploaded under a `.png` key → 422; over 5 MB → 422
  - confirm without an upload → 404; foreign or traversal keys → 422
  - another user's expense → 404 on every endpoint; download with no receipt → 404
  - a replaced receipt is deleted; deleting the expense deletes its file
  - a failed S3 delete is kept with backoff, then cleaned up by the script
  - the cleanup script's entry point

### 14. Wrap-up
- Complete the Postman collection and an environment file, with no secrets in either.
- README:
  - Setup steps (Docker and local).
  - Architecture and the index rationale.
  - **The coverage number**.
  - What was skipped and how it would be built.
  - The AI-use disclosure, including that `docs/PLAN.md` was drafted with AI.
- AWS deployment notes:
  - ECS Fargate API and worker services, rolling zero-downtime deploys.
  - RDS in a private subnet, with 5432 open only from the app security group and the password from Secrets Manager.
  - ElastiCache and what happens to each feature if it fails.
  - The IAM task-role policy JSON: `s3:PutObject`, `s3:GetObject` and `s3:DeleteObject` on `bucket/receipts/*` and `imports/*`.
  - Expand/contract migrations, run as a one-off task before the deploy.
- The five short answers, written from the code we actually built.
- Optional, if there's time: GitHub Actions running ruff and pytest with service containers.

**If time runs short, cut in this order:** first the extra tests in slice 13, then the cleanup cron in slice 13, then all of slice 13 (receipts). Every must-have test stays.

## Verification

- **Each slice:**
  - With host Postgres and Redis running: `uv run alembic upgrade head`, then `uv run pytest`, which must be green, with coverage shown.
  - `uv run ruff check . && uv run ruff format --check .`.
  - Manually hit the slice's endpoints through Postman or the `/docs` UI.
- **Concurrency features:** run the `asyncio.gather` tests 20 times, to rule out a race that only fails sometimes: `for i in {1..20}; do uv run pytest -k parallel -q --no-cov || break; done`.
- **End to end at slice 14:**
  1. From a clean clone, `docker compose up --build`.
  2. Run the whole Postman collection with the Postman runner or the CLI.
  3. Confirm `pytest` reports 85% or more and the README number matches.
  4. Run `git log -p | grep -iE "secret|password|AKIA"` as a secret check.
