# Importing every model here registers it on Base.metadata, which Alembic's
# autogenerate (database/migrations/env.py) and the test harness rely on.
from app.models.base import Base
from app.models.category import Category
from app.models.exchange_rate import ExchangeRate
from app.models.expense import Expense
from app.models.refresh_token import RefreshToken
from app.models.user import User

__all__ = ["Base", "Category", "ExchangeRate", "Expense", "RefreshToken", "User"]
