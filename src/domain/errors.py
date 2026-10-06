"""Domain-level exceptions."""


class DomainError(Exception):
    """Base class for expected, business-level failures."""


class InvalidFieldError(DomainError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message
