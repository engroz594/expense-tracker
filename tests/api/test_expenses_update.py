import asyncio
import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Expense
from tests.helpers import TODAY

EXPENSES = "/api/v1/expenses"

pytestmark = pytest.mark.usefixtures("rates")


@pytest.fixture
async def food(auth_headers, create_category) -> dict:
    return await create_category(auth_headers, name="Food")


@pytest.fixture
async def expense(auth_headers, food, create_expense) -> dict:
    return await create_expense(auth_headers, food["id"], amount="10.00", description="Lunch")


async def patch(client: AsyncClient, headers: dict, expense_id: str, body: dict, if_match=None):
    extra = {"If-Match": if_match} if if_match is not None else {}
    return await client.patch(f"{EXPENSES}/{expense_id}", json=body, headers=headers | extra)


# --- ETag -----------------------------------------------------------------------------


async def test_get_returns_etag_from_version(client: AsyncClient, auth_headers, expense):
    response = await client.get(f"{EXPENSES}/{expense['id']}", headers=auth_headers)

    assert response.headers["etag"] == '"1"'


async def test_patch_with_current_etag_updates_and_bumps_version(
    client: AsyncClient, auth_headers, expense
):
    response = await patch(
        client, auth_headers, expense["id"], {"amount": "15.25", "description": "Big lunch"}, '"1"'
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["amount"], body["description"], body["version"]) == ("15.25", "Big lunch", 2)
    assert response.headers["etag"] == '"2"'
    assert body["updated_at"] > expense["updated_at"]
    # Fields not sent are untouched.
    assert (body["currency"], body["spent_on"]) == (expense["currency"], expense["spent_on"])


async def test_weak_etag_form_is_accepted(client: AsyncClient, auth_headers, expense):
    response = await patch(client, auth_headers, expense["id"], {"description": "x"}, 'W/"1"')

    assert response.status_code == 200


# --- preconditions --------------------------------------------------------------------


@pytest.mark.parametrize("if_match", [None, "*", ""])
async def test_missing_if_match_is_428(client: AsyncClient, auth_headers, expense, if_match):
    response = await patch(client, auth_headers, expense["id"], {"description": "x"}, if_match)

    assert response.status_code == 428
    assert response.json()["error"]["code"] == "if_match_required"


@pytest.mark.parametrize("if_match", ['"2"', '"0"', "1", '"abc"'])
async def test_stale_or_malformed_if_match_is_412(
    client: AsyncClient, auth_headers, expense, db: AsyncSession, if_match
):
    response = await patch(client, auth_headers, expense["id"], {"description": "x"}, if_match)

    assert response.status_code == 412
    stored = await db.get(Expense, uuid.UUID(expense["id"]))
    assert (stored.description, stored.version) == ("Lunch", 1)  # nothing written


async def test_second_save_with_an_old_etag_does_not_overwrite_the_first(
    client: AsyncClient, auth_headers, expense
):
    first = await patch(client, auth_headers, expense["id"], {"description": "Phone A"}, '"1"')
    second = await patch(client, auth_headers, expense["id"], {"description": "Phone B"}, '"1"')

    assert (first.status_code, second.status_code) == (200, 412)
    assert second.json()["error"]["code"] == "version_conflict"
    current = await client.get(f"{EXPENSES}/{expense['id']}", headers=auth_headers)
    assert current.json()["description"] == "Phone A"


async def test_parallel_patches_with_same_etag_one_wins_one_gets_412(
    client: AsyncClient, auth_headers, expense, db: AsyncSession
):
    """Brief: two parallel PATCHes with the same ETag: one gets 200, the other 412."""
    responses = await asyncio.gather(
        patch(client, auth_headers, expense["id"], {"description": "Phone A"}, '"1"'),
        patch(client, auth_headers, expense["id"], {"description": "Phone B"}, '"1"'),
    )

    assert sorted(r.status_code for r in responses) == [200, 412]
    winner = next(r for r in responses if r.status_code == 200).json()
    stored = await db.scalar(select(Expense).where(Expense.id == uuid.UUID(expense["id"])))
    assert (stored.version, stored.description) == (2, winner["description"])


# --- ownership and validation ---------------------------------------------------------


async def test_patching_another_users_expense_is_404(
    client: AsyncClient, other_auth_headers, expense
):
    response = await patch(client, other_auth_headers, expense["id"], {"description": "x"}, '"1"')

    assert response.status_code == 404


async def test_moving_to_another_users_category_is_404(
    client: AsyncClient, auth_headers, other_auth_headers, create_category, expense
):
    bobs = await create_category(other_auth_headers, name="Bob's")

    response = await patch(client, auth_headers, expense["id"], {"category_id": bobs["id"]}, '"1"')

    assert response.status_code == 404


async def test_moving_to_a_date_without_rates_is_422(client: AsyncClient, auth_headers, expense):
    no_rates = (TODAY - timedelta(days=400)).isoformat()

    response = await patch(client, auth_headers, expense["id"], {"spent_on": no_rates}, '"1"')

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_exchange_rate"


async def test_changing_currency_and_category_together(
    client: AsyncClient, auth_headers, create_category, expense
):
    travel = await create_category(auth_headers, name="Travel")

    response = await patch(
        client, auth_headers, expense["id"], {"currency": "EUR", "category_id": travel["id"]}, '"1"'
    )

    assert response.status_code == 200
    assert (response.json()["currency"], response.json()["category_id"]) == ("EUR", travel["id"])


@pytest.mark.parametrize(
    "body",
    [
        {},  # nothing to change
        {"amount": None},  # explicit null
        {"amount": "0"},
        {"amount": "1.234"},
        {"version": 5},  # not a client-editable field
    ],
)
async def test_invalid_patch_body_is_422(client: AsyncClient, auth_headers, expense, body):
    response = await patch(client, auth_headers, expense["id"], body, '"1"')

    assert response.status_code == 422


async def test_patch_after_delete_is_404(client: AsyncClient, auth_headers, expense):
    await client.delete(f"{EXPENSES}/{expense['id']}", headers=auth_headers)

    response = await patch(client, auth_headers, expense["id"], {"description": "x"}, '"1"')

    assert response.status_code == 404
