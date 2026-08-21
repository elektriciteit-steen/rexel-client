"""Data models for Rexel API responses.

Money is always ``Decimal`` (never float) so levies and prices round-trip exactly.
Field shapes are grounded in real captured responses from the ``rexel`` SOAP service
and the ``rexstand`` DBF (see tests/fixtures).
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass
class Article:
    """One ``<ArticleDetail>`` from SOAP ``getArticleInformation``.

    The API returns an ArticleDetail even for unknown codes (``is_active=False``
    with an empty ``item_code``); the client drops those, so every Article here
    corresponds to a real item.
    """

    item_code: str  # Rexel item code (the enrich match key)
    supplier_item_code: str  # manufacturer's own reference
    ean: str
    description: str  # short description
    long_description: str
    sales_unit_code: str  # e.g. "P" (piece), "M" (meter)
    multiple: Decimal  # order multiple
    box_quantity1: Decimal
    box_quantity2: Decimal
    brand: str  # BrandDescription
    image_url_high: str
    image_url_low: str
    pdf_url: str  # LinkToPDF (often empty)
    item_url: str  # DirectItemLink (NetStore deep link)
    erp_code: str  # Rexel e-number ("E…")
    customer_item_reference: str
    is_active: bool = True


@dataclass
class Price:
    """One ``<PriceDetail>`` from SOAP ``getPrices``.

    ``Available`` comes back as a decimal quantity string (e.g. ``"9577.000"``);
    it is normalised to ``available_qty`` (int) plus an ``available`` bool.
    """

    item_code: str
    supplier_item_code: str
    ean: str
    description: str
    sales_unit_code: str
    base_price: Decimal  # BasePrice (gross/list)
    net_price: Decimal  # NetPrice (customer net)
    total_surcharge: Decimal  # sum of the levies below
    ecotax: Decimal
    recupel: Decimal
    sabam: Decimal
    vat_code: Decimal  # VAT rate, e.g. Decimal("21.00")
    currency: str  # CurrencyCode, e.g. "EUR"
    available: bool
    available_qty: int | None
    lead_time_days: int | None


@dataclass
class CatalogItem:
    """One row of the ``rexstand`` DBF (the agreed customer assortment).

    Carries all 24 DBF columns typed. The cable-pricing columns
    (``cable_base``/``cable_coef``/``cable_weight``) are all zero in the current
    assortment but are modelled so the raw catalog round-trips faithfully.
    """

    prod_group: str  # PROD_GROUP
    prod_group_desc_nl: str  # PG_DESC_NL
    prod_group_desc_fr: str  # PG_DESC_FR
    supplier_ref: str  # SUPPL_REF (manufacturer reference)
    item_code: str  # REXEL_REF (the enrich match key)
    description_nl: str  # DESC_NL
    description_fr: str  # DESC_FR
    stock_unit: str  # STOCK_UNIT
    brand: str  # FABRICANT
    catalog_date: date | None  # DATE ("YYYY/MM/DD")
    net_price: Decimal  # NET_PRICE (customer net)
    gross_price: Decimal  # BRUT_PRICE (gross/list)
    ean: str  # EAN_CODE
    stock_item: bool  # STOCK_ITEM (1/0)
    recupel: Decimal  # RECUPEL
    auvibel: Decimal  # AUVIBEL
    bebat: Decimal  # BEBAT
    min_order_qty: Decimal  # MIN_ORDQTY
    url: str  # URL (NetStore deep link)
    cable_base: Decimal  # CABLE_BASE
    cable_coef: Decimal  # CABLE_COEF
    cable_weight: Decimal  # CABLE_WEIG
    customer_ref: str  # CUST_REF
    erp_code: str  # ERP_NUMB (Rexel e-number)


@dataclass
class CatalogSnapshot:
    """One ``rexstand`` download, whole: the bytes, their identity, and the rows.

    A refresh decision needs more than the rows — it needs to know *which* download
    these rows came from. The ZIP is byte-identical between downloads of the same
    weekly publication, so ``sha256`` is a valid change key: an unchanged digest
    means nothing new was published.

    ``generated_at`` is the mtime of the ``.dbf`` member inside the ZIP (when Rexel
    built the export); ``catalog_date`` is the DBF ``DATE`` column, uniform across
    rows (the assortment's price-validity date). They are different clocks and can
    legitimately disagree. Uniformity is enforced, not assumed: rows carrying more
    than one distinct ``DATE`` (blank counting as one of them) are refused with a
    ``ValueError`` instead of reported as any one of them.
    """

    raw: bytes  # the ZIP verbatim, for archival
    sha256: str  # hex digest of ``raw`` — the change key
    generated_at: datetime | None  # ZIP member mtime; None if the entry carries none
    catalog_date: date | None  # DBF DATE column; None if the rows carry none
    items: list[CatalogItem]
