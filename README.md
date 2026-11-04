# rexel-client

Python client for the **Rexel Belgium** web services - the transport layer for the
`connector_rexel` Odoo addon. It wraps the two Rexel data sources behind one stable
surface.

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

# …or matched back to what you asked for: {requested_code: Article}, deduplicated
by_code = client.articles_by_code(["NIK70036600", "E2222407"])

# Live prices + levies + availability for (item_code, quantity) pairs — auto-batched ≤100
prices = client.price_get([("LEG310084", 2)])
prices[0].net_price       # line total for the quantity asked
prices[0].unit_net_price  # net_price / 2, likewise unit_ecotax, unit_recupel, …

# Weekly bulk catalog: the agreed customer assortment with net prices + Belgian levies
items = client.catalog_download()               # -> list[CatalogItem]
raw_zip = client.catalog_download_raw()          # -> bytes, for archival
snapshot = client.catalog_download_snapshot()    # -> raw + sha256 + generated_at + items
```

### Price amounts are line totals

`getPrices` answers `NetPrice`, `TotalSurcharge`, `Ecotax`, `Recupel` and `Sabam` as line
totals for the requested quantity; only `BasePrice` is per unit. `Price` keeps those raw
totals and adds `quantity` plus `unit_net_price`, `unit_ecotax`, `unit_recupel`,
`unit_sabam` and `unit_total_surcharge`, not rounded to a money scale, so no caller divides
by hand. SOAP rounds levies to 2 decimals per unit before totalling them, so `unit_ecotax`
can differ from the invoice's 4-decimal tariff.

Rows pair with quantities by position. Rexel answers one row per requested line in order,
and a reply that does not raises `RexelAPIError`. The service prices whole quantities: a
fractional quantity is truncated, and one below 1 raises `ValueError` before any call
(Rexel would answer quantity 0 with the amounts for 1).

### Unknown codes: the two calls disagree

`article_get` **drops** a code it does not know (the service answers with an inactive,
empty-code record and the client filters it out), so the list can be shorter than what
you asked for. `price_get` **returns a row** for one instead. Never infer that an item
exists from a price row, and never assume either call answers one-to-one with its input.

Rexel also answers `getArticleInformation` once per *requested* code, so two identifiers
of one product come back twice. `articles_by_code()` resolves each requested code — by
Rexel ref or e-number, case- and whitespace-insensitively — to a single `Article`. The
tolerance is the client's own: codes go on the wire stripped and upper-cased, so it does
not rest on the remote service normalising them. Codes differing only in case or spacing
are asked once and answered twice — each keeps its own key, holding the same `Article`.
The manufacturer reference is not resolvable: the service returns nothing for it.

### `getBasket` is one-shot

Call it only when you mean to, and keep what it returns. The client never retries it and
returns the raw `<Content>` XML string (`basket_get_raw()`) so callers can persist it
before parsing.

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
- `CatalogSnapshot.catalog_date` is the *one* date the DBF rows agree on. Rows carrying
  more than one distinct `DATE` (blank counting as one) raise a `ValueError` rather than
  report an arbitrary one — it is refresh and archival metadata. A ZIP holding anything
  other than exactly one `.dbf` member is refused for the same reason.
- The production WSDL is vendored at `tests/fixtures/rexel_prod.wsdl` — a point-in-time
  copy of the contract (three operations, no more), with its source and refresh recipe in
  `tests/fixtures/PROVENANCE.md`. A test asserts the hand-rolled envelopes match it.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
pytest -v
```
