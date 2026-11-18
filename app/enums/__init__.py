from enum import StrEnum


class Currency(StrEnum):
    """Currencies the API accepts. Must match the currencies that have exchange rates
    seeded (database/migrations), otherwise expenses in them couldn't be converted."""

    PKR = "PKR"
    AED = "AED"
    USD = "USD"
    EUR = "EUR"
