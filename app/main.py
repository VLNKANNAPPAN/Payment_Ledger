import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import OUTBOX_POLL_SECONDS
from app.database import Base, engine, get_session
from app.models import OutboxEvent, Payment
from app.schemas import CreatePaymentRequest, EventResponse, PaymentResponse
from app.services import IdempotencyPayloadMismatch, create_payment
from app.worker import deliver_pending_events

logging.basicConfig(level=logging.INFO)


def initialize_database() -> None:
    Base.metadata.create_all(engine)
    # ORM discipline is not enough to make a ledger immutable. The database
    # rejects direct UPDATE/DELETE attempts too.
    with engine.begin() as connection:
        connection.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION prevent_ledger_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'ledger_entries are append-only'; END;
        $$ LANGUAGE plpgsql;
        """)
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS ledger_entries_immutable ON ledger_entries")
        connection.exec_driver_sql("""
        CREATE TRIGGER ledger_entries_immutable BEFORE UPDATE OR DELETE ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION prevent_ledger_mutation();
        """)


def worker_loop(stop: threading.Event) -> None:
    while not stop.wait(OUTBOX_POLL_SECONDS):
        try:
            deliver_pending_events()
        except Exception:  # worker errors must not stop the API
            logging.exception("outbox polling failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    stop = threading.Event()
    thread = threading.Thread(target=worker_loop, args=(stop,), daemon=True, name="outbox-poller")
    thread.start()
    yield
    stop.set()
    thread.join(timeout=OUTBOX_POLL_SECONDS + 1)


app = FastAPI(title="Payment Ledger", lifespan=lifespan)


@app.post("/payments", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
def post_payment(
    body: CreatePaymentRequest,
    response: Response,
    session: Session = Depends(get_session),
) -> Payment:
    try:
        payment, created = create_payment(session, body)
    except IdempotencyPayloadMismatch as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return payment


@app.get("/payments/{payment_id}", response_model=PaymentResponse)
def get_payment(payment_id: uuid.UUID, session: Session = Depends(get_session)) -> Payment:
    payment = session.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="payment not found")
    return payment


@app.get("/payments/{payment_id}/events", response_model=list[EventResponse])
def get_payment_events(payment_id: uuid.UUID, session: Session = Depends(get_session)) -> list[OutboxEvent]:
    if session.get(Payment, payment_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="payment not found")
    return list(session.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id == payment_id)))
