from decimal import Decimal
from typing import Annotated

from pydantic import Field, PlainSerializer

# Money in requests: exact Decimal, never float. Pydantic parses JSON numbers from
# their text (0.1 -> Decimal("0.1"), no float rounding) and rejects 0, negatives,
# NaN/Infinity and more than 2 decimal places with a 422.
# max_digits=12 -> up to 9,999,999,999.99, inside the NUMERIC(14, 2) column.
Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]

# Money in responses: always a string with exactly 2 decimal places ("500.00"),
# whether the value came from the database or straight from the request. A string,
# not a JSON number, so no client parses it into a float.
MoneyOut = Annotated[Decimal, PlainSerializer(lambda value: f"{value:.2f}", return_type=str)]

# Percentages in responses, same exact 2-decimal string form ("21.00").
PercentOut = MoneyOut
