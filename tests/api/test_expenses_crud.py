import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Expense
from tests.helpers import TODAY

EXPENSES = "/api/v1/expenses"

pytestmark = pytest.mark.usefixtures("rates")


@pytest.fixture
async def food(auth_headers, create_category) -> dict:
    return await create_category(auth_headers, name="Food")


def expense_payload(category_id: str, **overrides) -> dict:
    return {
        "category_id": category_id,
        "amount": "12.50",
        "currency": "PKR",
        "description": "Lunch",
        "spent_on": TODAY.isoformat(),
    } | overrides


# --- create -------------------------------------------------------------------------


async def test_create_expense_returns_201(client: AsyncClient, auth_headers, food):
    response = await client.post(EXPENSES, json=expense_payload(food["id"]), headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["amount"] == "12.50"  # exact decimal string, never a float
    assert body["currency"] == "PKR"
    assert body["category_id"] == food["id"]
    assert body["version"] == 1


@pytest.mark.parametrize(
    "amount",
    [0, "0", "0.00", -5, "-5.00", "10.123", 10.123, "0.001", "NaN"],
)
async def test_zero_negative_or_more_than_two_decimals_is_rejected(
    client: AsyncClient, auth_headers, food, db: AsyncSession, amount
):
    """Brief: an amount of 0, a negative amount, or more than 2 decimal places is
    rejected — as a JSON string or a JSON number alike."""
    response = await client.post(
        EXPENSES, json=expense_payload(food["id"], amount=amount), headers=auth_headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "body.amount"
    assert await db.scalar(select(func.count()).select_from(Expense)) == 0


async def test_json_number_amount_is_kept_exact(client: AsyncClient, auth_headers, food):
    response = await client.post(
        EXPENSES, json=expense_payload(food["id"], amount=0.1), headers=auth_headers
    )

    assert response.status_code == 201
    assert response.json()["amount"] == "0.10"  # no 0.1000000000000000055...


async def test_unsupported_currency_is_422(client: AsyncClient, auth_headers, food):
    response = await client.post(
        EXPENSES, json=expense_payload(food["id"], currency="GBP"), headers=auth_headers
    )

    assert response.status_code == 422


async def test_expense_without_any_exchange_rate_for_its_date_is_422(
    client: AsyncClient, auth_headers, food
):
    before_any_rate = TODAY - timedelta(days=400)

    response = await client.post(
        EXPENSES,
        json=expense_payload(food["id"], spent_on=before_any_rate.isoformat()),
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_exchange_rate"


async def test_category_of_another_user_is_404(
    client: AsyncClient, auth_headers, other_auth_headers, create_category
):
    bobs = await create_category(other_auth_headers, name="Bob's")

    response = await client.post(EXPENSES, json=expense_payload(bobs["id"]), headers=auth_headers)

    assert response.status_code == 404


async def test_unknown_category_is_404(client: AsyncClient, auth_headers):
    response = await client.post(
        EXPENSES, json=expense_payload(str(uuid.uuid4())), headers=auth_headers
    )

    assert response.status_code == 404


# --- read / delete ------------------------------------------------------------------


async def test_get_own_expense(client: AsyncClient, auth_headers, food, create_expense):
    created = await create_expense(auth_headers, food["id"])

    response = await client.get(f"{EXPENSES}/{created['id']}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == created


async def test_another_users_expense_returns_404(
    client: AsyncClient, auth_headers, other_auth_headers, food, create_expense, db: AsyncSession
):
    """Brief: return 404 (not 403) for someone else's expense — for reads and writes."""
    alices = await create_expense(auth_headers, food["id"])
    url = f"{EXPENSES}/{alices['id']}"

    get = await client.get(url, headers=other_auth_headers)
    delete = await client.delete(url, headers=other_auth_headers)

    assert get.status_code == 404
    assert delete.status_code == 404
    assert get.json()["error"]["code"] == "not_found"
    # Bob's DELETE didn't touch it.
    assert await db.get(Expense, uuid.UUID(alices["id"])) is not None


async def test_delete_own_expense(client: AsyncClient, auth_headers, food, create_expense):
    created = await create_expense(auth_headers, food["id"])
    url = f"{EXPENSES}/{created['id']}"

    response = await client.delete(url, headers=auth_headers)

    assert response.status_code == 204
    assert (await client.get(url, headers=auth_headers)).status_code == 404
    assert (await client.delete(url, headers=auth_headers)).status_code == 404


async def test_expenses_require_authentication(client: AsyncClient):
    assert (await client.get(f"{EXPENSES}/{uuid.uuid4()}")).status_code == 401
    assert (await client.post(EXPENSES, json={})).status_code == 401
