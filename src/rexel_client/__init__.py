"""Rexel Belgium web-services client.

A Python client for the Rexel Belgium integration API: the ``rexel`` SOAP service
(article info, prices, basket) and the ``rexstand`` REST bulk catalog (DBF).
"""

from importlib.metadata import PackageNotFoundError, version

from .client import RexelClient
from .exceptions import RexelAPIError, RexelAuthError, RexelConnectionError, RexelError
from .models import Article, CatalogItem, CatalogSnapshot, Price

try:
    # Single source of truth for the version: the installed package metadata.
    # (cebeo-client hard-coded __version__ and drifted from pyproject — don't repeat that.)
    __version__ = version("rexel-client")
except PackageNotFoundError:  # pragma: no cover - only when running from a bare checkout
    __version__ = "0.0.0"

__all__ = [
    "RexelClient",
    "Article",
    "Price",
    "CatalogItem",
    "CatalogSnapshot",
    "RexelError",
    "RexelAPIError",
    "RexelAuthError",
    "RexelConnectionError",
    "__version__",
]
