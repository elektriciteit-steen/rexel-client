"""SOAP envelope building and response parsing for the ``rexel`` service.

The service is Apache CXF, document/literal, SOAP 1.1, namespace ``rexel.ws.service``
(``elementFormDefault="unqualified"`` — so the operation element is namespaced but its
children are not). Three operations: ``getArticleInformation``, ``getPrices``,
``getBasket``.

Quirk worth knowing: the response wraps its real payload as an **escaped XML string**
inside a single ``<return>`` element. That inner document is::

    <Root>
      <Status><StatusCode>0</StatusCode><StatusDescription>…</StatusDescription></Status>
      <Content>…ArticleDetail / PriceDetail / OrderLine…</Content>
    </Root>

So parsing is two-stage: unwrap the SOAP envelope to get the ``<return>`` text, then
parse that text as its own document.

Kept to the stdlib ``ElementTree`` (no lxml) to mirror cebeo-client exactly and avoid a
compiled dependency in Odoo workers; the payloads are small and fixed-shape.
"""

import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

from .exceptions import RexelAPIError, RexelAuthError
from .models import Article, Price

# Namespaces
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
SERVICE_NS = "rexel.ws.service"

# Status code → exception class. 0 is success (handled separately).
#   10 invalid user · 11 inactive user · 12 invalid password · 13 no web-service access
#   20 invalid customer · 21 ambiguous customer   → auth/access problems
#   -1 internal error · 30 GetBasket unavailable · 99 misconfigured → generic API errors
_AUTH_CODES = frozenset({10, 11, 12, 13, 20, 21})


ET.register_namespace("soapenv", SOAP_ENV)
ET.register_namespace("rex", SERVICE_NS)


# --- field parse helpers (shared with client/catalog) ---------------------------------


def to_decimal(value: str | None) -> Decimal:
    """Parse a Rexel decimal (dot separator, e.g. ``"6.3441"``). Empty → ``0``."""
    if not value:
        return Decimal("0")
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        return Decimal("0")


def to_int(value: str | None) -> int:
    """Parse an integer, tolerating decimal strings (``"9577.000"`` → ``9577``). Empty → 0."""
    if not value:
        return 0
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        try:
            return int(Decimal(value))
        except (InvalidOperation, ValueError):
            return 0


def to_bool(value: str | None) -> bool:
    """Parse a boolean; ``"true"`` (any case) → True, everything else → False."""
    return (value or "").strip().lower() == "true"


def text(element: ET.Element | None, tag: str, default: str = "") -> str:
    """Return the stripped text of ``element/tag``, or ``default``."""
    if element is None:
        return default
    child = element.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return default


# --- envelope building ----------------------------------------------------------------


def _envelope(operation: str) -> tuple[ET.Element, ET.Element]:
    """Build ``<Envelope><Body><op/></Body></Envelope>`` and return (root, op_element)."""
    root = ET.Element(f"{{{SOAP_ENV}}}Envelope")
    body = ET.SubElement(root, f"{{{SOAP_ENV}}}Body")
    op = ET.SubElement(body, f"{{{SERVICE_NS}}}{operation}")
    return root, op


def _auth(op: ET.Element, customer: str, user: str, password: str, language: str) -> None:
    """Append the four auth children every operation requires (unqualified)."""
    ET.SubElement(op, "customer").text = customer
    ET.SubElement(op, "user").text = user
    ET.SubElement(op, "password").text = password
    ET.SubElement(op, "language").text = language


def _serialize(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_get_article_information(
    customer: str, user: str, password: str, language: str, item_codes: list[str]
) -> bytes:
    """Build a ``getArticleInformation`` request (1–100 item codes)."""
    root, op = _envelope("getArticleInformation")
    _auth(op, customer, user, password, language)
    for code in item_codes:
        ET.SubElement(op, "itemCodes").text = code
    return _serialize(root)


def build_get_prices(
    customer: str,
    user: str,
    password: str,
    language: str,
    items: list[tuple[str, int]],
) -> bytes:
    """Build a ``getPrices`` request from (item_code, quantity) pairs (1–100)."""
    root, op = _envelope("getPrices")
    _auth(op, customer, user, password, language)
    for code, qty in items:
        item = ET.SubElement(op, "itemData")
        ET.SubElement(item, "itemCode").text = code
        ET.SubElement(item, "quantity").text = str(int(qty))
    return _serialize(root)


def build_get_basket(customer: str, user: str, password: str, language: str) -> bytes:
    """Build a ``getBasket`` request. ⚠️ Destructive on the server (consumes the basket)."""
    root, op = _envelope("getBasket")
    _auth(op, customer, user, password, language)
    return _serialize(root)


# --- response parsing -----------------------------------------------------------------


def _find_return_text(envelope: ET.Element) -> str:
    """Extract the inner XML string from the SOAP ``<return>`` element.

    Raises RexelAPIError on a SOAP Fault (e.g. the CXF ``Exception`` fault) or when no
    ``<return>`` element is present.
    """
    for el in envelope.iter():
        tag = el.tag.split("}")[-1]
        if tag == "Fault":
            reason = ""
            for child in el.iter():
                ctag = child.tag.split("}")[-1]
                if ctag in ("faultstring", "Text", "message") and child.text:
                    reason = child.text.strip()
                    break
            raise RexelAPIError(-1, f"SOAP fault: {reason or 'unknown'}")
    for el in envelope.iter():
        if el.tag.split("}")[-1] == "return":
            return el.text or ""
    raise RexelAPIError(-1, "Missing <return> element in SOAP response")


def parse_response(response_bytes: bytes) -> ET.Element:
    """Unwrap a SOAP response and return its inner ``<Content>`` element.

    Two-stage parse: SOAP envelope → ``<return>`` text → inner ``<Root>``. Validates the
    ``<Status>`` and raises on non-zero status codes.

    Raises:
        RexelAuthError: credential/access status codes (10-13, 20, 21).
        RexelAPIError: any other non-zero status code, or malformed XML.
    """
    try:
        envelope = ET.fromstring(response_bytes)
    except ET.ParseError as exc:
        raise RexelAPIError(-1, f"Invalid SOAP XML: {exc}") from exc

    inner_xml = _find_return_text(envelope)
    try:
        root = ET.fromstring(inner_xml.encode("utf-8"))
    except ET.ParseError as exc:
        raise RexelAPIError(-1, f"Invalid inner XML payload: {exc}") from exc

    status = root.find("Status")
    code = to_int(text(status, "StatusCode"))
    description = text(status, "StatusDescription")

    if code != 0:
        if code in _AUTH_CODES:
            raise RexelAuthError(code, description or "authentication/access error")
        raise RexelAPIError(code, description or "API error")

    content = root.find("Content")
    if content is None:
        # A successful status with no Content is a valid empty result.
        return ET.Element("Content")
    return content


def content_to_string(content: ET.Element) -> str:
    """Serialize a ``<Content>`` element back to an XML string (for raw archival)."""
    return ET.tostring(content, encoding="unicode")


# --- domain mapping (SOAP XML -> models) ----------------------------------------------
# Kept here so soap.py owns the full SOAP contract: envelope build, status unwrap, and
# the ArticleDetail/PriceDetail tag mapping all live together. The transport swap
# (~2027 Group API) replaces this file without touching RexelClient's public surface.


def parse_articles(content: ET.Element) -> list[Article]:
    """Map ``<ArticleDetail>`` children to Article models.

    Unknown item codes come back as an inactive record with an empty ``ItemCode``;
    those placeholders are dropped so the result holds only real articles.
    """
    articles: list[Article] = []
    for detail in content.findall("ArticleDetail"):
        item_code = text(detail, "ItemCode")
        if not item_code:
            continue
        articles.append(
            Article(
                item_code=item_code,
                supplier_item_code=text(detail, "SupplierItemCode"),
                ean=text(detail, "EanCode"),
                description=text(detail, "ItemDescription"),
                long_description=text(detail, "LongItemDescription"),
                sales_unit_code=text(detail, "SalesUnitCode"),
                multiple=to_decimal(text(detail, "Multiple")),
                box_quantity1=to_decimal(text(detail, "BoxQuantity1")),
                box_quantity2=to_decimal(text(detail, "BoxQuantity2")),
                brand=text(detail, "BrandDescription"),
                image_url_high=text(detail, "ImageLinkHigh"),
                image_url_low=text(detail, "ImageLinkLow"),
                pdf_url=text(detail, "LinkToPDF"),
                item_url=text(detail, "DirectItemLink"),
                erp_code=text(detail, "ERPCode"),
                customer_item_reference=text(detail, "CustomerItemReference"),
                is_active=to_bool(text(detail, "IsActive")),
            )
        )
    return articles


def parse_prices(content: ET.Element) -> list[Price]:
    """Map ``<PriceDetail>`` children to Price models.

    ``Available`` arrives as a decimal quantity string; it is normalised to an int
    ``available_qty`` plus an ``available`` bool.
    """
    prices: list[Price] = []
    for detail in content.findall("PriceDetail"):
        available_raw = text(detail, "Available")
        available_qty = to_int(available_raw) if available_raw else None
        lead_raw = text(detail, "LeadTime")
        prices.append(
            Price(
                item_code=text(detail, "ItemCode"),
                supplier_item_code=text(detail, "SupplierItemCode"),
                ean=text(detail, "EanCode"),
                description=text(detail, "ItemDescription"),
                sales_unit_code=text(detail, "SalesUnitCode"),
                base_price=to_decimal(text(detail, "BasePrice")),
                net_price=to_decimal(text(detail, "NetPrice")),
                total_surcharge=to_decimal(text(detail, "TotalSurcharge")),
                ecotax=to_decimal(text(detail, "Ecotax")),
                recupel=to_decimal(text(detail, "Recupel")),
                sabam=to_decimal(text(detail, "Sabam")),
                vat_code=to_decimal(text(detail, "VATCode")),
                currency=text(detail, "CurrencyCode"),
                available=(available_qty or 0) > 0,
                available_qty=available_qty,
                lead_time_days=to_int(lead_raw) if lead_raw else None,
            )
        )
    return prices
