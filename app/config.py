import os

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5433/payment_ledger"
)
OUTBOX_POLL_SECONDS = float(os.getenv("OUTBOX_POLL_SECONDS", "2"))
