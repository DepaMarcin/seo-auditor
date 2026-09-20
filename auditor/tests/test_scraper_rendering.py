"""Testy obejścia blokad WAF i renderowania po stronie klienta (CSR).

Regresja z produkcji: dla `swederm.pl` audyt zgłaszał brak `<title>` i `meta
description`, mimo że oba tagi na stronie są - statyczny HTML oddawany botowi ich
nie zawierał. Trzy niezależne mechanizmy mają temu zapobiegać: nagłówki przeglądarki,
fallback przeglądarkowy oraz zamienniki tagów w parserze.

Żaden test nie uruchamia prawdziwej przeglądarki ani nie łączy się z internetem.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
from django.test import SimpleTestCase

from auditor.services import renderer
from auditor.services.scraper import (
    BROWSER_CLIENT_HINTS,
    CHROME_MAJOR_VERSION,
    DEFAULT_USER_AGENT,
    FALLBACK_USER_AGENT,
    ScraperError,
    SEOScraper,
)


def _fake_response(status_code: int, text: str = "", url: str = "https://example.com") -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request("GET", url), text=text)


# Statyczna odpowiedź aplikacji renderowanej po stronie klienta: pusty kontener
# i skrypt, który dopiero w przeglądarce zbuduje treść.
CSR_HTML = """
<html><head></head>
<body><div id="root"></div><script src="/static/app.js"></script></body></html>
"""

RENDERED_HTML = """
<html><head>
<title>Kosmetyki profesjonalne - Sklep</title>
<meta name="description" content="Dermokosmetyki i preparaty do pielęgnacji skóry dla gabinetów.">
</head>
<body><h1>Kosmetyki profesjonalne</h1><p>Pełna oferta sklepu.</p></body></html>
"""


class BrowserHeaderTests(SimpleTestCase):
    """Nagłówki pierwszego żądania mają wyglądać jak ruch z Chrome na desktopie."""

    def setUp(self):
        self.scraper = SEOScraper()

    def test_default_user_agent_imitates_desktop_chrome(self):
        self.assertIn("Mozilla/5.0", DEFAULT_USER_AGENT)
        self.assertIn("Windows NT 10.0; Win64; x64", DEFAULT_USER_AGENT)
        self.assertIn(f"Chrome/{CHROME_MAJOR_VERSION}", DEFAULT_USER_AGENT)

    def test_client_hints_are_sent_with_browser_user_agent(self):
        for header in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"):
            with self.subTest(header=header):
                self.assertIn(header, self.scraper.headers)

    def test_client_hints_declare_the_same_version_as_user_agent(self):
        """Rozjechanie się wersji w UA i w Client Hints to czytelny sygnał automatu."""
        self.assertIn(f'"{CHROME_MAJOR_VERSION}"', BROWSER_CLIENT_HINTS["sec-ch-ua"])
        self.assertIn(f"Chrome/{CHROME_MAJOR_VERSION}", DEFAULT_USER_AGENT)

    def test_client_hints_are_omitted_for_bot_user_agent(self):
        """`sec-ch-ua: "Google Chrome"` obok UA bota to sprzeczność łatwa do wykrycia."""
        headers = SEOScraper(user_agent=FALLBACK_USER_AGENT).headers

        self.assertNotIn("sec-ch-ua", headers)
        self.assertNotIn("Sec-Fetch-Mode", headers)

    def test_accept_encoding_lists_only_decodable_formats(self):
        """Deklarowanie `br` bez dekodera kończyło pobranie błędem dekodowania."""
        from httpx._decoders import SUPPORTED_DECODERS

        declared = {
            kodowanie.strip() for kodowanie in self.scraper.headers["Accept-Encoding"].split(",")
        }

        self.assertTrue(declared)
        self.assertLessEqual(declared, set(SUPPORTED_DECODERS))


class FlexibleMetaParsingTests(SimpleTestCase):
    """Zamienniki tytułu i opisu oraz niewrażliwość na wielkość liter."""

    def setUp(self):
        self.scraper = SEOScraper()

    def _parse(self, html: str) -> dict:
        return self.scraper.parse(html, "https://example.com")

    def test_plain_title_and_description_are_read_from_their_own_tags(self):
        data = self._parse(
            '<html><head><title>Tytuł strony</title>'
            '<meta name="description" content="Opis strony."></head><body></body></html>'
        )

        self.assertEqual(data["title"], "Tytuł strony")
        self.assertEqual(data["title_source"], "title")
        self.assertEqual(data["meta_description"], "Opis strony.")
        self.assertEqual(data["meta_description_source"], "description")

    def test_description_is_found_regardless_of_letter_case(self):
        data = self._parse('<html><head><meta name="Description" content="Opis."></head></html>')

        self.assertEqual(data["meta_description"], "Opis.")
        self.assertEqual(data["meta_description_source"], "description")

    def test_open_graph_substitutes_for_missing_title(self):
        data = self._parse(
            '<html><head><meta property="og:title" content="Tytuł z Open Graph"></head></html>'
        )

        self.assertEqual(data["title"], "Tytuł z Open Graph")
        self.assertEqual(data["title_source"], "og:title")

    def test_twitter_card_substitutes_when_open_graph_is_missing(self):
        data = self._parse(
            '<html><head><meta name="twitter:title" content="Tytuł z Twitter Card">'
            '<meta name="twitter:description" content="Opis z Twitter Card"></head></html>'
        )

        self.assertEqual(data["title_source"], "twitter:title")
        self.assertEqual(data["meta_description_source"], "twitter:description")

    def test_open_graph_wins_over_twitter_card(self):
        data = self._parse(
            '<html><head>'
            '<meta property="og:description" content="Opis OG">'
            '<meta name="twitter:description" content="Opis Twitter">'
            "</head></html>"
        )

        self.assertEqual(data["meta_description"], "Opis OG")
        self.assertEqual(data["meta_description_source"], "og:description")

    def test_real_title_wins_over_substitutes(self):
        data = self._parse(
            '<html><head><title>Prawdziwy tytuł</title>'
            '<meta property="og:title" content="Tytuł OG"></head></html>'
        )

        self.assertEqual(data["title"], "Prawdziwy tytuł")
        self.assertEqual(data["title_source"], "title")

    def test_empty_title_falls_back_to_substitute(self):
        """`<title>   </title>` istnieje w DOM, ale nie niesie żadnej treści."""
        data = self._parse(
            '<html><head><title>   </title>'
            '<meta property="og:title" content="Tytuł OG"></head></html>'
        )

        self.assertEqual(data["title"], "Tytuł OG")
        self.assertEqual(data["title_source"], "og:title")

    def test_missing_everywhere_reports_no_source(self):
        data = self._parse("<html><head></head><body></body></html>")

        self.assertIsNone(data["title"])
        self.assertIsNone(data["title_source"])
        self.assertIsNone(data["meta_description_source"])

    def test_microdata_description_is_not_treated_as_page_description(self):
        """`<meta itemprop="description">` opisuje produkt, a nie całą stronę."""
        data = self._parse(
            '<html><head><meta itemprop="description" content="Opis produktu."></head></html>'
        )

        self.assertIsNone(data["meta_description"])


class RenderFallbackTests(SimpleTestCase):
    """Sięganie po bezgłowną przeglądarkę: kiedy tak, kiedy nie i co przy błędzie."""

    def setUp(self):
        sleep_patcher = patch("auditor.services.scraper.time.sleep")
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

        guard_patcher = patch(
            "auditor.services.scraper.validate_public_url", side_effect=lambda url: url
        )
        guard_patcher.start()
        self.addCleanup(guard_patcher.stop)

        client_patcher = patch("auditor.services.scraper.httpx.Client")
        self.mock_client_cls = client_patcher.start()
        self.addCleanup(client_patcher.stop)
        self.mock_client = self.mock_client_cls.return_value.__enter__.return_value

        available_patcher = patch("auditor.services.renderer.is_available", return_value=True)
        available_patcher.start()
        self.addCleanup(available_patcher.stop)

        render_patcher = patch("auditor.services.renderer.render_html")
        self.mock_render = render_patcher.start()
        self.addCleanup(render_patcher.stop)

        self.scraper = SEOScraper()

    def test_csr_page_is_re_read_through_the_browser(self):
        self.mock_client.get.return_value = _fake_response(200, text=CSR_HTML)
        self.mock_render.return_value = RENDERED_HTML

        data = self.scraper.scrape("https://example.com")

        self.mock_render.assert_called_once()
        self.assertEqual(data["title"], "Kosmetyki profesjonalne - Sklep")
        self.assertTrue(data["meta_description"])
        self.assertTrue(data["rendered_with_browser"])

    def test_complete_static_page_does_not_start_the_browser(self):
        """Renderowanie kosztuje sekundy i setki MB - nie uruchamiamy go bez powodu."""
        self.mock_client.get.return_value = _fake_response(200, text=RENDERED_HTML)

        data = self.scraper.scrape("https://example.com")

        self.mock_render.assert_not_called()
        self.assertFalse(data["rendered_with_browser"])

    def test_waf_rejection_is_retried_through_the_browser(self):
        self.mock_client.get.return_value = _fake_response(403, text="Forbidden")
        self.mock_render.return_value = RENDERED_HTML

        data = self.scraper.scrape("https://example.com")

        self.mock_render.assert_called_once()
        self.assertEqual(data["title"], "Kosmetyki profesjonalne - Sklep")

    def test_rate_limit_is_retried_through_the_browser(self):
        self.mock_client.get.return_value = _fake_response(429, text="Too Many Requests")
        self.mock_render.return_value = RENDERED_HTML

        self.assertIn("Kosmetyki", self.scraper.fetch("https://example.com"))

    def test_waf_rejection_is_not_retried_statically(self):
        """403 nie jest błędem przejściowym - powtórka tego samego żądania nic nie da."""
        self.mock_client.get.return_value = _fake_response(403, text="Forbidden")
        self.mock_render.return_value = RENDERED_HTML

        self.scraper.fetch("https://example.com")

        self.assertEqual(self.mock_client.get.call_count, 1)

    def test_not_found_does_not_start_the_browser(self):
        self.mock_client.get.return_value = _fake_response(404, text="Not found")

        with self.assertRaises(ScraperError):
            self.scraper.fetch("https://example.com")

        self.mock_render.assert_not_called()

    def test_failed_rendering_keeps_the_static_result(self):
        """Fallback ma poprawiać wynik, a nie tworzyć nowego powodu niepowodzenia."""
        self.mock_client.get.return_value = _fake_response(200, text=CSR_HTML)
        self.mock_render.side_effect = renderer.RendererError("Chromium padł")

        data = self.scraper.scrape("https://example.com")

        self.assertIsNone(data["title"])
        self.assertFalse(data["rendered_with_browser"])

    def test_failed_rendering_after_waf_rejection_reports_the_http_error(self):
        self.mock_client.get.return_value = _fake_response(403, text="Forbidden")
        self.mock_render.side_effect = renderer.RendererUnavailableError("brak playwrighta")

        with self.assertRaises(ScraperError) as ctx:
            self.scraper.fetch("https://example.com")

        self.assertIn("403", str(ctx.exception))

    def test_browser_receives_the_desktop_chrome_user_agent(self):
        self.mock_client.get.return_value = _fake_response(200, text=CSR_HTML)
        self.mock_render.return_value = RENDERED_HTML

        self.scraper.scrape("https://example.com")

        self.assertEqual(self.mock_render.call_args.kwargs["user_agent"], DEFAULT_USER_AGENT)

    def test_render_state_does_not_leak_between_pages(self):
        """Scraper skanuje szablony jedną instancją - stan renderowania jest per żądanie."""
        self.mock_client.get.return_value = _fake_response(200, text=CSR_HTML)
        self.mock_render.return_value = RENDERED_HTML
        self.scraper.scrape("https://example.com")

        self.mock_client.get.return_value = _fake_response(200, text=RENDERED_HTML)
        data = self.scraper.scrape("https://example.com/kontakt")

        self.assertFalse(data["rendered_with_browser"])


class RendererUnavailableTests(SimpleTestCase):
    """Brak Playwrighta jest normalnym stanem - audyt musi działać bez niego."""

    def setUp(self):
        guard_patcher = patch(
            "auditor.services.scraper.validate_public_url", side_effect=lambda url: url
        )
        guard_patcher.start()
        self.addCleanup(guard_patcher.stop)

        client_patcher = patch("auditor.services.scraper.httpx.Client")
        self.mock_client_cls = client_patcher.start()
        self.addCleanup(client_patcher.stop)
        self.mock_client = self.mock_client_cls.return_value.__enter__.return_value

    def test_scrape_works_without_playwright(self):
        self.mock_client.get.return_value = _fake_response(200, text=CSR_HTML)

        with patch("auditor.services.renderer.find_spec", return_value=None):
            data = SEOScraper().scrape("https://example.com")

        self.assertFalse(data["rendered_with_browser"])

    def test_render_html_reports_unavailability_instead_of_crashing(self):
        with patch("auditor.services.renderer.find_spec", return_value=None):
            with self.assertRaises(renderer.RendererUnavailableError):
                renderer.render_html("https://example.com")

    def test_fallback_can_be_disabled_by_setting(self):
        with self.settings(SCRAPER_RENDER_FALLBACK_ENABLED=False):
            with patch("auditor.services.renderer.find_spec", return_value=object()):
                self.assertFalse(renderer.is_available())


class RendererGuardTests(SimpleTestCase):
    """Ochrona SSRF wewnątrz przeglądarki i ograniczenie pobieranych zasobów."""

    def _route(self, url: str, resource_type: str = "document"):
        route, request = MagicMock(), MagicMock()
        request.url = url
        request.resource_type = resource_type
        renderer._guard_route(route, request)
        return route

    def test_public_request_is_allowed(self):
        route = self._route("https://example.com/strona")

        route.continue_.assert_called_once()
        route.abort.assert_not_called()

    def test_private_address_is_blocked(self):
        """Strona może przekierować w głąb sieci lokalnej - przeglądarka obsługuje
        przekierowania sama, więc jedyny moment na walidację to przechwycenie ruchu."""
        route = self._route("http://127.0.0.1:8000/admin/")

        route.abort.assert_called_once()
        route.continue_.assert_not_called()

    def test_cloud_metadata_address_is_blocked(self):
        route = self._route("http://169.254.169.254/latest/meta-data/")

        route.abort.assert_called_once()

    def test_heavy_resources_are_skipped(self):
        """Audyt czyta DOM, a nie wygląd - obrazki i czcionki to sam czas ładowania."""
        for typ in ("image", "font", "media"):
            with self.subTest(typ=typ):
                route = self._route("https://example.com/plik", resource_type=typ)
                route.abort.assert_called_once()

    def test_stylesheets_are_kept(self):
        """Wykrywanie ukrytej treści opiera się na stylach - bez CSS dałoby fałszywe wyniki."""
        route = self._route("https://example.com/styl.css", resource_type="stylesheet")

        route.continue_.assert_called_once()

    def test_navigation_headers_drop_browser_owned_ones(self):
        """Playwright ustawia UA i Client Hints spójnie z Chromium - nadpisanie ich
        ręcznie tworzy dokładnie tę sprzeczność, przed którą fallback ma chronić."""
        headers = renderer._navigation_headers(
            {
                "User-Agent": "cokolwiek",
                "Accept-Encoding": "gzip",
                "sec-ch-ua": '"Chromium";v="1"',
                "Accept-Language": "pl-PL,pl;q=0.9",
            }
        )

        self.assertEqual(headers, {"Accept-Language": "pl-PL,pl;q=0.9"})


class SubstituteSourceScoringTests(SimpleTestCase):
    """Zamiennik pozwala pokazać treść, ale nie zamienia braku tagu w wynik "OK".

    Bez tego rozróżnienia elastyczny parser zamieniłby jeden fałszywy alarm (brak
    tytułu, który jest) na drugi, groźniejszy: milczenie o braku tagu, którego
    naprawdę nie ma.
    """

    def setUp(self):
        from auditor.services.audit_service import AuditService

        # Oceny metryk są czystymi funkcjami danych ze scrapera - budujemy usługę bez
        # `__init__`, żeby test nie ciągnął za sobą PageSpeed, Senuto ani silnika RAG.
        self.service = AuditService.__new__(AuditService)
        self.service.rag_engine = MagicMock()
        self.service.rag_engine.generate_recommendation.return_value = ""

    def _title(self, **data) -> dict:
        return self.service._evaluate_title(data)

    def _description(self, **data) -> dict:
        return self.service._evaluate_description(data)

    def test_real_title_of_correct_length_passes(self):
        metryka = self._title(
            title="Tytuł strony o całkiem rozsądnej długości dla wyników wyszukiwania",
            title_length=65,
            title_source="title",
        )

        self.assertEqual(metryka["status"], "ok")

    def test_title_from_open_graph_is_a_warning_not_a_pass(self):
        metryka = self._title(
            title="Tytuł strony o całkiem rozsądnej długości dla wyników wyszukiwania",
            title_length=65,
            title_source="og:title",
        )

        self.assertEqual(metryka["status"], "warning")
        self.assertIn("og:title", metryka["value"]["note"])

    def test_missing_title_everywhere_stays_an_error(self):
        metryka = self._title(title=None, title_length=0, title_source=None)

        self.assertEqual(metryka["status"], "error")

    def test_description_from_open_graph_is_a_warning(self):
        metryka = self._description(
            meta_description="Opis strony o długości mieszczącej się w zalecanym zakresie "
                             "dla wyników wyszukiwania Google.",
            meta_description_length=120,
            meta_description_source="og:description",
        )

        self.assertEqual(metryka["status"], "warning")
        self.assertIn("og:description", metryka["value"]["note"])

    def test_source_is_recorded_in_the_metric(self):
        """Źródło trafia do metryki, żeby raport i eksport mogły je pokazać."""
        metryka = self._title(title="Tytuł", title_length=6, title_source="twitter:title")

        self.assertEqual(metryka["value"]["source"], "twitter:title")

    def test_result_without_source_field_is_treated_as_a_real_tag(self):
        """Wyniki sprzed wprowadzenia zamienników nie mogą nagle zgłaszać ostrzeżeń."""
        metryka = self._title(
            title="Tytuł strony o całkiem rozsądnej długości dla wyników wyszukiwania",
            title_length=65,
        )

        self.assertEqual(metryka["status"], "ok")
