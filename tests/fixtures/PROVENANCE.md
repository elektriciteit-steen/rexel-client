# Fixture provenance

## `rexel_prod.wsdl`

- **Source:** `https://services.rexel.be/exchange/ws/prod/rexel?wsdl` (public, unauthenticated)
- **Retrieved:** 2026-08-21
- **Refresh:** re-fetch with
  `curl -s 'https://services.rexel.be/exchange/ws/prod/rexel?wsdl' > tests/fixtures/rexel_prod.wsdl`
  and commit the diff — before adding an operation, or whenever the live contract is in doubt.

This is a **point-in-time copy**, not live authority: it says what the service declared on
the retrieval date. `TestWsdlContract` asserts our hand-rolled envelopes against *this*
snapshot, which catches drift in our builders, not drift in Rexel's service.

## Other fixtures

Captured production responses (no credentials in them), plus two hand-built error
envelopes, the hand-built `get_prices_unknown_code_response.xml`, and a 3-row DBF sliced
from the real weekly catalog.

## `get_prices_quantities_response.xml`

One live `getPrices` call, recorded on 2026-11-04 (a read-only call; the response echoes
neither credentials nor the customer number, checked before committing). Requested, in this
order: `NIK76136605` x10, `ZZZDOESNOTEXIST999` x3, `E2222407` x4, `LEG310084` x2,
`NIK76136605` x1. It pins what a hand-built fixture cannot: one row per requested line in
request order, including the unknown code and the repeated one.

**The money is made up** (BasePrice, NetPrice, TotalSurcharge, Ecotax, Recupel, Sabam).
Everything else is as recorded. The recorded amounts were line totals: they agreed with the
vendor bill's rows at these quantities.
