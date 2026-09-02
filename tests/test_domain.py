from decimal import Decimal

import pytest

from app.models import LedgerEntry, Payment, PaymentStatus
from app.services import assert_balanced, transition


def test_valid_state_machine_path():
    payment = Payment(status=PaymentStatus.CREATED)
    transition(payment, PaymentStatus.PROCESSING)
    transition(payment, PaymentStatus.SUCCEEDED)
    assert payment.status == PaymentStatus.SUCCEEDED


def test_terminal_payment_cannot_transition():
    payment = Payment(status=PaymentStatus.SUCCEEDED)
    with pytest.raises(ValueError, match="Cannot transition"):
        transition(payment, PaymentStatus.FAILED)


def test_ledger_balance_invariant_rejects_unbalanced_entries():
    entries = [
        LedgerEntry(debit=Decimal("10.00"), credit=Decimal("0")),
        LedgerEntry(debit=Decimal("0"), credit=Decimal("9.99")),
    ]
    with pytest.raises(ValueError, match="Unbalanced"):
        assert_balanced(entries)


def test_ledger_balance_invariant_accepts_matching_debit_credit():
    entries = [
        LedgerEntry(debit=Decimal("10.00"), credit=Decimal("0")),
        LedgerEntry(debit=Decimal("0"), credit=Decimal("10.00")),
    ]
    assert_balanced(entries)
