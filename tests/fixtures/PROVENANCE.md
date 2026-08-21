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
