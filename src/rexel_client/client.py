"""Rexel Belgium web-services client.

One class, :class:`RexelClient`, over the two Rexel data sources:

* **SOAP** ``getArticleInformation`` / ``getPrices`` / ``getBasket`` on
  ``…/exchange/ws/prod/rexel`` (auto-batched at ≤100 per call).
* **REST** ``rexstand`` bulk catalog on ``…/exchange/rs/prod/netstore/downloads``.

This is the one place that knows the Rexel protocol.
"""

import logging
import re
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from decimal import Decimal

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import soap
from .catalog import parse_catalog_snapshot, parse_catalog_zip
from .exceptions import RexelAPIError, RexelConnectionError
from .models import Article, CatalogItem, CatalogSnapshot, Price

DEFAULT_BASE_URL = "https://services.rexel.be/exchange"
SOAP_PATH = "/ws/prod/rexel"
REXSTAND_PATH = "/rs/prod/netstore/downloads/rexstand"

# SOAP/DBF both cap a batch at 100 codes; keep the default a little under.
MAX_BATCH = 100


def _canonical(code: str | None) -> str:
    """The canonical form of an item code — stripped, upper-cased — as sent and matched."""
    return (code or "").strip().upper()


def _without_query(message: object) -> str:
    """``message`` with any URL query string cut: rexstand's holds the credentials."""
    return re.sub(r"\?\S*", "", str(message))


class _HideRexstandQuery(logging.Filter):
    """Keep the credentials out of the request URLs urllib3 logs for rexstand."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _without_query(arg) if isinstance(arg, str) and REXSTAND_PATH in arg else arg
                for arg in record.args
            )
        return True


for _logger_name in ("urllib3.connectionpool", "urllib3.util.retry"):
    logging.getLogger(_logger_name).addFilter(_HideRexstandQuery())


@contextmanager
def _transport_errors():
    """Map requests failures to RexelConnectionError (single source of truth).

    ``from None`` matters: the original exception's text holds the URL, and with it the
    rexstand credentials.
    """
    try:
        yield
    except requests.exceptions.Timeout as exc:
        raise RexelConnectionError(f"Request timed out: {_without_query(exc)}") from None
    except requests.exceptions.ConnectionError as exc:
        raise RexelConnectionError(f"Connection failed: {_without_query(exc)}") from None
    except requests.exceptions.HTTPError as exc:
        raise RexelConnectionError(f"HTTP error: {_without_query(exc)}") from None
    except requests.exceptions.RequestException as exc:  # e.g. RetryError after repeated 503s
        raise RexelConnectionError(f"Request failed: {_without_query(exc)}") from None


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
        # rexstand GET). getBasket is NEVER retried — it uses a bare POST.
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

        Unknown codes are **dropped** (the API returns an inactive, empty-code record
        for them), so the result contains only real articles and may be shorter than
        ``item_codes``. Note the asymmetry with :meth:`price_get`, which answers with a
        row for an unknown code instead of dropping it.

        Rexel answers once per *requested* code, so asking for two identifiers of the
        same product yields two entries. Use :meth:`articles_by_code` to get replies
        matched back to what was asked, deduplicated.
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

    def articles_by_code(self, item_codes: list[str]) -> dict[str, Article]:
        """Fetch articles and map each requested code to the article it stands for.

        Keys are the requested codes verbatim; unknown ones are simply absent. A code
        matches on the article's ``item_code`` **or** ``erp_code`` — Rexel accepts a
        Rexel ref or an e-number and answers with the canonical Rexel ref either way —
        case-insensitively and ignoring surrounding whitespace. It does **not** match on
        ``supplier_item_code``: the manufacturer's own reference is not an identifier
        the service resolves, so asking for one returns nothing at all.

        The tolerance is this client's own: codes go on the wire upper-cased and
        stripped, so it does not rest on the remote service normalising for us. Codes
        that are blank once stripped are never sent — they cannot resolve to anything.

        Two identifiers of the same product map to the same Article, not to two copies.
        Conversely, requested codes that differ only in case or surrounding whitespace
        (``"ABC"`` and ``" abc "``) are asked once and answered twice: each keeps its
        own key, holding the same Article. Nothing is ever clobbered, because the keys
        are the caller's strings — only an exactly repeated string collapses, since a
        dict cannot hold it twice.
        """
        # One wire code per distinct identifier, in the order first asked for.
        canonical = [code for code in dict.fromkeys(_canonical(c) for c in item_codes) if code]

        # Index by every identifier the articles carry, first reply wins: the duplicate
        # replies for one product are identical, so they collapse onto one Article.
        by_identifier: dict[str, Article] = {}
        for article in self.article_get(canonical):
            for identifier in (article.item_code, article.erp_code):
                if identifier:
                    by_identifier.setdefault(_canonical(identifier), article)

        resolved: dict[str, Article] = {}
        for requested in item_codes:
            article = by_identifier.get(_canonical(requested))
            if article is not None:
                resolved[requested] = article
        return resolved

    # --- SOAP getPrices ----------------------------------------------------------------

    def price_get(self, items: list[tuple[str, int | Decimal]]) -> list[Price]:
        """Fetch live prices for (item_code, quantity) pairs. Auto-batches at ≤100.

        An unknown code still gets a **row** here (zero prices, no availability) rather
        than being dropped, so the result lines up one-to-one with ``items``, in order; a
        reply that does not raises :class:`RexelAPIError`. This is the opposite of
        :meth:`article_get`, which drops unknown codes — do not infer existence from a
        price row.

        Each row's amounts are line totals for its ``quantity``; see :class:`Price` for
        the per-unit ones. The service prices whole quantities, so a fractional quantity
        is truncated. A quantity below 1 raises ``ValueError``: Rexel would answer it
        with the amounts for 1.
        """
        for code, qty in items:
            if qty < 1:
                raise ValueError(f"Quantity for {code} must be at least 1, got {qty}")
        whole = [(code, int(qty)) for code, qty in items]
        prices: list[Price] = []
        for start in range(0, len(whole), self.batch_size):
            batch = whole[start : start + self.batch_size]
            body = soap.build_get_prices(
                self.customer_number, self.username, self.password, self.language, batch
            )
            rows = soap.parse_prices(self._post_soap(body))
            if len(rows) != len(batch):
                raise RexelAPIError(-1, f"getPrices: {len(rows)} rows for {len(batch)} items")
            # By position: an e-number comes back under its Rexel ref, an unknown code under
            # no code at all, and one code can be asked twice at two quantities.
            for price, (_, qty) in zip(rows, batch):
                price.quantity = qty
            prices.extend(rows)
        return prices

    # --- SOAP getBasket (one-shot) ------------------------------------------------------

    def basket_get_raw(self) -> str | None:
        """Fetch the NetStore basket and return the raw inner ``<Content>`` XML string.

        **One-shot**: call it only when you mean to, and keep what it returns. It is never
        retried. Callers get the raw XML so they can persist it before parsing.

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

    def catalog_download_snapshot(self) -> CatalogSnapshot:
        """Download the weekly ``rexstand`` ZIP as a whole :class:`CatalogSnapshot`.

        Prefer this over :meth:`catalog_download` when the caller archives the ZIP or
        decides whether to refresh: the digest and the generation stamp come back with
        the rows instead of having to be recovered from the bytes a second time.
        """
        return parse_catalog_snapshot(self.catalog_download_raw())

    def catalog_download_raw(self) -> bytes:
        """Download the ``rexstand`` ZIP and return the raw bytes (for archival).

        The sync job archives this verbatim as an attachment so every DBF column is
        preserved week-by-week. Parsing is separate — see
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
