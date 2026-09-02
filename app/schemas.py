import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.models import PaymentStatus


class CreatePaymentRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    source_account: str = Field(min_length=1, max_length=255)
    destination_account: str = Field(min_length=1, max_length=255)

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return value.upper()


class PaymentResponse(BaseModel):
    id: uuid.UUID
    status: PaymentStatus
    amount: Decimal
    currency: str
    source_account: str
    destination_account: str
    created_at: datetime

    model_config = {"from_attributes": True}


class EventResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    status: str
    attempts: int
    created_at: datetime
    published_at: datetime | None

    model_config = {"from_attributes": True}
