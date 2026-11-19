from fastapi import APIRouter

from app.controllers.api.v1.auth_controller import router as auth_router

# Main v1 router, mounted at /api/v1 in app/core/application.py.
# Each feature controller's router is included here as its slice lands
# (auth, categories, expenses, reports, notifications, imports).
router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["Auth"])
