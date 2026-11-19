from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from httpx import AsyncClient

from app.enums import Currency
from app.scripts.fill_exchange_rates import rate_for
from tests.helpers import TODAY

CATEGORIES = "/api/v1/categories"


async def test_create_category_returns_201(client: AsyncClient, auth_headers):
    response = await client.post(
        CATEGORIES, json={"name": "  Food  ", "monthly_budget": "500"}, headers=auth_headers
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Food"  # trimmed
    assert body["monthly_budget"] == "500.00"  # exact decimal string, 2 places
    assert set(body) == {"id", "name", "monthly_budget", "created_at"}


async def test_budget_is_optional(client: AsyncClient, auth_headers):
    response = await client.post(CATEGORIES, json={"name": "Misc"}, headers=auth_headers)

    assert response.status_code == 201
    assert response.json()["monthly_budget"] is None


async def test_duplicate_name_is_409_case_insensitively(
    client: AsyncClient, auth_headers, create_category
):
    await create_category(auth_headers, name="Food")

    response = await client.post(CATEGORIES, json={"name": "FOOD"}, headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "category_exists"


async def test_same_name_is_fine_for_a_different_user(
    auth_headers, other_auth_headers, create_category
):
    await create_category(auth_headers, name="Food")

    await create_category(other_auth_headers, name="Food")  # asserts 201


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": "x" * 101},
        {"name": "Food", "monthly_budget": "0"},
        {"name": "Food", "monthly_budget": "-10"},
        {"name": "Food", "monthly_budget": "10.123"},
    ],
)
async def test_invalid_category_is_422(client: AsyncClient, auth_headers, payload):
    response = await client.post(CATEGORIES, json=payload, headers=auth_headers)

    assert response.status_code == 422


async def test_list_returns_only_my_categories_sorted_by_name(
    client: AsyncClient, auth_headers, other_auth_headers, create_category
):
    await create_category(auth_headers, name="Travel")
    await create_category(auth_headers, name="Food")
    await create_category(other_auth_headers, name="Bob's secret category")

    response = await client.get(CATEGORIES, headers=auth_headers)

    assert response.status_code == 200
    assert [c["name"] for c in response.json()["items"]] == ["Food", "Travel"]


@pytest.mark.usefixtures("rates")
async def test_list_shows_this_months_spend_vs_budget_in_base_currency(
    client: AsyncClient, auth_headers, create_category, create_expense
):
    # The default user's base currency is PKR.
    food = await create_category(auth_headers, name="Food", monthly_budget="1000")
    travel = await create_category(auth_headers, name="Travel")
    this_month = TODAY
    last_month = TODAY.replace(day=1) - timedelta(days=1)
    await create_expense(auth_headers, food["id"], amount="100.00", spent_on=this_month.isoformat())
    await create_expense(auth_headers, food["id"], amount="50.50", spent_on=this_month.isoformat())
    await create_expense(auth_headers, food["id"], amount="999.00", spent_on=last_month.isoformat())
    await create_expense(
        auth_headers, travel["id"], amount="10.00", currency="EUR", spent_on=this_month.isoformat()
    )

    response = await client.get(CATEGORIES, headers=auth_headers)

    items = {c["name"]: c for c in response.json()["items"]}
    assert items["Food"]["spent_this_month"] == "150.50"  # last month's 999 excluded
    assert items["Food"]["percent_used"] == "15.05"
    eur_in_pkr = (
        Decimal("10.00") * rate_for(Currency.EUR, this_month) / rate_for(Currency.PKR, this_month)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert items["Travel"]["spent_this_month"] == str(eur_in_pkr)
    assert items["Travel"]["percent_used"] is None


async def test_categories_require_authentication(client: AsyncClient):
    assert (await client.get(CATEGORIES)).status_code == 401
    assert (await client.post(CATEGORIES, json={"name": "Food"})).status_code == 401
