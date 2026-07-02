"""Rexel Belgium web-services client.

One class, :class:`RexelClient`, over the two Rexel data sources:

* **SOAP** ``getArticleInformation`` / ``getPrices`` / ``getBasket`` on
  ``…/exchange/ws/prod/rexel`` (auto-batched at ≤100 per call).
* **REST** ``rexstand`` bulk catalog on ``…/exchange/rs/prod/netstore/downloads``.

This is the one place that knows the Rexel protocol. When the Rexel Group-wide API
lands (~2027), the transport swap happens here, behind this same public surface.
"""

import xml.etree.ElementTree as ET
from contextlib import contextmanager
from decimal import Decimal

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import soap
from .catalog import parse_catalog_zip
from .exceptions import RexelConnectionError
from .models import Article, CatalogItem, Price

DEFAULT_BASE_URL = "https://services.rexel.be/exchange"
SOAP_PATH = "/ws/prod/rexel"
REXSTAND_PATH = "/rs/prod/netstore/downloads/rexstand"

# SOAP/DBF both cap a batch at 100 codes; keep the default a little under.
MAX_BATCH = 100


@contextmanager
def _transport_errors():
    """Map requests transport failures to RexelConnectionError (single source of truth)."""
    try:
        yield
    except requests.exceptions.Timeout as exc:
        raise RexelConnectionError(f"Request timed out: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise RexelConnectionError(f"Connection failed: {exc}") from exc
    except requests.exceptions.HTTPError as exc:
        raise RexelConnectionError(f"HTTP error: {exc}") from exc


class RexelClient:
    """Client for the Rexel Belgium web services.

    Args:
        customer_number: Rexel customer number (``customer``).
        username: web-service user code (``user``).
        password: web-service password.
        base_url: exchange base URL (defaults to production).
        language: ``"NL"`` or ``"FR"`` — request language (default ``"NL"``).
        timeout: per-request timeout in seconds.
        batch_size: max items per SOAP call (clamped to 100).
    """

    def __init__(
        self,
        customer_number: str,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        language: str = "NL",
        timeout: int = 30,
        batch_size: int = 50,
    ):
        self.customer_number = customer_number
        self.username = username
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.language = language
        self.timeout = timeout
        self.batch_size = min(batch_size, MAX_BATCH)

        # Shared session with a retry adapter for idempotent reads (SOAP article/price,
        # rexstand GET). getBasket is destructive and NEVER retried — it uses a bare POST.
        self._session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    # --- transport ---------------------------------------------------------------------

    @property
    def _soap_url(self) -> str:
        return f"{self.base_url}{SOAP_PATH}"

    def _post_soap(self, body: bytes, *, session: requests.Session | None = None) -> ET.Element:
        """POST a SOAP envelope and return the parsed inner ``<Content>`` element.

        Pass ``session`` to override the retrying default (getBasket uses a fresh,
        no-retry session).
        """
        sess = session or self._session
        with _transport_errors():
            response = sess.post(
                self._soap_url,
                data=body,
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": '""',
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        return soap.parse_response(response.content)

    # --- SOAP getArticleInformation ----------------------------------------------------

    def article_get(self, item_codes: list[str]) -> list[Article]:
        """Fetch article details by Rexel item code (or e-number). Auto-batches at ≤100.

        Unknown codes are dropped (the API returns an inactive, empty-code record for
        them), so the result contains only real articles.
        """
        if not item_codes:
            return []
        articles: list[Article] = []
        for start in range(0, len(item_codes), self.batch_size):
            batch = item_codes[start : start + self.batch_size]
            body = soap.build_get_article_information(
                self.customer_number, self.username, self.password, self.language, batch
            )
            articles.extend(soap.parse_articles(self._post_soap(body)))
        return articles

    # --- SOAP getPrices ----------------------------------------------------------------

    def price_get(self, items: list[tuple[str, int | Decimal]]) -> list[Price]:
        """Fetch live prices for (item_code, quantity) pairs. Auto-batches at ≤100."""
        if not items:
            return []
        prices: list[Price] = []
        for start in range(0, len(items), self.batch_size):
            batch = [(code, int(qty)) for code, qty in items[start : start + self.batch_size]]
            body = soap.build_get_prices(
                self.customer_number, self.username, self.password, self.language, batch
            )
            prices.extend(soap.parse_prices(self._post_soap(body)))
        return prices

    # --- SOAP getBasket (⚠️ destructive) -----------------------------------------------

    def basket_get_raw(self) -> str | None:
        """Fetch the NetStore basket and return the raw inner ``<Content>`` XML string.

        ⚠️ **Destructive**: reading the basket empties it in NetStore. This call is
        never retried. Structured ``Basket``/``BasketLine`` modelling is deferred to
        Phase 4 (needs a captured real response first); until then callers get the raw
        XML so they can persist-before-parse (the Phase 4 crash-safety requirement).

        Returns the ``<Content>`` XML string, or ``None`` if the basket is empty.
        """
        # A fresh, retry-free session: getBasket must never be re-sent.
        no_retry = requests.Session()
        try:
            body = soap.build_get_basket(
                self.customer_number, self.username, self.password, self.language
            )
            content = self._post_soap(body, session=no_retry)
        finally:
            no_retry.close()
        if len(content) == 0:
            return None
        return soap.content_to_string(content)

    # --- REST rexstand -----------------------------------------------------------------

    def catalog_download(self) -> list[CatalogItem]:
        """Download the weekly ``rexstand`` ZIP and parse its DBF into CatalogItem rows."""
        return parse_catalog_zip(self.catalog_download_raw())

    def catalog_download_raw(self) -> bytes:
        """Download the ``rexstand`` ZIP and return the raw bytes (for archival).

        The sync job archives this verbatim as an attachment so every DBF column is
        preserved week-by-week (plan OQ5). Parsing is separate — see
        :meth:`catalog_download`.
        """
        with _transport_errors():
            response = self._session.get(
                f"{self.base_url}{REXSTAND_PATH}",
                params={
                    "customer": self.customer_number,
                    "user": self.username,
                    "password": self.password,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        return response.content
