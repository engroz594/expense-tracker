# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An async multi-currency expense tracker REST API, built for a 48-hour backend assessment. The spec is in `docs/PLAN.md`, and the work is done in the numbered slices listed there.

The brief's hard rules, which apply to all code:
- **Fully async.** No `time.sleep`, `requests` or sync DB drivers inside async code. Ruff's `ASYNC` rules enforce this.
- **Money is never a float.** Use `Decimal` in Python and `NUMERIC` in Postgres.
- **No secrets in the repo.** Only `.env.example` is committed.

## Commands

```bash
uv sync                                   # install deps (Python 3.12 is pinned in .python-version)
cp .env.example .env                      # then set DB_USER / DB_PASSWORD / DB_NAME
# Postgres and Redis run on the host (not in compose), e.g. `brew services start redis`
uv run alembic upgrade head              # apply migrations (schema only)
uv run python -m app.scripts.fill_exchange_rates   # fill missing rates for the last 90 days; idempotent, re-run any time
uv run python -m app.scripts.cleanup_s3_deletions  # retry S3 deletes that failed (run on a schedule); idempotent
uv run uvicorn main:app --reload          # dev server on :8000
docker compose up --build                 # `migrate` runs alembic, then `api` starts; both reach host DB/Redis via host.docker.internal

uv run alembic revision --autogenerate -m "<what>"   # new migration from model changes; always review it
uv run alembic check                      # fails if models and migrations have drifted
uv run alembic downgrade -1               # migrations must stay reversible

uv run ruff check . && uv run ruff format --check .   # lint + format check

uv run pytest                             # all tests + coverage report (needs host Postgres + Redis)
uv run pytest --cov-fail-under=85         # with the brief's coverage gate (CI)
uv run pytest tests/api/test_health.py::test_ready_is_ok_when_postgres_and_redis_respond --no-cov   # one test
```

### Test harness (`tests/conftest.py`)

- **Real PostgreSQL and Redis**, never SQLite. The harness forces `DB_NAME=expense_tracker_test` (overridable with `TEST_DB_NAME`) and `REDIS_DB=15` *before* importing the app, because the engine and Redis client are created at import time. It also refuses to run unless `DB_NAME` ends in `_test`.
- **Session setup:** creates the test database if it's missing (`DB_USER` needs `CREATEDB`), then runs `alembic upgrade head`.
- **Before every test:** `TRUNCATE` on every table and `FLUSHDB` on Redis. We truncate rather than roll back a transaction because the `asyncio.gather` race tests need separate, committed connections.
- **Anything that calls `asyncio.run()`** (Alembic's `env.py`) runs in a fresh thread, through `run_in_fresh_thread` or `asyncio.to_thread`.
- **Event loop:** one loop for the whole session, because the pooled connections are bound to it.
- **Fixtures:** `client` is an httpx `AsyncClient` over `ASGITransport(raise_app_exceptions=False)`, so 500s come back as responses. `db` is a session for arranging data and asserting on it. `register_user` and `login` are async factories, and `auth_headers` gives a ready Bearer header for a default user. Shared constants and `bearer(tokens)` live in `tests/helpers.py`. Import them from there, never from `conftest`.
- **Test secrets:** the harness sets a test-only `JWT_SECRET`, so tests don't depend on `.env`.
- **Coverage:** runs with `concurrency = ["greenlet", "thread"]`. SQLAlchemy async runs on greenlets, and without this setting coverage under-reports code that did execute.
- **Coverage gate:** the 85% threshold is passed on the command line, not set in config. pytest-cov would otherwise apply it to every run, including focused `-k` runs.

## Architecture

- `main.py` calls `app/core/application.py::create_application()`. That registers the exception handlers (`app/core/errors.py`), the health router at the root (`/health`, `/ready`) and `routes/api/v1.py` at `/api/v1`. Feature controllers are added to the v1 router.
- **Layers:** thin controllers (`app/controllers/`), then service classes (`app/services/`, where the business logic lives), then SQLAlchemy models (`app/models/`). Pydantic request and response models live in `app/schemas/`.
- **Database:** `app/core/database.py` holds one module-level async engine (asyncpg) and `SessionLocal` (`expire_on_commit=False`). `get_db` yields one session per request. Services call `commit`; anything uncommitted rolls back when the session closes.
- **Models and migrations:** all models subclass `app/models/base.py::Base`, whose metadata has a naming convention so constraint and index names are deterministic. Every model must be imported in `app/models/__init__.py`, or Alembic autogenerate won't see it. Models define **no `relationship()`**: async SQLAlchemy can't lazy-load, so queries state their joins explicitly. Currency columns use `currency_check()` against the `Currency` enum. `database/migrations/env.py` builds the URL from `Settings` (so `alembic.ini` has none) and runs through asyncpg; it calls `asyncio.run()`, so it can't be invoked from inside a running event loop. **A table is added in the slice whose feature uses it**, each in its own migration — see the table in `docs/PLAN.md` slice 2.
- **Redis:** one shared `redis.asyncio` client in `app/core/redis.py`, provided through the `get_redis` dependency.
- **Auth (sessions):** protect an endpoint with `user: User = Depends(get_current_user)` from `app/core/auth.py`.
  - A login starts a session, which is a refresh-token family in `refresh_tokens`.
  - The access JWT carries `sid` = `family_id`. `get_current_user` accepts the token only while that family has a live row (`used_at` and `revoked_at` both NULL), so revoking refresh tokens kills access tokens immediately. There is deliberately no `users.token_version`.
  - Refresh rotates the token: the old row gets `used_at` and a new row is added to the same family. A token presented with `used_at` already set means reuse, so every token of the user is revoked, **committed before** the 401 is raised. Logout revokes the whole family.
  - Password hashing (argon2) is CPU-bound, so it always goes through `asyncio.to_thread` (`app/core/security.py`).
- **Money:** request fields use `Money`, and response fields use `MoneyOut`, from `app/schemas/types.py`.
  - `Money` is a Decimal greater than 0 with at most 2 decimal places, enforced with a 422. JSON numbers are parsed from their text, so there's no float rounding.
  - `MoneyOut` always serialises as a string with 2 decimal places.
  - Never use `float`.
- **Ownership and expense writes:** every query filters on `user_id`, so another user's rows come back as 404, never 403. `CategoryService.get` is the shared "my category or 404" check.
  - Expense writes go through `ExpenseService`. `_stage_create` adds to the session and flushes **without committing**. Later features (the outbox event) add their rows to the same transaction before the one commit.
  - Every expense write calls `currency_service.ensure_rates_available`, for the expense currency and the user's base currency, so every stored expense can be converted.
- **Don't `rollback()` mid-request and then keep going.** A rollback expires every loaded object, including the `User` from `get_current_user`, which shares the request's session. The next attribute access then triggers a lazy load, which async SQLAlchemy forbids (`MissingGreenlet`). Rolling back right before raising is fine. Otherwise just raise, and the session's close rolls back.
- **Listing and pagination:** keyset pagination. Cursors are opaque base64url JSON holding the last row's sort key (`app/core/pagination.py`); a bad cursor is 400. The query is `WHERE (spent_on, id) < cursor ORDER BY spent_on DESC, id DESC LIMIT n+1`, where the extra row tells us whether a next page exists. Filters are a Pydantic query model (`ExpenseListQuery`, `extra="forbid"`). `q` uses ILIKE with `%`, `_` and `\` escaped.
- **Optimistic locking:** `ETag: "<version>"` on GET, POST and PATCH responses.
  - PATCH needs `If-Match`. A missing header or `*` is 428; a stale or malformed one is 412.
  - The version check is **only** inside `UPDATE … WHERE version = :v RETURNING`, never compared in Python. The SELECT before it exists only for 404 handling and validating a partial update.
- **Idempotency-Key (POST /expenses):** handled in Redis only, by `app/services/idempotency_service.py`. There is no table.
  - `SET NX EX 86400` claims the key; replays and conflicts are decided by the request fingerprint (`ExpenseCreate.canonical()`).
  - A different body is 422, a request still in progress is 409, and a completed one is replayed with `Idempotent-Replayed: true`.
  - A failed create releases the key. A crash after the commit leaves the key in `processing`: a stuck retry, never a duplicate.
  - If Redis is down, requests that send the header get 503 (fail closed). That's the opposite of the login limiter, which fails open.
- **Currency conversion:** done in SQL only, through `currency_service.CONVERTED_EXPENSES_CTE`, a CTE string spliced into larger `text()` queries.
  - Parameters: `:user_id`, `:base_currency`, `:period_start` (inclusive) and `:period_end` (exclusive).
  - Each expense is converted with `CROSS JOIN LATERAL` lookups of the latest rate on or before `spent_on`.
  - Values are rounded **once**, at the end of the outer query, never per expense.
  - Both the monthly report (`report_service.py`, a single statement using `LAG`) and `GET /categories` (this month's spend, UTC) use it. Reuse it rather than converting in Python.
- **Storage:** code that stores files depends on the `StorageClient` ABC in `app/contracts/storage_client.py` (async `upload_fileobj`, `download_to_file` and `delete`), never on S3 directly. A missing object raises `ObjectNotFound` from `app/exceptions/storage.py`: a plain `Exception`, not an `AppError`, because the calling service decides what it means for the request. Get one through the `get_storage_client` dependency.
  - `S3StorageClient` in `app/clients/storage/s3_storage_client.py` (aiobotocore) holds **all** S3 code. Never import a boto client anywhere else.
  - It reads `settings.S3_*` at call time. Keys are used only if configured; otherwise botocore's default chain applies (the IAM role on ECS).
  - A missing key raises `ObjectNotFound`, and so does a 403: without `s3:ListBucket`, S3 returns 403 for missing keys.
  - Local file I/O around S3 goes through `asyncio.to_thread`.
  - Tests use moto in **server mode**: the session fixture `moto_s3_endpoint` patches the settings, and the `s3_bucket` fixture gives each test a fresh empty bucket. In-process `mock_aws` can't intercept aiobotocore's aiohttp calls. Real S3 is never called.
- **Receipts:** `app/services/receipt_service.py`.
  - The upload URL is a pre-signed POST for a server-chosen key, `receipts/{user}/{expense}/{uuid}.{ext}`; its policy pins the key, the content type and 1 B–5 MB.
  - Confirm re-validates everything: the key must be issuable for that expense, `head` checks existence and size, and the first bytes must match the extension's signature. A bad file is deleted and the request gets a 422. Attaching a receipt bumps `version`.
  - **Never delete an S3 object directly after a DB change.** Stage a `pending_s3_deletions` row in the same transaction (`S3DeletionService.stage`, or the insert in `ExpenseService.delete`), commit, then call `delete_now`. A failure leaves the row for `app/scripts/cleanup_s3_deletions.py`, which retries with exponential backoff.
- **Login rate limit:** `app/services/rate_limit_service.py` uses `limits.aio` (`MovingWindowRateLimiter`, Redis storage with `implementation="redispy"`; there's no coredis and no slowapi).
  - `hit()` runs *before* the password check, so check and record happen in one atomic step. `clear()` runs on success.
  - It fails open on `StorageError`.
  - It's a dependency, `get_login_rate_limiter`, so tests can override it.
- **Errors:** raise `AppError` subclasses from `app/exceptions/` (`NotFound`, `Conflict`, `PreconditionFailed`, `RateLimited(retry_after_seconds)`, and so on). Never build error responses by hand. Every error, including framework 404/405s and validation 422s, leaves as `{"error": {"code", "message", "details"}}`. The handler is registered on Starlette's `HTTPException`, not FastAPI's, so routing errors also get the envelope.
- **Config:** pydantic `Settings` in `app/core/config.py`, read from env or `.env`. Connections are configured as parts (`DB_HOST`, `DB_PORT`, `DB_USER`, … and `REDIS_*`); `settings.database_url` builds the URL with `URL.create`. Credentials and `DB_NAME` have no defaults, so a missing value fails at startup. Only add settings for code paths that actually run.
- **Response shapes:** successful responses return the shapes the brief requires directly. They are not wrapped in a response envelope.