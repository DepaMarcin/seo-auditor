"""Testy SEOScraper: pobieranie strony, detekcja On-Page oraz Schema.org/FAQ/typ podstrony.

Wszystkie żądania HTTP są mockowane (unittest.mock) - żaden test nie łączy się
z prawdziwym internetem.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
from django.test import SimpleTestCase

from auditor.services.scraper import ScraperError, SEOScraper
from auditor.services.url_guard import UnsafeUrlError


def _fake_response(status_code: int, text: str = "", url: str = "https://example.com") -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(status_code, request=request, text=text)


def _fake_redirect(url: str, location: str, status_code: int = 301) -> httpx.Response:
    """Odpowiedź przekierowująca z ustawionym `next_request`.

    Przy `follow_redirects=False` to httpx wypełnia `response.next_request` kolejnym
    żądaniem (patrz `_send_handling_redirects`) - atrapa musi to odwzorować, bo scraper
    właśnie stamtąd bierze adres następnego skoku do walidacji.
    """
    response = httpx.Response(
        status_code, request=httpx.Request("GET", url), headers={"location": location}
    )
    response.next_request = httpx.Request("GET", location)
    return response


class SEOScraperFetchTests(SimpleTestCase):
    """Pobieranie strony: sukces (200), awarie (timeout / HTTP 500) oraz obsługa
    przekierowań, które od czasu wdrożenia ochrony przed SSRF są wykonywane ręcznie
    (każdy skok przechodzi `validate_public_url`).

    `validate_public_url` jest tu podmieniane, bo wykonuje odpytanie DNS - te testy
    sprawdzają zachowanie samego pobierania, a nie walidatora (ten ma własny zestaw
    testów w `test_url_guard.py`).
    """

    def setUp(self):
        self.scraper = SEOScraper()
        guard_patcher = patch(
            "auditor.services.scraper.validate_public_url", side_effect=lambda url: url
        )
        self.mock_guard = guard_patcher.start()
        self.addCleanup(guard_patcher.stop)

        client_patcher = patch("auditor.services.scraper.httpx.Client")
        self.mock_client_cls = client_patcher.start()
        self.addCleanup(client_patcher.stop)
        self.mock_client = self.mock_client_cls.return_value.__enter__.return_value

    def test_fetch_success_returns_html(self):
        self.mock_client.get.return_value = _fake_response(200, text="<html><body>OK</body></html>")

        html = self.scraper.fetch("https://example.com")

        self.assertEqual(html, "<html><body>OK</body></html>")
        self.mock_client.get.assert_called_once_with("https://example.com")

    def test_fetch_timeout_raises_scraper_error(self):
        self.mock_client.get.side_effect = httpx.ReadTimeout(
            "Timed out", request=httpx.Request("GET", "https://example.com")
        )

        with self.assertRaises(ScraperError):
            self.scraper.fetch("https://example.com")

    def test_fetch_http_500_raises_scraper_error(self):
        self.mock_client.get.return_value = _fake_response(500, text="Internal Server Error")

        with self.assertRaises(ScraperError):
            self.scraper.fetch("https://example.com")

    def test_fetch_connection_error_raises_scraper_error(self):
        self.mock_client.get.side_effect = httpx.ConnectError("Connection refused")

        with self.assertRaises(ScraperError):
            self.scraper.fetch("https://example.com")

    def test_scrape_normalizes_scheme_less_url_and_parses(self):
        self.mock_client.get.return_value = _fake_response(
            200, text="<html><head><title>Test</title></head><body><h1>H1</h1></body></html>"
        )

        data = self.scraper.scrape("example.com")

        self.assertEqual(data["url"], "https://example.com")
        self.assertEqual(data["title"], "Test")
        self.mock_client.get.assert_called_once_with("https://example.com")

    def test_fetch_follows_redirect_and_counts_hops(self):
        self.mock_client.get.side_effect = [
            _fake_redirect("https://example.com", "https://example.com/final"),
            _fake_response(200, text="<html>ok</html>", url="https://example.com/final"),
        ]

        html = self.scraper.fetch("https://example.com")

        self.assertEqual(html, "<html>ok</html>")
        self.assertEqual(self.scraper._last_redirect_count, 1)

    def test_fetch_rejects_redirect_to_private_address(self):
        """Publiczny adres nie może przez przekierowanie wprowadzić scrapera do sieci
        lokalnej - drugi skok jest walidowany tak samo jak adres wejściowy."""
        self.mock_client.get.return_value = _fake_redirect(
            "https://example.com", "http://127.0.0.1:8000/admin/", status_code=302
        )
        self.mock_guard.side_effect = [
            "https://example.com",
            UnsafeUrlError("Adresy w sieci lokalnej nie podlegają audytowi."),
        ]

        with self.assertRaises(ScraperError):
            self.scraper.fetch("https://example.com")


class SEOScraperOnPageTests(SimpleTestCase):
    """Detekcja struktur On-Page: Title, Meta Description, H1-H6, obrazki bez ALT."""

    def setUp(self):
        self.scraper = SEOScraper()

    def test_detects_title_and_meta_description(self):
        html = (
            "<html><head><title>Tytuł testowej strony</title>"
            '<meta name="description" content="Opis testowej strony"></head>'
            "<body></body></html>"
        )
        data = self.scraper.parse(html, "https://example.com")

        self.assertEqual(data["title"], "Tytuł testowej strony")
        self.assertEqual(data["meta_description"], "Opis testowej strony")

    def test_missing_title_and_meta_description(self):
        html = "<html><head></head><body></body></html>"
        data = self.scraper.parse(html, "https://example.com")

        self.assertIsNone(data["title"])
        self.assertIsNone(data["meta_description"])
        self.assertEqual(data["title_length"], 0)
        self.assertEqual(data["meta_description_length"], 0)

    def test_detects_all_heading_levels_h1_to_h6(self):
        headings_html = "".join(f"<h{level}>Nagłówek {level}</h{level}>" for level in range(1, 7))
        html = f"<html><body>{headings_html}</body></html>"

        data = self.scraper.parse(html, "https://example.com")

        for level in range(1, 7):
            self.assertEqual(data["headings"][f"h{level}"], [f"Nagłówek {level}"])
        self.assertEqual(data["h1_count"], 1)

    def test_missing_h1(self):
        html = "<html><body><h2>Tylko H2</h2></body></html>"
        data = self.scraper.parse(html, "https://example.com")

        self.assertEqual(data["h1_count"], 0)

    def test_images_without_alt_are_counted(self):
        html = (
            "<html><body>"
            '<img src="a.jpg" alt="Opis obrazka">'
            '<img src="b.jpg" alt="">'
            '<img src="c.jpg">'
            "</body></html>"
        )
        data = self.scraper.parse(html, "https://example.com")

        self.assertEqual(data["images_total"], 3)
        self.assertEqual(data["images_with_alt"], 1)
        self.assertEqual(data["images_without_alt"], 2)

    def test_no_images_on_page(self):
        html = "<html><body><p>Brak obrazków</p></body></html>"
        data = self.scraper.parse(html, "https://example.com")

        self.assertEqual(data["images_total"], 0)
        self.assertEqual(data["images_with_alt"], 0)
        self.assertEqual(data["images_without_alt"], 0)

    def test_canonical_and_open_graph_extraction(self):
        html = (
            "<html><head>"
            '<link rel="canonical" href="https://example.com/kanoniczny">'
            '<meta property="og:title" content="Tytuł OG">'
            '<meta property="og:description" content="Opis OG">'
            "</head><body></body></html>"
        )
        data = self.scraper.parse(html, "https://example.com")

        self.assertEqual(data["canonical"], "https://example.com/kanoniczny")
        self.assertEqual(data["open_graph"]["title"], "Tytuł OG")
        self.assertEqual(data["open_graph"]["description"], "Opis OG")
        self.assertNotIn("image", data["open_graph"])


class SchemaOrgExtractionTests(SimpleTestCase):
    """Ekstrakcja @type z JSON-LD i Microdata (itemscope/itemtype)."""

    def setUp(self):
        self.scraper = SEOScraper()

    def test_extracts_type_from_json_ld(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">{"@type": "Article"}</script>'
            "</head><body></body></html>"
        )
        data = self.scraper.parse(html, "https://example.com")

        self.assertIn("Article", data["schema"]["types_found"])
        self.assertIn("Article", data["schema"]["json_ld_types"])

    def test_extracts_multiple_types_from_json_ld_graph(self):
        html = (
            "<html><head><script type=\"application/ld+json\">"
            '{"@context": "https://schema.org", "@graph": ['
            '{"@type": "Organization"}, {"@type": "WebSite"}]}'
            "</script></head><body></body></html>"
        )
        data = self.scraper.parse(html, "https://example.com")

        self.assertIn("Organization", data["schema"]["types_found"])
        self.assertIn("WebSite", data["schema"]["types_found"])

    def test_extracts_type_from_microdata(self):
        html = (
            '<html><body><div itemscope itemtype="https://schema.org/Product">'
            "<h1>Produkt</h1></div></body></html>"
        )
        data = self.scraper.parse(html, "https://example.com")

        self.assertIn("Product", data["schema"]["types_found"])
        self.assertIn("Product", data["schema"]["microdata_types"])

    def test_invalid_json_ld_counts_as_parse_error_without_crashing(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">{niepoprawny json</script>'
            "</head><body></body></html>"
        )
        data = self.scraper.parse(html, "https://example.com")

        self.assertEqual(data["schema"]["parse_errors"], 1)
        self.assertEqual(data["schema"]["types_found"], [])


class BreadcrumbListUniversalRuleTests(SimpleTestCase):
    """Uniwersalna reguła 1: BreadcrumbList powinien wystąpić na każdej podstronie."""

    def setUp(self):
        self.scraper = SEOScraper()

    def test_breadcrumblist_detected(self):
        html = (
            "<html><head><script type=\"application/ld+json\">"
            '{"@type": "BreadcrumbList", "itemListElement": []}'
            "</script></head><body></body></html>"
        )
        data = self.scraper.parse(html, "https://example.com/dowolna-podstrona")

        self.assertIn("BreadcrumbList", data["schema"]["types_found"])

    def test_breadcrumblist_missing(self):
        html = "<html><body><h1>Strona bez breadcrumbs</h1></body></html>"
        data = self.scraper.parse(html, "https://example.com/dowolna-podstrona")

        self.assertNotIn("BreadcrumbList", data["schema"]["types_found"])


class FaqDynamicDetectionTests(SimpleTestCase):
    """Uniwersalna reguła 2: dynamiczna detekcja sekcji FAQ i porównanie z FAQPage."""

    def setUp(self):
        self.scraper = SEOScraper()

    def test_faq_detected_by_heading_keyword(self):
        html = "<html><body><h2>Często zadawane pytania</h2><p>Treść</p></body></html>"
        data = self.scraper.parse(html, "https://example.com/pomoc")

        self.assertTrue(data["faq_detected"])
        self.assertNotIn("FAQPage", data["schema"]["types_found"])

    def test_faq_detected_by_english_faq_heading(self):
        html = "<html><body><h2>FAQ</h2></body></html>"
        data = self.scraper.parse(html, "https://example.com/help")

        self.assertTrue(data["faq_detected"])

    def test_faq_detected_by_details_tags(self):
        html = (
            "<html><body>"
            "<details><summary>Pytanie 1?</summary><p>Odpowiedź 1</p></details>"
            "<details><summary>Pytanie 2?</summary><p>Odpowiedź 2</p></details>"
            "</body></html>"
        )
        data = self.scraper.parse(html, "https://example.com/pomoc")

        self.assertTrue(data["faq_detected"])

    def test_single_details_tag_is_not_enough(self):
        html = "<html><body><details><summary>Coś</summary><p>Treść</p></details></body></html>"
        data = self.scraper.parse(html, "https://example.com/pomoc")

        self.assertFalse(data["faq_detected"])

    def test_no_faq_signals_present(self):
        html = "<html><body><h1>Zwykła strona</h1><p>Bez pytań i odpowiedzi.</p></body></html>"
        data = self.scraper.parse(html, "https://example.com/o-nas")

        self.assertFalse(data["faq_detected"])

    def test_faq_detected_with_matching_faqpage_schema(self):
        html = (
            "<html><head><script type=\"application/ld+json\">"
            '{"@type": "FAQPage"}'
            "</script></head><body><h2>FAQ</h2></body></html>"
        )
        data = self.scraper.parse(html, "https://example.com/faq")

        self.assertTrue(data["faq_detected"])
        self.assertIn("FAQPage", data["schema"]["types_found"])


class PageTypeDetectionTests(SimpleTestCase):
    """Klasyfikacja typu podstrony: homepage/product/article/category/generic."""

    def setUp(self):
        self.scraper = SEOScraper()

    def test_homepage_detected_by_root_path_with_organization_schema(self):
        html = (
            "<html><head><script type=\"application/ld+json\">"
            '{"@context": "https://schema.org", "@graph": ['
            '{"@type": "Organization", "name": "Firma"},'
            '{"@type": "WebSite", "name": "Firma"}]}'
            "</script></head><body><h1>Witamy</h1></body></html>"
        )
        data = self.scraper.parse(html, "https://firma.pl/")

        self.assertEqual(data["page_type"], "homepage")
        self.assertIn("Organization", data["schema"]["types_found"])

    def test_homepage_detected_when_path_is_empty(self):
        html = "<html><body><h1>Strona główna</h1></body></html>"
        data = self.scraper.parse(html, "https://firma.pl")

        self.assertEqual(data["page_type"], "homepage")

    def test_product_page_detected_by_url_path_and_microdata(self):
        html = (
            '<html><body><div itemscope itemtype="https://schema.org/Product">'
            "<h1>Super produkt</h1><span class=\"price\">99 zł</span>"
            "</div></body></html>"
        )
        data = self.scraper.parse(html, "https://sklep.pl/produkt/super-produkt")

        self.assertEqual(data["page_type"], "product")
        self.assertIn("Product", data["schema"]["types_found"])

    def test_product_page_detected_by_cart_button_without_url_hint(self):
        html = '<html><body><h1>Coś</h1><button class="add-to-cart">Dodaj do koszyka</button></body></html>'
        data = self.scraper.parse(html, "https://sklep.pl/tajemniczy-produkt")

        self.assertEqual(data["page_type"], "product")

    def test_article_page_detected_by_url_and_article_tag(self):
        html = (
            "<html><head><script type=\"application/ld+json\">"
            '{"@type": "Article", "headline": "Jak pisać SEO"}'
            "</script></head><body><article><h1>Jak pisać SEO</h1></article></body></html>"
        )
        data = self.scraper.parse(html, "https://blog.pl/artykul/jak-pisac-seo")

        self.assertEqual(data["page_type"], "article")
        self.assertIn("Article", data["schema"]["types_found"])

    def test_article_page_detected_by_author_meta_without_url_hint(self):
        html = (
            '<html><head><meta name="author" content="Jan Kowalski"></head>'
            "<body><h1>Tekst</h1></body></html>"
        )
        data = self.scraper.parse(html, "https://serwis.pl/losowa-sciezka")

        self.assertEqual(data["page_type"], "article")

    def test_category_page_detected_by_url_and_product_list(self):
        html = (
            "<html><body><h1>Buty</h1>"
            '<div class="product-item">But 1</div>'
            '<div class="product-item">But 2</div>'
            "</body></html>"
        )
        data = self.scraper.parse(html, "https://sklep.pl/kategoria/buty")

        self.assertEqual(data["page_type"], "category")

    def test_generic_page_when_no_signals_match(self):
        html = "<html><body><h1>Zwykła podstrona</h1></body></html>"
        data = self.scraper.parse(html, "https://serwis.pl/informacje")

        self.assertEqual(data["page_type"], "generic")
