import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.enums import Currency
from app.schemas.types import Money, MoneyOut

# Filter bounds: like Money, but 0 is a valid lower bound.
AmountBound = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


class ExpenseCreate(BaseModel):
    category_id: uuid.UUID
    amount: Money
    currency: Currency
    description: str = Field(default="", max_length=500)
    spent_on: date


class ExpenseUpdate(BaseModel):
    """PATCH body: send only the fields to change.

    Fields are typed non-optional with a None default: omitting a field leaves it
    unchanged, but an explicit null is a 422 (an expense can't have a null amount).
    """

    model_config = ConfigDict(extra="forbid")

    category_id: uuid.UUID = None  # type: ignore[assignment]
    amount: Money = None  # type: ignore[assignment]
    currency: Currency = None  # type: ignore[assignment]
    description: str = Field(default=None, max_length=500)  # type: ignore[assignment]
    spent_on: date = None  # type: ignore[assignment]

    @model_validator(mode="after")
    def at_least_one_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        return self

    def changes(self) -> dict:
        """Only the fields the client actually sent."""
        return self.model_dump(exclude_unset=True, mode="python")


class ExpenseListQuery(BaseModel):
    """GET /expenses query parameters."""

    model_config = ConfigDict(extra="forbid")

    category_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None
    min_amount: AmountBound | None = None
    max_amount: AmountBound | None = None
    currency: Currency | None = None
    q: str | None = Field(default=None, min_length=1, max_length=100)
    cursor: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def ranges_are_ordered(self) -> Self:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        if (
            self.min_amount is not None
            and self.max_amount is not None
            and self.min_amount > self.max_amount
        ):
            raise ValueError("min_amount must be less than or equal to max_amount")
        return self


class ExpenseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category_id: uuid.UUID
    amount: MoneyOut  # "12.50": exact decimal string, never a float
    currency: Currency
    description: str
    spent_on: date
    version: int
    created_at: datetime
    updated_at: datetime


class ExpensePage(BaseModel):
    items: list[ExpenseResponse]
    next_cursor: str | None  # null on the last page
