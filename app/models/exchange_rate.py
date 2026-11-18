from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, currency_check


class ExchangeRate(Base):
    """rate_to_usd = how many USD one unit of `currency` is worth on `valid_on`.

    Converting X from currency A to currency B on day d:
        X * rate_to_usd(A, d) / rate_to_usd(B, d)
    where each rate is the latest one with valid_on <= d (gaps fall back to the
    previous day's rate).
    """

    __tablename__ = "exchange_rates"
    __table_args__ = (
        CheckConstraint("rate_to_usd > 0", name="rate_positive"),
        CheckConstraint(currency_check("currency"), name="currency"),
    )

    # Composite PK = one row per currency per day, and its index serves the
    # "latest rate on or before d" lookup: WHERE currency = ? AND valid_on <= ?
    # ORDER BY valid_on DESC LIMIT 1 is a single backward index probe.
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    valid_on: Mapped[date] = mapped_column(Date, primary_key=True)
    rate_to_usd: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
