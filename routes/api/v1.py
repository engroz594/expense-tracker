from fastapi import APIRouter

from app.controllers.api.v1.auth_controller import router as auth_router
from app.controllers.api.v1.category_controller import router as category_router
from app.controllers.api.v1.expense_controller import router as expense_router

# Main v1 router, mounted at /api/v1 in app/core/application.py.
# Each feature controller's router is included here as its slice lands
# (auth, categories, expenses, reports, notifications, imports).
router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["Auth"])
router.include_router(category_router, prefix="/categories", tags=["Categories"])
router.include_router(expense_router, prefix="/expenses", tags=["Expenses"])
