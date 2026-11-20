"""Storage exceptions, raised by StorageClient implementations.

These are internal signals, not API errors, so they don't subclass AppError: the
calling service decides what each one means for the request (e.g. ReceiptService
turns ObjectNotFound into a 404). One that isn't caught surfaces as a 500.
"""


class ObjectNotFound(Exception):
    """The requested object doesn't exist (or isn't visible to us)."""
