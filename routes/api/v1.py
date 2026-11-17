from fastapi import APIRouter

# Main v1 router, mounted at /api/v1 in app/core/application.py.
# Each feature controller's router is included here as its slice lands
# (auth, categories, expenses, reports, notifications, imports).
router = APIRouter()
