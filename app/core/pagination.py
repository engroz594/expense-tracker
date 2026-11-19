"""Opaque keyset cursors.

A cursor is the sort key of the last row of a page — (spent_on, id) for expenses —
base64url-encoded JSON. The next page is "rows strictly after that key in sort
order", so it depends only on that one row's values, never on how many rows came
before it. That is why pages stay correct while rows are inserted between requests
(an OFFSET would shift and skip or repeat rows), and why deep pages stay fast.

Opaque to clients: they pass next_cursor back unchanged and must not build their own.
"""

import base64
import binascii
import json
import uuid
from datetime import date

from app.exceptions import BadRequest


def encode_cursor(spent_on: date, row_id: uuid.UUID) -> str:
    raw = json.dumps({"d": spent_on.isoformat(), "id": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[date, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return date.fromisoformat(data["d"]), uuid.UUID(data["id"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        # json.JSONDecodeError and UnicodeDecodeError are ValueError subclasses.
        raise BadRequest("Invalid cursor", code="invalid_cursor") from None
