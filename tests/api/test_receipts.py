"""Receipts against moto's fake S3: real pre-signed POST uploads, real downloads."""

import io
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.storage.s3_storage_client import S3StorageClient, get_storage_client
from app.exceptions import ObjectNotFound
from app.models import Expense, PendingS3Deletion
from app.services.s3_deletion_service import S3DeletionService
from main import app

EXPENSES = "/api/v1/expenses"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
PDF = b"%PDF-1.7\n" + b"\x00" * 200
EXE = b"MZ\x90\x00" + b"\x00" * 200  # a Windows executable's signature

pytestmark = pytest.mark.usefixtures("rates", "s3_bucket")


@pytest.fixture
async def expense(auth_headers, create_category, create_expense) -> dict:
    food = await create_category(auth_headers, name="Food")
    return await create_expense(auth_headers, food["id"])


async def upload_url(client, headers, expense_id, content_type="image/png"):
    return await client.post(
        f"{EXPENSES}/{expense_id}/receipt/upload-url",
        json={"content_type": content_type},
        headers=headers,
    )


async def upload_to_s3(grant: dict, content: bytes) -> int:
    """What a mobile client does: POST the form straight to S3 (here: moto)."""
    async with AsyncClient() as http:
        response = await http.post(
            grant["url"], data=grant["fields"], files={"file": ("receipt", content)}
        )
    return response.status_code


async def confirm(client, headers, expense_id, key):
    return await client.post(
        f"{EXPENSES}/{expense_id}/receipt/confirm", json={"key": key}, headers=headers
    )


async def attach(client, headers, expense_id, content=PNG, content_type="image/png") -> dict:
    grant = (await upload_url(client, headers, expense_id, content_type)).json()
    assert await upload_to_s3(grant, content) in (200, 201, 204)
    response = await confirm(client, headers, expense_id, grant["key"])
    assert response.status_code == 200, response.text
    return grant


async def object_exists(key: str) -> bool:
    try:
        await S3StorageClient().head(key)
        return True
    except ObjectNotFound:
        return False


# --- happy path -------------------------------------------------------------------------


async def test_upload_url_is_a_presigned_post_for_a_server_chosen_key(
    client: AsyncClient, auth_headers, expense, auth_user_id
):
    response = await upload_url(client, auth_headers, expense["id"], "image/jpeg")

    assert response.status_code == 200
    body = response.json()
    assert body["key"].startswith(f"receipts/{auth_user_id}/{expense['id']}/")
    assert body["key"].endswith(".jpg")
    assert body["max_bytes"] == 5 * 1024 * 1024
    assert body["fields"]["key"] == body["key"]
    assert "policy" in body["fields"]  # size/type limits are signed into the policy


@pytest.mark.parametrize(
    ("content", "content_type"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (PDF, "application/pdf")],
)
async def test_upload_confirm_download_round_trip(
    client: AsyncClient, auth_headers, expense, content, content_type
):
    grant = await attach(client, auth_headers, expense["id"], content, content_type)

    stored = (await client.get(f"{EXPENSES}/{expense['id']}", headers=auth_headers)).json()
    assert stored["version"] == expense["version"] + 1  # attaching a receipt is a change
    download = await client.get(f"{EXPENSES}/{expense['id']}/receipt", headers=auth_headers)
    assert download.status_code == 200
    url = download.json()["url"]
    assert parse_qs(urlparse(url).query)["X-Amz-Expires"] == ["900"]  # 15 minutes
    async with AsyncClient() as http:
        fetched = await http.get(url)
    assert fetched.content == content
    assert grant["key"] in url


# --- the real file type is checked ------------------------------------------------------


async def test_exe_renamed_to_png_is_rejected_and_deleted(
    client: AsyncClient, auth_headers, expense, db: AsyncSession
):
    """Brief: test a fake file (an .exe renamed to .png)."""
    grant = (await upload_url(client, auth_headers, expense["id"], "image/png")).json()
    await upload_to_s3(grant, EXE)

    response = await confirm(client, auth_headers, expense["id"], grant["key"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_receipt_type"
    assert not await object_exists(grant["key"])  # not kept around
    stored = await db.get(Expense, uuid.UUID(expense["id"]))
    assert stored.receipt_key is None


async def test_real_pdf_uploaded_under_a_png_key_is_rejected(
    client: AsyncClient, auth_headers, expense
):
    grant = (await upload_url(client, auth_headers, expense["id"], "image/png")).json()
    await upload_to_s3(grant, PDF)

    response = await confirm(client, auth_headers, expense["id"], grant["key"])

    assert response.status_code == 422


async def test_file_over_5mb_is_rejected_at_confirm(client: AsyncClient, auth_headers, expense):
    # Put the oversize object directly (as if the POST limit had been bypassed): the
    # confirm step re-checks the real size.
    grant = (await upload_url(client, auth_headers, expense["id"], "image/png")).json()
    big = PNG + b"\x00" * (5 * 1024 * 1024)
    await S3StorageClient().upload_fileobj(io.BytesIO(big), grant["key"], "image/png")

    response = await confirm(client, auth_headers, expense["id"], grant["key"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "receipt_too_large"
    assert not await object_exists(grant["key"])


async def test_confirm_without_an_upload_is_404(client: AsyncClient, auth_headers, expense):
    grant = (await upload_url(client, auth_headers, expense["id"])).json()

    response = await confirm(client, auth_headers, expense["id"], grant["key"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "receipt_not_uploaded"


@pytest.mark.parametrize(
    "make_key",
    [
        lambda user, exp: f"receipts/{uuid.uuid4()}/{exp}/{uuid.uuid4()}.png",  # other user
        lambda user, exp: f"receipts/{user}/{uuid.uuid4()}/{uuid.uuid4()}.png",  # other expense
        lambda user, exp: f"receipts/{user}/{exp}/../../x/{uuid.uuid4()}.png",  # traversal
        lambda user, exp: f"receipts/{user}/{exp}/{uuid.uuid4()}.exe",  # bad extension
        lambda user, exp: f"receipts/{user}/{exp}/not-a-uuid.png",
    ],
)
async def test_confirm_refuses_keys_not_issued_for_this_expense(
    client: AsyncClient, auth_headers, expense, auth_user_id, make_key
):
    response = await confirm(
        client, auth_headers, expense["id"], make_key(auth_user_id, expense["id"])
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_receipt_key"


async def test_unsupported_content_type_is_422(client: AsyncClient, auth_headers, expense):
    response = await upload_url(client, auth_headers, expense["id"], "application/x-msdownload")

    assert response.status_code == 422


# --- ownership --------------------------------------------------------------------------


async def test_another_users_expense_is_404_for_every_receipt_endpoint(
    client: AsyncClient, auth_headers, other_auth_headers, expense
):
    grant = await attach(client, auth_headers, expense["id"])

    assert (await upload_url(client, other_auth_headers, expense["id"])).status_code == 404
    assert (
        await confirm(client, other_auth_headers, expense["id"], grant["key"])
    ).status_code == 404
    download = await client.get(f"{EXPENSES}/{expense['id']}/receipt", headers=other_auth_headers)
    assert download.status_code == 404


async def test_download_when_no_receipt_is_404(client: AsyncClient, auth_headers, expense):
    response = await client.get(f"{EXPENSES}/{expense['id']}/receipt", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "no_receipt"


# --- deleting files ---------------------------------------------------------------------


async def test_replacing_a_receipt_deletes_the_old_file(
    client: AsyncClient, auth_headers, expense, db: AsyncSession
):
    first = await attach(client, auth_headers, expense["id"])
    second = await attach(client, auth_headers, expense["id"], JPEG, "image/jpeg")

    assert not await object_exists(first["key"])
    assert await object_exists(second["key"])
    assert await db.scalar(select(PendingS3Deletion)) is None


async def test_deleting_an_expense_deletes_its_receipt(
    client: AsyncClient, auth_headers, expense, db: AsyncSession
):
    grant = await attach(client, auth_headers, expense["id"])

    response = await client.delete(f"{EXPENSES}/{expense['id']}", headers=auth_headers)

    assert response.status_code == 204
    assert not await object_exists(grant["key"])
    assert await db.scalar(select(PendingS3Deletion)) is None  # nothing left to retry


class _S3DeleteDown(S3StorageClient):
    async def delete(self, key: str) -> None:
        raise ConnectionError("S3 unavailable")


async def test_failed_s3_delete_is_kept_and_cleaned_up_later(
    client: AsyncClient, auth_headers, expense, db: AsyncSession
):
    """Brief: if the S3 delete fails, the file must be cleaned up later, not forgotten."""
    grant = await attach(client, auth_headers, expense["id"])
    app.dependency_overrides[get_storage_client] = _S3DeleteDown
    try:
        response = await client.delete(f"{EXPENSES}/{expense['id']}", headers=auth_headers)
    finally:
        app.dependency_overrides.pop(get_storage_client, None)

    # The expense is gone, the file is still in S3, and the deletion is recorded.
    assert response.status_code == 204
    assert await object_exists(grant["key"])
    pending = await db.scalar(select(PendingS3Deletion))
    assert (pending.object_key, pending.attempts) == (grant["key"], 1)
    assert "S3 unavailable" in pending.last_error
    assert pending.next_attempt_at > datetime.now(UTC)  # backoff before the next try

    # Not due yet: the cleanup run skips it.
    assert await S3DeletionService(db, S3StorageClient()).retry_due() == (0, 0)

    # Once due, the cleanup script's run deletes the file and the record.
    pending.next_attempt_at = datetime.now(UTC)
    await db.commit()
    assert await S3DeletionService(db, S3StorageClient()).retry_due() == (1, 0)
    assert not await object_exists(grant["key"])
    assert await db.scalar(select(PendingS3Deletion)) is None


async def test_cleanup_keeps_retrying_with_growing_backoff(db: AsyncSession):
    service = S3DeletionService(db, _S3DeleteDown())
    await service.stage("receipts/u/e/k.png")
    await db.commit()

    assert await service.retry_due() == (0, 1)
    first = await db.scalar(select(PendingS3Deletion))
    first_delay = first.next_attempt_at - datetime.now(UTC)
    first.next_attempt_at = datetime.now(UTC)
    await db.commit()

    assert await service.retry_due() == (0, 1)
    second = await db.scalar(select(PendingS3Deletion))
    assert second.attempts == 2
    assert second.next_attempt_at - datetime.now(UTC) > first_delay  # backoff grows
