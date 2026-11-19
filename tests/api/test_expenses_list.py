from datetime import timedelta

import pytest
from httpx import AsyncClient

from tests.helpers import TODAY

EXPENSES = "/api/v1/expenses"

pytestmark = pytest.mark.usefixtures("rates")


def day(offset: int) -> str:
    return (TODAY - timedelta(days=offset)).isoformat()


@pytest.fixture
async def food(auth_headers, create_category) -> dict:
    return await create_category(auth_headers, name="Food")


async def list_all(client: AsyncClient, headers: dict, **params) -> list[dict]:
    response = await client.get(EXPENSES, params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


# --- pagination -----------------------------------------------------------------------


async def test_pages_are_sorted_newest_first_with_id_as_tiebreaker(
    client: AsyncClient, auth_headers, food, create_expense
):
    for offset in (3, 1, 1, 2, 1):  # three expenses share a day
        await create_expense(auth_headers, food["id"], spent_on=day(offset))

    seen, cursor = [], None
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        body = (await client.get(EXPENSES, params=params, headers=auth_headers)).json()
        seen += body["items"]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    keys = [(e["spent_on"], e["id"]) for e in seen]
    assert keys == sorted(keys, reverse=True)
    assert len(seen) == 5


async def test_rows_added_between_pages_cause_no_duplicates_or_gaps(
    client: AsyncClient, auth_headers, food, create_expense
):
    """Brief: cursor pagination returns no duplicate or missing rows while new rows
    are added between pages."""
    originals = [
        (await create_expense(auth_headers, food["id"], spent_on=day(offset)))["id"]
        for offset in (1, 2, 3, 3, 4, 5, 6, 7)
    ]

    first = (await client.get(EXPENSES, params={"limit": 3}, headers=auth_headers)).json()
    pages = [first["items"]]
    cursor = first["next_cursor"]
    cursor_day = first["items"][-1]["spent_on"]

    # Between page requests, other writes land: newer than everything already read,
    # on the very same day as the cursor row, and older than the cursor.
    newer = await create_expense(auth_headers, food["id"], spent_on=day(0))
    same_day = await create_expense(auth_headers, food["id"], spent_on=cursor_day)
    older = await create_expense(auth_headers, food["id"], spent_on=day(20))

    while cursor:
        body = (
            await client.get(EXPENSES, params={"limit": 3, "cursor": cursor}, headers=auth_headers)
        ).json()
        pages.append(body["items"])
        cursor = body["next_cursor"]

    returned = [e["id"] for page in pages for e in page]
    assert len(returned) == len(set(returned)), "a row was returned twice"
    assert set(originals) <= set(returned), "a row that existed all along was skipped"
    # A row newer than the cursor belongs to pages already read; it can't reappear.
    assert newer["id"] not in returned
    # A row older than the cursor is simply part of the later pages.
    assert older["id"] in returned
    # The same-day row lands on whichever side of the cursor its id sorts to; either
    # way it appears at most once (checked above).
    assert returned.count(same_day["id"]) <= 1


async def test_last_page_has_null_cursor(client: AsyncClient, auth_headers, food, create_expense):
    await create_expense(auth_headers, food["id"])

    body = (await client.get(EXPENSES, headers=auth_headers)).json()

    assert len(body["items"]) == 1
    assert body["next_cursor"] is None


async def test_only_my_expenses_are_listed(
    client: AsyncClient, auth_headers, other_auth_headers, food, create_category, create_expense
):
    mine = await create_expense(auth_headers, food["id"])
    bobs_category = await create_category(other_auth_headers, name="Bob's")
    await create_expense(other_auth_headers, bobs_category["id"])

    assert [e["id"] for e in await list_all(client, auth_headers)] == [mine["id"]]


# --- filters --------------------------------------------------------------------------


async def test_each_filter_narrows_the_result(
    client: AsyncClient, auth_headers, food, create_category, create_expense
):
    travel = await create_category(auth_headers, name="Travel")
    lunch = await create_expense(
        auth_headers,
        food["id"],
        amount="10.00",
        currency="PKR",
        description="Team lunch",
        spent_on=day(1),
    )
    taxi = await create_expense(
        auth_headers,
        travel["id"],
        amount="55.50",
        currency="AED",
        description="Airport taxi",
        spent_on=day(5),
    )
    dinner = await create_expense(
        auth_headers,
        food["id"],
        amount="120.00",
        currency="EUR",
        description="Dinner, 50% off",
        spent_on=day(10),
    )

    async def ids(**params) -> set[str]:
        return {e["id"] for e in await list_all(client, auth_headers, **params)}

    assert await ids(category_id=travel["id"]) == {taxi["id"]}
    assert await ids(date_from=day(6), date_to=day(1)) == {lunch["id"], taxi["id"]}
    assert await ids(min_amount="50", max_amount="100") == {taxi["id"]}
    assert await ids(currency="EUR") == {dinner["id"]}
    assert await ids(q="LUNCH") == {lunch["id"]}  # case-insensitive substring
    assert await ids(q="50%") == {dinner["id"]}  # % is matched literally, not a wildcard
    assert await ids(q="%") == {dinner["id"]}
    assert await ids(category_id=food["id"], min_amount="100") == {dinner["id"]}


@pytest.mark.parametrize(
    "params",
    [
        {"date_from": day(1), "date_to": day(5)},  # from after to
        {"min_amount": "10", "max_amount": "5"},
        {"min_amount": "-1"},
        {"limit": 0},
        {"limit": 101},
        {"currency": "GBP"},
        {"unexpected": "x"},
    ],
)
async def test_invalid_filters_are_422(client: AsyncClient, auth_headers, params):
    response = await client.get(EXPENSES, params=params, headers=auth_headers)

    assert response.status_code == 422


@pytest.mark.parametrize("cursor", ["not-base64!", "e30", "eyJkIjoieCIsImlkIjoieSJ9"])
async def test_malformed_cursor_is_400(client: AsyncClient, auth_headers, cursor):
    response = await client.get(EXPENSES, params={"cursor": cursor}, headers=auth_headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"
