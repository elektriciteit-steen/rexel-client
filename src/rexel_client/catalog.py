"""``rexstand`` eCatalog: download + DBF parsing.

The REST endpoint returns ``<customer>.zip`` containing ``<customer>.dbf`` (dBASE III,
no memo) — the agreed customer assortment with net prices + Belgian levies. This module
turns those bytes into typed :class:`~rexel_client.models.CatalogItem` rows.

Numeric DBF fields are parsed to ``Decimal`` (not float) via a custom field parser so
prices and levies keep exact scale.
"""

import hashlib
import io
import tempfile
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from dbfread import DBF, FieldParser

from .models import CatalogItem, CatalogSnapshot


class _DecimalFieldParser(FieldParser):
    """Parse numeric (``N``) DBF fields to ``Decimal`` instead of float."""

    def parseN(self, field, data):  # noqa: N802 (dbfread's required method name)
        data = data.strip().strip(b"*\x00")
        if not data:
            return None
        if field.decimal_count or b"." in data:
            return Decimal(data.decode())
        return int(data)


def _dec(value) -> Decimal:
    """Coerce a parsed DBF numeric (Decimal | int | None) to Decimal, ``0`` for empty."""
    if value is None:
        return Decimal("0")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _parse_catalog_date(value: str | None) -> date | None:
    """Parse the DBF ``DATE`` column (``"YYYY/MM/DD"``) to a date, or None."""
    if not value:
        return None
    try:
        year, month, day = (int(part) for part in value.strip().split("/"))
        return date(year, month, day)
    except (ValueError, TypeError):
        return None


def _row_to_item(row: dict) -> CatalogItem:
    """Map one DBF record (uppercase field names) to a CatalogItem."""
    return CatalogItem(
        prod_group=(row.get("PROD_GROUP") or "").strip(),
        prod_group_desc_nl=(row.get("PG_DESC_NL") or "").strip(),
        prod_group_desc_fr=(row.get("PG_DESC_FR") or "").strip(),
        supplier_ref=(row.get("SUPPL_REF") or "").strip(),
        item_code=(row.get("REXEL_REF") or "").strip(),
        description_nl=(row.get("DESC_NL") or "").strip(),
        description_fr=(row.get("DESC_FR") or "").strip(),
        stock_unit=(row.get("STOCK_UNIT") or "").strip(),
        brand=(row.get("FABRICANT") or "").strip(),
        catalog_date=_parse_catalog_date(row.get("DATE")),
        net_price=_dec(row.get("NET_PRICE")),
        gross_price=_dec(row.get("BRUT_PRICE")),
        ean=(row.get("EAN_CODE") or "").strip(),
        stock_item=bool(row.get("STOCK_ITEM")),
        recupel=_dec(row.get("RECUPEL")),
        auvibel=_dec(row.get("AUVIBEL")),
        bebat=_dec(row.get("BEBAT")),
        min_order_qty=_dec(row.get("MIN_ORDQTY")),
        url=(row.get("URL") or "").strip(),
        cable_base=_dec(row.get("CABLE_BASE")),
        cable_coef=_dec(row.get("CABLE_COEF")),
        cable_weight=_dec(row.get("CABLE_WEIG")),
        customer_ref=(row.get("CUST_REF") or "").strip(),
        erp_code=(row.get("ERP_NUMB") or "").strip(),
    )


def parse_catalog_dbf(dbf_bytes: bytes) -> list[CatalogItem]:
    """Parse raw ``.dbf`` bytes into CatalogItem rows.

    dbfread needs a filesystem path, so the bytes are written to a temp file.
    """
    with tempfile.NamedTemporaryFile(suffix=".dbf") as tmp:
        tmp.write(dbf_bytes)
        tmp.flush()
        table = DBF(
            tmp.name,
            parserclass=_DecimalFieldParser,
            char_decode_errors="replace",
            lowernames=False,
        )
        return [_row_to_item(row) for row in table]


def _entry_mtime(info: zipfile.ZipInfo) -> datetime | None:
    """The ZIP entry's stored mtime, or None when it is not a real date."""
    try:
        return datetime(*info.date_time)
    except ValueError:
        return None


def _dbf_entry(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    """The single ``.dbf`` member of a rexstand ZIP."""
    for info in archive.infolist():
        if Path(info.filename).suffix.lower() == ".dbf":
            return info
    raise ValueError("rexstand ZIP contains no .dbf file")


def parse_catalog_zip(zip_bytes: bytes) -> list[CatalogItem]:
    """Extract the ``.dbf`` from a ``rexstand`` ZIP and parse it into CatalogItem rows.

    Use :func:`parse_catalog_snapshot` when the download's identity (digest,
    generation stamp) matters too — it answers with all of it in one pass.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        dbf_bytes = archive.read(_dbf_entry(archive))
    return parse_catalog_dbf(dbf_bytes)


def parse_catalog_snapshot(zip_bytes: bytes) -> CatalogSnapshot:
    """Parse a ``rexstand`` ZIP into a whole :class:`CatalogSnapshot`.

    The generation stamp lives on the ZIP entry, not in the DBF, so it is only
    recoverable while the archive is open — reading it afterwards means unzipping
    the same bytes twice. This does both in one pass.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        entry = _dbf_entry(archive)
        generated_at = _entry_mtime(entry)
        items = parse_catalog_dbf(archive.read(entry))
    return CatalogSnapshot(
        raw=zip_bytes,
        sha256=hashlib.sha256(zip_bytes).hexdigest(),
        generated_at=generated_at,
        catalog_date=next((item.catalog_date for item in items if item.catalog_date), None),
        items=items,
    )
