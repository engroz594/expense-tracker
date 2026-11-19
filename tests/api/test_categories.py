import pytest
from httpx import AsyncClient

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


async def test_categories_require_authentication(client: AsyncClient):
    assert (await client.get(CATEGORIES)).status_code == 401
    assert (await client.post(CATEGORIES, json={"name": "Food"})).status_code == 401
