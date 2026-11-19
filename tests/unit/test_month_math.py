from datetime import date

import pytest

from app.services.currency_service import month_start, shift_month


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 1, 1), -1, date(2025, 12, 1)),  # January's "previous month"
        (date(2026, 12, 1), 1, date(2027, 1, 1)),  # December's "next month"
        (date(2026, 10, 1), -1, date(2026, 9, 1)),
        (date(2026, 10, 1), 0, date(2026, 10, 1)),
        (date(2026, 3, 1), -14, date(2025, 1, 1)),
    ],
)
def test_shift_month_crosses_year_boundaries(start, months, expected):
    assert shift_month(start, months) == expected


def test_month_start():
    assert month_start(date(2026, 2, 28)) == date(2026, 2, 1)
