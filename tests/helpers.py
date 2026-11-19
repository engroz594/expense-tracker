"""Plain helpers shared by tests (fixtures live in conftest.py)."""

DEFAULT_EMAIL = "alice@example.com"
DEFAULT_PASSWORD = "correct-horse-battery"  # test fixture value, not a real secret


def bearer(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}
