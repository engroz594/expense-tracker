from enum import StrEnum


class Currency(StrEnum):
    """Currencies the API accepts. Must match the currencies that have exchange rates
    seeded (database/migrations), otherwise expenses in them couldn't be converted."""

    PKR = "PKR"
    AED = "AED"
    USD = "USD"
    EUR = "EUR"


class ReceiptType(StrEnum):
    """Allowed receipt formats; the value is the MIME type the client sends."""

    JPEG = "image/jpeg"
    PNG = "image/png"
    PDF = "application/pdf"

    @property
    def extension(self) -> str:
        return {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}[self.value]

    @classmethod
    def from_extension(cls, extension: str) -> "ReceiptType":
        for receipt_type in cls:
            if receipt_type.extension == extension:
                return receipt_type
        raise ValueError(f"Unsupported receipt extension: {extension!r}")
