r"""Run with: docker compose up -d db; $env:RUN_POSTGRES_TESTS='1'; .\.venv\Scripts\python -m pytest -m integration"""
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select

if os.getenv("RUN_POSTGRES_TESTS") != "1":
    pytest.skip("Set RUN_POSTGRES_TESTS=1 to run against Docker PostgreSQL", allow_module_level=True)
os.environ.setdefault("OUTBOX_POLL_SECONDS", "3600")  # worker delivery is exercised explicitly below

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app, initialize_database  # noqa: E402
from app.models import LedgerEntry, OutboxEvent, Payment  # noqa: E402
from app.schemas import CreatePaymentRequest  # noqa: E402
from app.services import create_payment  # noqa: E402
from app.worker import deliver_pending_events  # noqa: E402


@pytest.fixture(scope="module")
def api_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("API did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def reset_database():
    Base.metadata.drop_all(engine)
    initialize_database()
    yield


@pytest.mark.integration
def test_200_concurrent_same_key_creates_one_balanced_payment(api_url):
    payload = {
        "idempotency_key": "headline-concurrency-key",
        "amount": "12.50",
        "currency": "USD",
        "source_account": "customer:1",
        "destination_account": "merchant:1",
    }

    limits = httpx.Limits(max_connections=250, max_keepalive_connections=0)
    with httpx.Client(timeout=30, limits=limits, trust_env=False) as client:
        def send_request() -> int:
            return client.post(f"{api_url}/payments", json=payload).status_code

        with ThreadPoolExecutor(max_workers=200) as executor:
            statuses = list(executor.map(lambda _: send_request(), range(200)))

    assert statuses.count(201) == 1
    assert statuses.count(200) == 199
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        entries = list(session.scalars(select(LedgerEntry)))
    assert len(entries) == 2
    assert sum((entry.debit for entry in entries), Decimal("0")) == sum(
        (entry.credit for entry in entries), Decimal("0")
    )


@pytest.mark.integration
def test_payment_ledger_and_outbox_commit_together(api_url):
    response = httpx.post(
        f"{api_url}/payments",
        json={
            "idempotency_key": "atomicity-key",
            "amount": "5.00",
            "currency": "USD",
            "source_account": "customer:2",
            "destination_account": "merchant:2",
        },
        timeout=10,
    )
    assert response.status_code == 201
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
        assert session.scalar(select(func.count()).select_from(LedgerEntry)) == 2
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1


@pytest.mark.integration
def test_failure_before_commit_rolls_back_payment_ledger_and_outbox():
    request = CreatePaymentRequest(
        idempotency_key="forced-rollback-key",
        amount="9.00",
        currency="USD",
        source_account="customer:3",
        destination_account="merchant:3",
    )
    def simulate_crash() -> None:
        raise RuntimeError("simulated crash")

    with SessionLocal() as session:
        with pytest.raises(RuntimeError, match="simulated crash"):
            create_payment(session, request, before_commit=simulate_crash)

    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 0
        assert session.scalar(select(func.count()).select_from(LedgerEntry)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


@pytest.mark.integration
def test_outbox_worker_marks_once_and_does_not_redeliver(api_url):
    response = httpx.post(
        f"{api_url}/payments",
        json={
            "idempotency_key": "outbox-idempotency-key",
            "amount": "7.00",
            "currency": "USD",
            "source_account": "customer:4",
            "destination_account": "merchant:4",
        },
        timeout=10,
    )
    assert response.status_code == 201
    assert deliver_pending_events() == 1
    assert deliver_pending_events() == 0
    with SessionLocal() as session:
        event = session.scalar(select(OutboxEvent))
        assert event is not None
        assert event.attempts == 1
        assert event.published_at is not None
