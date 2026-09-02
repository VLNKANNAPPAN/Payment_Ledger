import hashlib
import json
import uuid
from decimal import Decimal
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import LedgerEntry, OutboxEvent, OutboxStatus, Payment, PaymentStatus
from app.schemas import CreatePaymentRequest


class IdempotencyPayloadMismatch(ValueError):
    """An idempotency key may only be reused with its original request."""


ALLOWED_TRANSITIONS = {
    PaymentStatus.CREATED: {PaymentStatus.PROCESSING},
    PaymentStatus.PROCESSING: {PaymentStatus.SUCCEEDED, PaymentStatus.FAILED},
    PaymentStatus.SUCCEEDED: set(),
    PaymentStatus.FAILED: set(),
}


def transition(payment: Payment, target: PaymentStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[payment.status]:
        raise ValueError(f"Cannot transition {payment.status} to {target}")
    payment.status = target


def request_fingerprint(request: CreatePaymentRequest) -> str:
    # The idempotency key is intentionally excluded: it identifies this operation.
    canonical = json.dumps(
        {
            "amount": str(request.amount),
            "currency": request.currency,
            "source_account": request.source_account,
            "destination_account": request.destination_account,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def assert_balanced(entries: list[LedgerEntry]) -> None:
    debits = sum((entry.debit for entry in entries), Decimal("0"))
    credits = sum((entry.credit for entry in entries), Decimal("0"))
    if debits != credits:
        raise ValueError(f"Unbalanced ledger transaction: debit={debits}, credit={credits}")


def create_payment(
    session: Session,
    request: CreatePaymentRequest,
    before_commit: Callable[[], None] | None = None,
) -> tuple[Payment, bool]:
    """Create one atomic payment, ledger transaction, and outbox event.

    The savepoint is the key concurrency detail.  If another request wins the
    unique-key race, PostgreSQL rejects this INSERT; we roll back only the
    savepoint and return the row the winning transaction committed.
    """
    fingerprint = request_fingerprint(request)
    created = False

    with session.begin():
        try:
            with session.begin_nested():
                payment = Payment(
                    idempotency_key=request.idempotency_key,
                    request_fingerprint=fingerprint,
                    amount=request.amount,
                    currency=request.currency,
                    source_account=request.source_account,
                    destination_account=request.destination_account,
                    status=PaymentStatus.CREATED,
                )
                session.add(payment)
                session.flush()  # forces the database unique constraint now
                created = True
        except IntegrityError:
            payment = session.scalar(
                select(Payment).where(Payment.idempotency_key == request.idempotency_key)
            )
            if payment is None:  # defensive: the database should make this impossible
                raise

        if not created:
            if payment.request_fingerprint != fingerprint:
                raise IdempotencyPayloadMismatch("idempotency_key was already used with a different payload")
            return payment, False

        transition(payment, PaymentStatus.PROCESSING)
        transaction_id = uuid.uuid4()
        entries = [
            LedgerEntry(
                transaction_id=transaction_id,
                payment_id=payment.id,
                account_id=request.source_account,
                debit=request.amount,
                credit=Decimal("0"),
                amount=request.amount,
                currency=request.currency,
            ),
            LedgerEntry(
                transaction_id=transaction_id,
                payment_id=payment.id,
                account_id=request.destination_account,
                debit=Decimal("0"),
                credit=request.amount,
                amount=request.amount,
                currency=request.currency,
            ),
        ]
        assert_balanced(entries)
        session.add_all(entries)
        transition(payment, PaymentStatus.SUCCEEDED)
        session.add(
            OutboxEvent(
                event_type="payment.succeeded",
                aggregate_id=payment.id,
                payload={"event_id": str(uuid.uuid4()), "payment_id": str(payment.id), "transaction_id": str(transaction_id)},
                status=OutboxStatus.PENDING,
            )
        )
        if before_commit is not None:
            before_commit()

    return payment, True
