"""Tests for RexelClient (SOAP) and the rexstand catalog parser.

Fixtures under ``tests/fixtures`` are real captured responses from the production
``rexel`` service (no credentials in them), plus two hand-built error envelopes and a
3-row DBF sliced from the real weekly catalog.
"""

from decimal import Decimal
from pathlib import Path

import pytest
import requests
import responses

from rexel_client import (
    Article,
    CatalogItem,
    Price,
    RexelAPIError,
    RexelAuthError,
    RexelClient,
    RexelConnectionError,
)
from rexel_client.catalog import parse_catalog_zip

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


def load_fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


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
        assert p.net_price == Decimal("5.5000")
        assert p.base_price == Decimal("10.0000")
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
        assert item.net_price == Decimal("10.00")
        assert item.gross_price == Decimal("15.0")
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

    def test_parse_catalog_zip_without_dbf_raises(self):
        import io
        import zipfile

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
