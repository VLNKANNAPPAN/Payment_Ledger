# Payment Ledger

A small, correctness-first payment ledger API built with FastAPI, PostgreSQL, SQLAlchemy 2.0, and Docker Compose.

It demonstrates three production-critical patterns:

- database-enforced idempotency under concurrent client retries;
- immutable, balanced double-entry ledger records;
- transactional outbox delivery with a polling worker.

## Architecture

```text
Client
  │  POST /payments
  ▼
FastAPI API ── one PostgreSQL transaction ──┐
  │                                         ├── payments
  │                                         ├── ledger_entries
  │                                         └── outbox_events
  │
  └──────────────────── polling worker ──> log / future webhook receiver
```

## Guarantees

- `payments.idempotency_key` has a PostgreSQL `UNIQUE` constraint. Concurrent duplicate requests produce exactly one payment.
- Reusing a key with different payment details returns `409 Conflict`.
- Every successful payment writes exactly one debit entry and one credit entry sharing one ledger transaction ID.
- Ledger rows are append-only; PostgreSQL rejects updates and deletes.
- Payment state, ledger rows, and a pending outbox event commit or roll back together.
- Outbox delivery is at-least-once. Consumers must deduplicate by event ID; duplicate delivery never creates new ledger entries.

## API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/payments` | Create a payment or safely return an existing idempotent payment. |
| `GET` | `/payments/{payment_id}` | Fetch a payment. |
| `GET` | `/payments/{payment_id}/events` | Fetch its outbox events. |

Example request:

```json
{
  "idempotency_key": "checkout-1001",
  "amount": "12.50",
  "currency": "USD",
  "source_account": "customer:alice",
  "destination_account": "merchant:books"
}
```

The first submission returns `201 Created`; a matching retry returns `200 OK` with the same payment. A retry using the same key with changed payment details returns `409 Conflict`.

## Run with Docker

```powershell
docker compose up --build
```

The API is available at `http://localhost:8000`. PostgreSQL is exposed on host port `5433` to avoid conflicts with a local PostgreSQL installation on `5432`.

```powershell
$body = @{ idempotency_key = 'demo-1'; amount = '12.50'; currency = 'USD'; source_account = 'customer:alice'; destination_account = 'merchant:books' } | ConvertTo-Json
Invoke-WebRequest http://localhost:8000/payments -Method Post -ContentType application/json -Body $body
```

Stop the local stack with:

```powershell
docker compose down
```

## Local development and tests

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

The unit suite is fast and does not require Docker. To run PostgreSQL integration tests, including the 200-request concurrency proof:

```powershell
docker compose up -d db
$env:RUN_POSTGRES_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -m integration -q
```

The integration suite proves:

- 200 simultaneous requests sharing one key produce one payment, two balanced ledger entries, and one outbox event;
- a failure before commit leaves no payment, ledger, or outbox records;
- a delivered outbox event is not processed again by the polling worker.

## Design decisions

**Why a database constraint for idempotency?** Two requests can both observe “no payment exists” before either inserts. The unique index lets PostgreSQL decide the race atomically.

**Why an immutable double-entry ledger?** History stays auditable. A correction is a new compensating entry, and account balances can always be derived from the ledger rather than trusted as mutable state.

**Why a transactional outbox?** It prevents a crash from committing money without recording a delivery event, or emitting an event for a payment that later rolls back.

**Why polling?** One local worker and a few-second delivery target do not justify a broker or PostgreSQL `LISTEN/NOTIFY`. At higher throughput, lower latency, or multiple independent consumers, introduce dedicated messaging infrastructure.

**Why READ COMMITTED?** The unique constraint and the one-transaction write boundary provide the required correctness. `SERIALIZABLE` everywhere would add retryable transaction failures without solving a problem this design has.
