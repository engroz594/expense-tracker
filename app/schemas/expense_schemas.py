import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.enums import Currency
from app.schemas.types import Money, MoneyOut


class ExpenseCreate(BaseModel):
    category_id: uuid.UUID
    amount: Money
    currency: Currency
    description: str = Field(default="", max_length=500)
    spent_on: date


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
