import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.enums import Currency
from app.schemas.types import MoneyOut, PercentOut


class MonthlyReportQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)


class CategoryReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category_id: uuid.UUID
    name: str
    total: MoneyOut  # this month, in the base currency
    budget: MoneyOut | None
    percent_used: PercentOut | None  # null when the category has no budget
    previous_total: MoneyOut  # last month, in the base currency
    change: MoneyOut  # total - previous_total
    change_percent: PercentOut | None  # null when last month's total was 0


class MonthlyReportResponse(BaseModel):
    year: int
    month: int
    base_currency: Currency
    categories: list[CategoryReport]
