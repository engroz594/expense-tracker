"""Monthly report. Rates are inserted by hand (not the generated fill) so every
expected number below can be checked with a calculator.

User's base currency: AED.  rate_to_usd = USD value of one unit.

    USD  1.00   from 2026-09-01
    AED  0.25   from 2026-09-01   (4 AED per USD; no later AED rate at all)
    EUR  1.10   from 2026-10-01,  1.20 from 2026-10-10
    PKR  0.004  from 2026-09-01,  0.005 from 2026-10-20

Conversion: amount * rate(currency) / rate(AED), each the latest rate <= spent_on.
"""

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import engine
from app.models import ExchangeRate
from tests.helpers import bearer

REPORT = "/api/v1/reports/monthly"

RATES = [
    ("USD", date(2026, 9, 1), "1.00"),
    ("AED", date(2026, 9, 1), "0.25"),
    ("EUR", date(2026, 10, 1), "1.10"),
    ("EUR", date(2026, 10, 10), "1.20"),
    ("PKR", date(2026, 9, 1), "0.004"),
    ("PKR", date(2026, 10, 20), "0.005"),
]


@pytest.fixture
async def aed_user(db: AsyncSession, register_user, login) -> dict:
    db.add_all(ExchangeRate(currency=c, valid_on=d, rate_to_usd=Decimal(r)) for c, d, r in RATES)
    await db.commit()
    await register_user(email="aed@example.com", base_currency="AED")
    return bearer(await login(email="aed@example.com"))


async def get_report(client: AsyncClient, headers: dict, year=2026, month=10) -> dict:
    response = await client.get(REPORT, params={"year": year, "month": month}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_report_converts_mixed_currencies_with_missing_rate_days(
    client: AsyncClient, aed_user, create_category, create_expense
):
    """Brief: monthly report totals are right with mixed currencies and a missing
    exchange-rate day."""
    food = await create_category(aed_user, name="Food", monthly_budget="1000")
    travel = await create_category(aed_user, name="Travel")
    await create_category(aed_user, name="Misc")

    def spend(category, amount, currency, day):
        return create_expense(
            aed_user, category["id"], amount=amount, currency=currency, spent_on=day
        )

    # October, Food:
    await spend(food, "50.00", "AED", "2026-10-03")  # 50 AED                    = 50.00
    await spend(food, "10.00", "EUR", "2026-10-05")  # no EUR rate on the 5th -> 1.10 (1st)
    #                                                  10 * 1.10 / 0.25        = 44.00
    await spend(food, "20.00", "EUR", "2026-10-15")  # 20 * 1.20 (10th) / 0.25 = 96.00
    await spend(food, "1000.00", "PKR", "2026-10-21")  # 1000 * 0.005 / 0.25   = 20.00
    #                                                  Food October total     = 210.00
    # September, Food:
    await spend(food, "100.00", "USD", "2026-09-15")  # 100 * 1 / 0.25         = 400.00
    # October, Travel:
    await spend(travel, "5.00", "USD", "2026-10-31")  # 5 / 0.25               = 20.00
    # Outside the two months — must not count anywhere:
    await spend(food, "999.00", "AED", "2026-11-01")

    report = await get_report(client, aed_user)

    assert report["base_currency"] == "AED"
    by_name = {row["name"]: row for row in report["categories"]}
    assert by_name["Food"] == {
        "category_id": food["id"],
        "name": "Food",
        "total": "210.00",
        "budget": "1000.00",
        "percent_used": "21.00",
        "previous_total": "400.00",
        "change": "-190.00",
        "change_percent": "-47.50",
    }
    assert by_name["Travel"] | {"category_id": None} == {
        "category_id": None,
        "name": "Travel",
        "total": "20.00",
        "budget": None,
        "percent_used": None,  # no budget
        "previous_total": "0.00",
        "change": "20.00",
        "change_percent": None,  # last month was 0: no meaningful percentage
    }
    assert by_name["Misc"]["total"] == "0.00"  # categories without spend are listed too
    # Highest total first.
    assert [row["name"] for row in report["categories"]] == ["Food", "Travel", "Misc"]


async def test_report_rounds_only_once_at_the_end(
    client: AsyncClient, aed_user, create_category, create_expense
):
    food = await create_category(aed_user, name="Food")
    # Each converts to 0.004 * 1 / 0.25 = 0.016 AED. Rounded per expense that would
    # be 0.02 * 3 = 0.06; summed exactly then rounded once it is 0.048 -> 0.05.
    for _ in range(3):
        await create_expense(
            aed_user, food["id"], amount="1.00", currency="PKR", spent_on="2026-10-02"
        )

    report = await get_report(client, aed_user)

    assert report["categories"][0]["total"] == "0.05"


async def test_report_is_a_single_sql_statement(
    client: AsyncClient, aed_user, create_category, create_expense
):
    food = await create_category(aed_user, name="Food")
    await create_expense(aed_user, food["id"], amount="1.00", currency="USD", spent_on="2026-10-02")
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        await get_report(client, aed_user)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    # One statement authenticates the request (get_current_user); the report is the other.
    report_statements = [s for s in statements if "LAG(" in s]
    assert len(statements) == 2
    assert len(report_statements) == 1


async def test_report_only_includes_my_data(
    client: AsyncClient, aed_user, other_auth_headers, create_category, create_expense
):
    bobs = await create_category(other_auth_headers, name="Bob's")
    await create_expense(other_auth_headers, bobs["id"], amount="5.00", spent_on="2026-10-02")

    report = await get_report(client, aed_user)

    assert report["categories"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"year": 2026, "month": 13},
        {"year": 2026, "month": 0},
        {"year": 1999, "month": 1},
        {"year": 2026},
        {},
    ],
)
async def test_invalid_report_period_is_422(client: AsyncClient, auth_headers, params):
    response = await client.get(REPORT, params=params, headers=auth_headers)

    assert response.status_code == 422


async def test_report_requires_authentication(client: AsyncClient):
    response = await client.get(REPORT, params={"year": 2026, "month": 10})

    assert response.status_code == 401
