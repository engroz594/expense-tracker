import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.types import Money, MoneyOut, PercentOut


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # In the user's base currency; omit for "no budget" (no alerts).
    monthly_budget: Money | None = None


class CategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    monthly_budget: MoneyOut | None
    created_at: datetime


class CategoryWithSpendResponse(CategoryResponse):
    spent_this_month: MoneyOut  # current calendar month (UTC), in the base currency
    percent_used: PercentOut | None  # null when the category has no budget


class CategoryListResponse(BaseModel):
    items: list[CategoryWithSpendResponse]
