from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models import User
from app.schemas.category_schemas import (
    CategoryCreate,
    CategoryListResponse,
    CategoryResponse,
    CategoryWithSpendResponse,
)
from app.services.category_service import CategoryService

router = APIRouter()


def get_category_service(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> CategoryService:
    return CategoryService(db, user)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CategoryResponse)
async def create_category(
    payload: CategoryCreate, categories: CategoryService = Depends(get_category_service)
) -> CategoryResponse:
    category = await categories.create(payload.name, payload.monthly_budget)
    return CategoryResponse.model_validate(category)


@router.get("", response_model=CategoryListResponse)
async def list_categories(
    categories: CategoryService = Depends(get_category_service),
) -> CategoryListResponse:
    items = [
        CategoryWithSpendResponse.model_validate(c) for c in await categories.list_with_spend()
    ]
    return CategoryListResponse(items=items)
