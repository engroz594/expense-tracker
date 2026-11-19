"""Plain helpers shared by tests (fixtures live in conftest.py)."""

from datetime import UTC, datetime

DEFAULT_EMAIL = "alice@example.com"
OTHER_EMAIL = "bob@example.com"
DEFAULT_PASSWORD = "correct-horse-battery"  # test fixture value, not a real secret

# "Today" as the app sees it (rates are filled by UTC date).
TODAY = datetime.now(UTC).date()


def bearer(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}
