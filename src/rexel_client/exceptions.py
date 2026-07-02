"""Exceptions for the Rexel client.

Hierarchy mirrors cebeo-client so the two connectors handle errors the same way::

    RexelError
    ├── RexelAPIError(code, message)     # non-zero <StatusCode> in the response
    │   └── RexelAuthError               # credential/access status codes (10-13, 20, 21)
    └── RexelConnectionError             # transport failure (timeout, DNS, HTTP 5xx)
"""


class RexelError(Exception):
    """Base exception for Rexel client errors."""

    pass


class RexelAPIError(RexelError):
    """Error returned by the Rexel API (non-zero StatusCode)."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Rexel API error {code}: {message}")


class RexelAuthError(RexelAPIError):
    """Authentication / access error from the Rexel API.

    Raised for the credential status codes: 10 invalid user, 11 inactive user,
    12 invalid password, 13 no web-service access, 20 invalid customer,
    21 ambiguous customer.
    """

    pass


class RexelConnectionError(RexelError):
    """Connection error when calling the Rexel API (timeout, DNS, HTTP error)."""

    pass
