import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models import User
from app.schemas.expense_schemas import ExpenseCreate, ExpenseResponse
from app.services.expense_service import ExpenseService

router = APIRouter()


def get_expense_service(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> ExpenseService:
    return ExpenseService(db, user)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ExpenseResponse)
async def create_expense(
    payload: ExpenseCreate, expenses: ExpenseService = Depends(get_expense_service)
) -> ExpenseResponse:
    return ExpenseResponse.model_validate(await expenses.create(payload))


@router.get("/{expense_id}", response_model=ExpenseResponse)
async def get_expense(
    expense_id: uuid.UUID, expenses: ExpenseService = Depends(get_expense_service)
) -> ExpenseResponse:
    return ExpenseResponse.model_validate(await expenses.get(expense_id))


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_expense(
    expense_id: uuid.UUID, expenses: ExpenseService = Depends(get_expense_service)
) -> Response:
    await expenses.delete(expense_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
