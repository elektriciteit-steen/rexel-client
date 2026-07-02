# rexel-client

Python client for the **Rexel Belgium** web services - the transport layer for the
`connector_rexel` Odoo addon. It wraps the two Rexel data sources behind one stable
surface so that the coming Rexel Group-wide API (expected ~2027) can be swapped in here
without touching Odoo.

- **SOAP** `getArticleInformation`, `getPrices`, `getBasket` on
  `https://services.rexel.be/exchange/ws/prod/rexel` (Apache CXF, document/literal).
- **REST** `rexstand` weekly bulk catalog (a zipped dBASE-III `.dbf`) on
  `…/exchange/rs/prod/netstore/downloads/rexstand`.

## Install

```bash
pip install rexel-client
```

## Usage

```python
from decimal import Decimal
from rexel_client import RexelClient

client = RexelClient(
    customer_number="123456",
    username="…",
    password="…",
    language="NL",       # or "FR"
)

# Article enrichment (descriptions, brand, images, EAN, packaging) — auto-batched ≤100
articles = client.article_get(["NIK70036600"])

# Live prices + levies + availability for (item_code, quantity) pairs — auto-batched ≤100
prices = client.price_get([("NIK70036600", 1)])

# Weekly bulk catalog: the agreed customer assortment with net prices + Belgian levies
items = client.catalog_download()          # -> list[CatalogItem]
raw_zip = client.catalog_download_raw()     # -> bytes, for archival
```

### ⚠️ `getBasket` is destructive

Reading the NetStore basket **empties it** on the server. The client never retries it and
returns the raw `<Content>` XML string (`basket_get_raw()`) so callers can persist it
before parsing. Structured basket modelling lands in Phase 4.

## Design notes

- **Money is `Decimal`** everywhere — prices and Belgian levies (ecotax, recupel, sabam,
  auvibel, bebat) keep exact scale.
- **No lxml / no zeep.** The three SOAP operations are document/literal with a fixed
  namespace; envelopes are built and parsed with the stdlib `ElementTree`. This mirrors
  cebeo-client and keeps a compiled dependency out of Odoo workers. The only non-stdlib
  runtime deps are `requests` and `dbfread`.
- The SOAP response wraps its real payload as an **escaped XML string** inside a single
  `<return>` element (`<Root><Status/><Content/></Root>`); parsing is two-stage.
- Status codes: `0` OK · `10–13`/`20`/`21` → `RexelAuthError` · `-1`/`30`/`99`/other →
  `RexelAPIError` · transport failures → `RexelConnectionError`.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
pytest -v
```
