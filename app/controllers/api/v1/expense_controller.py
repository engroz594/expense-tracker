import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.exceptions import PreconditionFailed, PreconditionRequired
from app.models import User
from app.schemas.expense_schemas import (
    ExpenseCreate,
    ExpenseListQuery,
    ExpensePage,
    ExpenseResponse,
    ExpenseUpdate,
)
from app.services.expense_service import ExpenseService

router = APIRouter()

# An entity tag is a quoted string, optionally weak: "3" or W/"3".
_ETAG = re.compile(r'^(?:W/)?"(\d+)"$')


def get_expense_service(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> ExpenseService:
    return ExpenseService(db, user)


def etag_for(version: int) -> str:
    return f'"{version}"'


def parse_if_match(if_match: str | None) -> int:
    """If-Match -> the version the client last saw.

    Missing -> 428: an update without a version could silently overwrite someone
    else's change, which is exactly what the header exists to prevent. "*" ("any
    version") is refused for the same reason. A malformed tag can't match any
    version, so it is a 412.
    """
    if if_match is None or if_match.strip() in ("", "*"):
        raise PreconditionRequired(
            "Send If-Match with the ETag from GET /expenses/{id}", code="if_match_required"
        )
    match = _ETAG.match(if_match.strip())
    if match is None:
        raise PreconditionFailed("If-Match does not match the current version")
    return int(match.group(1))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ExpenseResponse)
async def create_expense(
    payload: ExpenseCreate,
    response: Response,
    expenses: ExpenseService = Depends(get_expense_service),
) -> ExpenseResponse:
    expense = await expenses.create(payload)
    response.headers["ETag"] = etag_for(expense.version)
    return ExpenseResponse.model_validate(expense)


@router.get("", response_model=ExpensePage)
async def list_expenses(
    query: Annotated[ExpenseListQuery, Query()],
    expenses: ExpenseService = Depends(get_expense_service),
) -> ExpensePage:
    items, next_cursor = await expenses.list(query)
    return ExpensePage(
        items=[ExpenseResponse.model_validate(e) for e in items], next_cursor=next_cursor
    )


@router.get("/{expense_id}", response_model=ExpenseResponse)
async def get_expense(
    expense_id: uuid.UUID,
    response: Response,
    expenses: ExpenseService = Depends(get_expense_service),
) -> ExpenseResponse:
    expense = await expenses.get(expense_id)
    response.headers["ETag"] = etag_for(expense.version)
    return ExpenseResponse.model_validate(expense)


@router.patch("/{expense_id}", response_model=ExpenseResponse)
async def update_expense(
    expense_id: uuid.UUID,
    payload: ExpenseUpdate,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
    expenses: ExpenseService = Depends(get_expense_service),
) -> ExpenseResponse:
    expected_version = parse_if_match(if_match)
    expense = await expenses.update(expense_id, expected_version, payload)
    response.headers["ETag"] = etag_for(expense.version)
    return ExpenseResponse.model_validate(expense)


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_expense(
    expense_id: uuid.UUID, expenses: ExpenseService = Depends(get_expense_service)
) -> Response:
    await expenses.delete(expense_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
