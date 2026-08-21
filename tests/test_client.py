"""Tests for RexelClient (SOAP) and the rexstand catalog parser.

Fixtures under ``tests/fixtures`` are real captured responses from the production
``rexel`` service (no credentials in them), plus two hand-built error envelopes and a
3-row DBF sliced from the real weekly catalog. ``rexel_prod.wsdl`` is the production
WSDL verbatim (public, unauthenticated) as it stood on its retrieval date — see
``tests/fixtures/PROVENANCE.md`` for its source URL and refresh recipe.
"""

import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import requests
import responses

from rexel_client import (
    Article,
    CatalogItem,
    CatalogSnapshot,
    Price,
    RexelAPIError,
    RexelAuthError,
    RexelClient,
    RexelConnectionError,
    soap,
)
from rexel_client.catalog import parse_catalog_snapshot, parse_catalog_zip

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


def load_fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def emitted_item_codes(call) -> list[str]:
    """The ``itemCodes`` actually put on the wire by a getArticleInformation call."""
    operation = ET.fromstring(call.request.body).find(f"{{{soap.SOAP_ENV}}}Body")[0]
    return [element.text for element in operation.findall("itemCodes")]


def rexstand_zip_with_dates(*dates: str) -> bytes:
    """The 3-row fixture ZIP, rewritten so its rows carry the given DATE values.

    DBF fields are fixed width, so each 10-char ``YYYY/MM/DD`` is overwritten in place
    (blanks padded with spaces) and every record offset survives untouched.
    """
    with zipfile.ZipFile(io.BytesIO(load_fixture_bytes("rexstand_999999.zip"))) as archive:
        name = archive.namelist()[0]
        dbf = bytearray(archive.read(name))
    offsets = [m.start() for m in re.finditer(rb"\d{4}/\d{2}/\d{2}", bytes(dbf))]
    assert len(offsets) == len(dates), "one replacement date per row required"
    for offset, value in zip(offsets, dates):
        dbf[offset : offset + 10] = value.ljust(10).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        out.writestr(name, bytes(dbf))
    return buf.getvalue()


@pytest.fixture
def client():
    return RexelClient(
        customer_number="123456",
        username="testuser",
        password="testpass",
        batch_size=50,
    )


class TestRexelClient:
    """Initialization and configuration."""

    def test_default_base_url(self):
        c = RexelClient("123456", "user", "pass")
        assert c.base_url == "https://services.rexel.be/exchange"
        assert c._soap_url == "https://services.rexel.be/exchange/ws/prod/rexel"

    def test_custom_base_url_is_stripped(self):
        c = RexelClient("123456", "user", "pass", base_url="https://test.example.com/")
        assert c.base_url == "https://test.example.com"

    def test_default_language_nl(self):
        assert RexelClient("123456", "u", "p").language == "NL"

    def test_batch_size_clamped_to_100(self):
        assert RexelClient("123456", "u", "p", batch_size=250).batch_size == 100


class TestArticleGet:
    """article_get / getArticleInformation."""

    @responses.activate
    def test_article_get_success(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("get_article_information_response.xml"),
            status=200,
            content_type="text/xml",
        )
        articles = client.article_get(["NIK70036600", "ZZZDOESNOTEXIST999"])

        # Unknown code is dropped -> only the real article remains.
        assert len(articles) == 1
        art = articles[0]
        assert isinstance(art, Article)
        assert art.item_code == "NIK70036600"
        assert art.supplier_item_code == "700-36600"
        assert art.ean == "5413736277236"
        assert art.brand == "NIKO"
        assert art.sales_unit_code == "P"
        assert art.multiple == Decimal("1.000")
        assert art.box_quantity1 == Decimal("10.000")
        assert art.erp_code == "E2222407"
        assert art.image_url_high.endswith("/IMAGE/NIK/NIK70036600.png")
        assert art.is_active is True

    def test_article_get_empty_input_no_call(self, client):
        # No HTTP mock registered -> proves no request is made.
        assert client.article_get([]) == []

    @responses.activate
    def test_article_get_batches(self):
        c = RexelClient("123456", "u", "p", batch_size=50)
        responses.add(
            responses.POST,
            c._soap_url,
            body=load_fixture("get_article_information_response.xml"),
            status=200,
            content_type="text/xml",
        )
        c.article_get([f"CODE{i}" for i in range(150)])
        assert len(responses.calls) == 3  # 150 / 50


class TestArticlesByCode:
    """articles_by_code: replies matched back to the codes that were asked for."""

    @responses.activate
    def test_two_identifiers_resolve_to_one_article(self, client):
        # Rexel answers once per requested code, so asking for a Rexel ref and its
        # e-number returns the same product twice.
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("get_article_information_duplicate_response.xml"),
            status=200,
            content_type="text/xml",
        )
        found = client.articles_by_code(["NIK70036600", "E2222407"])
        assert set(found) == {"NIK70036600", "E2222407"}
        assert found["NIK70036600"] is found["E2222407"]
        assert found["E2222407"].item_code == "NIK70036600"

    @responses.activate
    def test_match_is_case_and_whitespace_insensitive(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("get_article_information_response.xml"),
            status=200,
            content_type="text/xml",
        )
        found = client.articles_by_code(["  nik70036600  "])
        # The tolerance is ours, not the remote service's: the code goes out canonical.
        assert emitted_item_codes(responses.calls[0]) == ["NIK70036600"]
        # Keyed by the requested string verbatim, so the caller can look it back up.
        assert list(found) == ["  nik70036600  "]
        assert found["  nik70036600  "].item_code == "NIK70036600"

    @responses.activate
    def test_codes_differing_only_in_case_are_asked_once_and_answered_twice(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("get_article_information_response.xml"),
            status=200,
            content_type="text/xml",
        )
        found = client.articles_by_code(["NIK70036600", "  nik70036600  "])
        assert emitted_item_codes(responses.calls[0]) == ["NIK70036600"]
        # Both spellings keep their own key; neither overwrites the other.
        assert list(found) == ["NIK70036600", "  nik70036600  "]
        assert found["NIK70036600"] is found["  nik70036600  "]

    @responses.activate
    def test_blank_codes_are_never_sent(self, client):
        # No HTTP mock registered -> proves nothing goes on the wire for codes that
        # cannot resolve to anything.
        assert client.articles_by_code(["", "   "]) == {}

    @responses.activate
    def test_unknown_code_is_absent(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("get_article_information_response.xml"),
            status=200,
            content_type="text/xml",
        )
        found = client.articles_by_code(["NIK70036600", "ZZZDOESNOTEXIST999"])
        assert list(found) == ["NIK70036600"]

    @responses.activate
    def test_supplier_item_code_does_not_match(self, client):
        # The manufacturer reference is not an identifier the service resolves
        # (verified live: asking for it returns zero articles), so it must not be
        # matched here either — that would invent a resolution Rexel does not offer.
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("get_article_information_response.xml"),
            status=200,
            content_type="text/xml",
        )
        assert client.articles_by_code(["700-36600"]) == {}

    def test_empty_input_no_call(self, client):
        # No HTTP mock registered -> proves no request is made.
        assert client.articles_by_code([]) == {}


class TestPriceGet:
    """price_get / getPrices."""

    @responses.activate
    def test_price_get_success(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("get_prices_response.xml"),
            status=200,
            content_type="text/xml",
        )
        prices = client.price_get([("NIK70036600", 1), ("LEG010424", 5)])
        assert len(prices) == 2
        p = prices[0]
        assert isinstance(p, Price)
        assert p.item_code == "NIK70036600"
        assert p.net_price == Decimal("6.3441")
        assert p.base_price == Decimal("11.1300")
        assert p.recupel == Decimal("0.0100")
        assert p.total_surcharge == Decimal("0.0100")
        assert p.vat_code == Decimal("21.00")
        assert p.currency == "EUR"
        # Available is a decimal qty string -> normalized to bool + int.
        assert p.available is True
        assert p.available_qty == 9576
        assert p.lead_time_days == 1

    @responses.activate
    def test_price_get_zero_availability(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("get_prices_response.xml"),
            status=200,
            content_type="text/xml",
        )
        prices = client.price_get([("NIK70036600", 1), ("LEG010424", 5)])
        out_of_stock = prices[1]
        assert out_of_stock.item_code == "LEG010424"
        assert out_of_stock.available is False
        assert out_of_stock.available_qty == 0

    def test_price_get_empty_input_no_call(self, client):
        assert client.price_get([]) == []

    @responses.activate
    def test_price_get_batches(self):
        c = RexelClient("123456", "u", "p", batch_size=50)
        responses.add(
            responses.POST,
            c._soap_url,
            body=load_fixture("get_prices_response.xml"),
            status=200,
            content_type="text/xml",
        )
        c.price_get([(f"CODE{i}", 1) for i in range(101)])
        assert len(responses.calls) == 3  # 101 -> 50 + 50 + 1


class TestUnknownCodeAsymmetry:
    """article_get drops an unknown code; price_get answers with a row for one.

    Verified live and documented nowhere until 0.2.0. The unknown price row's own
    field values are hand-built (only its *presence* was verified), so nothing here
    asserts its contents beyond the row count.
    """

    @responses.activate
    def test_article_get_returns_fewer_than_requested(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("get_article_information_response.xml"),
            status=200,
            content_type="text/xml",
        )
        assert len(client.article_get(["NIK70036600", "ZZZDOESNOTEXIST999"])) == 1

    @responses.activate
    def test_price_get_returns_a_row_for_an_unknown_code(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("get_prices_unknown_code_response.xml"),
            status=200,
            content_type="text/xml",
        )
        prices = client.price_get([("NIK70036600", 1), ("ZZZDOESNOTEXIST999", 1)])
        # Count only: the fixture's unknown row is hand-built, so this pins that the
        # parser keeps a placeholder row and is *no* evidence about its field values.
        assert len(prices) == 2


class TestErrorHandling:
    """Status-code mapping and transport failures."""

    @responses.activate
    def test_auth_error(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("auth_error_response.xml"),
            status=200,
            content_type="text/xml",
        )
        with pytest.raises(RexelAuthError) as exc:
            client.price_get([("NIK70036600", 1)])
        assert exc.value.code == 12

    @responses.activate
    def test_api_error(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("api_error_response.xml"),
            status=200,
            content_type="text/xml",
        )
        with pytest.raises(RexelAPIError) as exc:
            client.article_get(["NIK70036600"])
        assert exc.value.code == -1

    def test_auth_error_is_api_error_subclass(self):
        assert issubclass(RexelAuthError, RexelAPIError)

    @responses.activate
    def test_soap_fault_raises_api_error(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("soap_fault_response.xml"),
            status=500,
            content_type="text/xml",
        )
        # A SOAP Fault surfaces as an HTTP 500 first -> transport error.
        with pytest.raises(RexelConnectionError):
            client.article_get(["NIK70036600"])

    @responses.activate
    def test_soap_fault_without_http_error_raises_api_error(self, client):
        # Some CXF faults come back 200; those must map to RexelAPIError.
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("soap_fault_response.xml"),
            status=200,
            content_type="text/xml",
        )
        with pytest.raises(RexelAPIError):
            client.article_get(["NIK70036600"])

    @responses.activate
    def test_connection_error(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=requests.exceptions.ConnectionError("boom"),
        )
        with pytest.raises(RexelConnectionError):
            client.article_get(["NIK70036600"])


class TestCatalogDownload:
    """rexstand ZIP -> DBF parsing."""

    @responses.activate
    def test_catalog_download(self, client):
        responses.add(
            responses.GET,
            f"{client.base_url}/rs/prod/netstore/downloads/rexstand",
            body=load_fixture_bytes("rexstand_999999.zip"),
            status=200,
            content_type="application/octet-stream",
        )
        items = client.catalog_download()
        assert len(items) == 3
        item = items[0]
        assert isinstance(item, CatalogItem)
        assert item.item_code == "ABBKIM600763W"
        assert item.net_price == Decimal("12.53")
        assert item.gross_price == Decimal("17.9")
        assert item.recupel == Decimal("0.01")
        assert item.stock_item is True
        assert item.brand == "ABB"
        assert item.stock_unit == "P"
        assert item.ean == "8592624129301"
        assert item.erp_code == "E5420168"
        assert item.catalog_date is not None
        # Money is Decimal, never float.
        assert isinstance(item.net_price, Decimal)
        assert isinstance(item.min_order_qty, Decimal)

    @responses.activate
    def test_catalog_download_raw_returns_bytes(self, client):
        payload = load_fixture_bytes("rexstand_999999.zip")
        responses.add(
            responses.GET,
            f"{client.base_url}/rs/prod/netstore/downloads/rexstand",
            body=payload,
            status=200,
            content_type="application/octet-stream",
        )
        assert client.catalog_download_raw() == payload

    @responses.activate
    def test_catalog_download_sends_credentials(self, client):
        responses.add(
            responses.GET,
            f"{client.base_url}/rs/prod/netstore/downloads/rexstand",
            body=load_fixture_bytes("rexstand_999999.zip"),
            status=200,
        )
        client.catalog_download()
        request = responses.calls[0].request
        assert "customer=123456" in request.url
        assert "user=testuser" in request.url

    @responses.activate
    def test_catalog_download_http_error(self, client):
        responses.add(
            responses.GET,
            f"{client.base_url}/rs/prod/netstore/downloads/rexstand",
            status=401,
        )
        with pytest.raises(RexelConnectionError):
            client.catalog_download()

    @responses.activate
    def test_catalog_download_snapshot(self, client):
        payload = load_fixture_bytes("rexstand_999999.zip")
        responses.add(
            responses.GET,
            f"{client.base_url}/rs/prod/netstore/downloads/rexstand",
            body=payload,
            status=200,
            content_type="application/octet-stream",
        )
        snapshot = client.catalog_download_snapshot()
        assert isinstance(snapshot, CatalogSnapshot)
        assert snapshot.raw == payload
        assert snapshot.sha256 == hashlib.sha256(payload).hexdigest()
        # The generation stamp is the ZIP entry's mtime — nowhere in the DBF itself.
        assert snapshot.generated_at == datetime(2026, 7, 2, 15, 15, 44)
        assert snapshot.catalog_date == snapshot.items[0].catalog_date
        assert len(snapshot.items) == 3
        assert snapshot.items[0].item_code == "ABBKIM600763W"

    def test_snapshot_reads_the_zip_once(self):
        # Same rows as the plain parse, so callers can switch without re-verifying.
        payload = load_fixture_bytes("rexstand_999999.zip")
        assert parse_catalog_snapshot(payload).items == parse_catalog_zip(payload)

    def test_snapshot_date_is_none_when_no_row_carries_one(self):
        snapshot = parse_catalog_snapshot(rexstand_zip_with_dates("", "", ""))
        assert snapshot.catalog_date is None
        assert len(snapshot.items) == 3

    def test_snapshot_rejects_disagreeing_row_dates(self):
        payload = rexstand_zip_with_dates("2026/07/01", "2026/07/08", "2026/07/01")
        with pytest.raises(ValueError, match="inconsistent DATE"):
            parse_catalog_snapshot(payload)
        # The rows themselves still parse — only the one-date claim is refused.
        assert {item.catalog_date for item in parse_catalog_zip(payload)} == {
            date(2026, 7, 1),
            date(2026, 7, 8),
        }

    def test_snapshot_rejects_a_mix_of_dated_and_blank_rows(self):
        with pytest.raises(ValueError, match=r"inconsistent DATE.*\(missing\)"):
            parse_catalog_snapshot(rexstand_zip_with_dates("2026/07/01", "", "2026/07/01"))

    def test_parse_catalog_zip_rejects_more_than_one_dbf(self):
        # Rexel ships exactly one DBF; picking among several would be arbitrary.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a.dbf", b"first")
            z.writestr("b.dbf", b"second")
        for parse in (parse_catalog_zip, parse_catalog_snapshot):
            with pytest.raises(ValueError, match="2 .dbf members"):
                parse(buf.getvalue())

    def test_parse_catalog_zip_rejects_a_duplicated_dbf_name(self):
        # Two members under one name: zipfile would resolve the name to the last one.
        buf = io.BytesIO()
        with pytest.warns(UserWarning, match="Duplicate name"), zipfile.ZipFile(buf, "w") as z:
            z.writestr("999999.dbf", b"first")
            z.writestr("999999.dbf", b"second")
        with pytest.raises(ValueError, match="2 .dbf members"):
            parse_catalog_zip(buf.getvalue())

    def test_parse_catalog_snapshot_without_dbf_raises(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("readme.txt", "no dbf here")
        with pytest.raises(ValueError, match="no .dbf"):
            parse_catalog_snapshot(buf.getvalue())

    def test_parse_catalog_zip_without_dbf_raises(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("readme.txt", "no dbf here")
        with pytest.raises(ValueError, match="no .dbf"):
            parse_catalog_zip(buf.getvalue())


class TestBasketGet:
    """getBasket is destructive: verify raw passthrough, empty handling, no retry."""

    @responses.activate
    def test_basket_get_raw_returns_content_xml(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("get_basket_response.xml"),
            status=200,
            content_type="text/xml",
        )
        raw = client.basket_get_raw()
        # Raw <Content> passed through verbatim for persist-before-parse (Phase 4).
        assert raw is not None
        assert "<OrderLine>" in raw
        assert "NIK70036600" in raw

    @responses.activate
    def test_basket_get_raw_empty_returns_none(self, client):
        responses.add(
            responses.POST,
            client._soap_url,
            body=load_fixture("get_basket_empty_response.xml"),
            status=200,
            content_type="text/xml",
        )
        assert client.basket_get_raw() is None

    @responses.activate
    def test_basket_get_raw_no_retry(self, client):
        # A 503 must surface immediately, not be retried away.
        responses.add(
            responses.POST,
            client._soap_url,
            status=503,
        )
        with pytest.raises(RexelConnectionError):
            client.basket_get_raw()
        assert len(responses.calls) == 1


class TestWsdlContract:
    """The vendored production WSDL: what the service declared on its retrieval date.

    Kept in-repo (public, 6.7 KB, unauthenticated) so the contract can be read and
    asserted against without a network call — and so a future 'add an operation'
    request meets the declaration first. It is a snapshot, not live authority: these
    tests catch drift in our builders, not in Rexel's service. Source URL, retrieval
    date and refresh recipe live in ``tests/fixtures/PROVENANCE.md``.
    """

    WSDL = "{http://schemas.xmlsoap.org/wsdl/}"

    def _definitions(self):
        return ET.fromstring((FIXTURES_DIR / "rexel_prod.wsdl").read_bytes())

    def _declared_operations(self, definitions):
        port_type = definitions.find(f"{self.WSDL}portType")
        return {op.get("name") for op in port_type.findall(f"{self.WSDL}operation")}

    def test_service_declares_exactly_three_operations(self):
        declared = self._declared_operations(self._definitions())
        assert declared == {"getArticleInformation", "getPrices", "getBasket"}

    def test_built_envelopes_match_the_declared_operations(self):
        definitions = self._definitions()
        declared = self._declared_operations(definitions)
        assert definitions.get("targetNamespace") == soap.SERVICE_NS

        built = [
            soap.build_get_article_information("c", "u", "p", "NL", ["X"]),
            soap.build_get_prices("c", "u", "p", "NL", [("X", 1)]),
            soap.build_get_basket("c", "u", "p", "NL"),
        ]
        for envelope in built:
            operation = ET.fromstring(envelope).find(f"{{{soap.SOAP_ENV}}}Body")[0]
            namespace, _, name = operation.tag.partition("}")
            assert namespace.lstrip("{") == soap.SERVICE_NS
            assert name in declared
