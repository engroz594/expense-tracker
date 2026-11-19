from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models import User
from app.schemas.report_schemas import CategoryReport, MonthlyReportQuery, MonthlyReportResponse
from app.services.report_service import ReportService

router = APIRouter()


def get_report_service(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> ReportService:
    return ReportService(db, user)


@router.get("/monthly", response_model=MonthlyReportResponse)
async def monthly_report(
    query: Annotated[MonthlyReportQuery, Query()],
    reports: ReportService = Depends(get_report_service),
) -> MonthlyReportResponse:
    rows = await reports.monthly(query.year, query.month)
    return MonthlyReportResponse(
        year=query.year,
        month=query.month,
        base_currency=reports.user.base_currency,
        categories=[CategoryReport.model_validate(row) for row in rows],
    )
