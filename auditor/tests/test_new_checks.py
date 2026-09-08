"""Testy nowych modułów audytowych: AI/GEO, Social Graph i jakość treści.

Sprawdzają zarówno ekstrakcję danych ze strony (SEOScraper), jak i zamianę ich na
metryki o spójnej strukturze (AuditService). Żaden test nie łączy się z siecią.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from django.test import SimpleTestCase

from auditor.services.audit_service import (
    AI_RELEVANT_SCHEMA_TYPES,
    THIN_CONTENT_MIN_WORDS,
    AuditService,
)
from auditor.services.scraper import AI_BOT_USER_AGENTS, SEOScraper


def _service() -> AuditService:
    """AuditService z wyłączonym RAG - testy sprawdzają logikę oceny, nie rekomendacje AI."""
    rag = MagicMock()
    rag.generate_recommendation.return_value = "Rekomendacja testowa."
    return AuditService(rag_engine=rag)


class MetricContractTests(SimpleTestCase):
    """Każda nowa metryka musi mieć tę samą strukturę co istniejące."""

    REQUIRED_KEYS = {"category", "key", "value", "status", "current_value"}

    def test_all_new_metrics_share_the_same_shape(self):
        service = _service()
        data = {
            "schema": {"blocks_found": 0, "parse_errors": 0, "types_found": []},
            "twitter_card": {"tags": {}, "card_type": None, "has_card": False},
            "open_graph": {},
            "favicon": {"declared": [], "has_apple_touch_icon": False},
            "word_count": 10,
        }
        metrics = [
            service._evaluate_schema_validity(data),
            service._evaluate_twitter_cards(data),
            service._evaluate_favicon(data),
            service._evaluate_thin_content(data),
            service._evaluate_robots_ai_bots({"checked": True, "exists": True, "blocked_ai_bots": ["GPTBot"]}),
        ]

        for metric in metrics:
            with self.subTest(key=metric["key"]):
                self.assertEqual(self.REQUIRED_KEYS, set(metric))
                self.assertIn(metric["status"], ("ok", "warning", "error", "info"))
                self.assertTrue(metric["value"]["note"])
                self.assertIsInstance(metric["current_value"], str)


class AiBotsRobotsTests(SimpleTestCase):
    def setUp(self):
        self.scraper = SEOScraper()

    def test_detects_explicitly_blocked_ai_bot(self):
        content = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"

        blocked = self.scraper._robots_blocked_ai_bots(content)

        self.assertEqual(blocked, ["GPTBot"])

    def test_wildcard_disallow_blocks_all_ai_bots(self):
        blocked = self.scraper._robots_blocked_ai_bots("User-agent: *\nDisallow: /\n")

        self.assertEqual(set(blocked), set(AI_BOT_USER_AGENTS))

    def test_bot_with_own_permissive_group_is_not_blocked(self):
        """Własna sekcja bota ma pierwszeństwo przed regułą ogólną "User-agent: *"."""
        content = "User-agent: *\nDisallow: /\n\nUser-agent: ClaudeBot\nAllow: /\n"

        blocked = self.scraper._robots_blocked_ai_bots(content)

        self.assertNotIn("ClaudeBot", blocked)

    def test_directory_level_disallow_is_not_a_block(self):
        content = "User-agent: GPTBot\nDisallow: /admin/\nDisallow: /koszyk\n"

        self.assertEqual(self.scraper._robots_blocked_ai_bots(content), [])

    def test_comments_and_blank_lines_are_ignored(self):
        content = "# komentarz\n\nUser-agent: PerplexityBot   # bot AI\nDisallow: /\n"

        self.assertEqual(self.scraper._robots_blocked_ai_bots(content), ["PerplexityBot"])

    def test_metric_warns_when_bots_are_blocked(self):
        metric = _service()._evaluate_robots_ai_bots(
            {"checked": True, "exists": True, "blocked_ai_bots": ["GPTBot", "ClaudeBot"]}
        )

        self.assertEqual(metric["status"], "warning")
        self.assertEqual(metric["key"], "robots_ai_bots")
        self.assertIn("GPTBot", metric["value"]["note"])

    def test_metric_ok_when_no_bots_blocked(self):
        metric = _service()._evaluate_robots_ai_bots(
            {"checked": True, "exists": True, "blocked_ai_bots": []}
        )

        self.assertEqual(metric["status"], "ok")

    def test_missing_robots_file_means_full_access(self):
        metric = _service()._evaluate_robots_ai_bots({"checked": True, "exists": False})

        self.assertEqual(metric["status"], "ok")


class SchemaValidityTests(SimpleTestCase):
    def test_parse_error_is_critical(self):
        metric = _service()._evaluate_schema_validity(
            {"schema": {"blocks_found": 2, "parse_errors": 1, "types_found": ["Organization"]}}
        )

        self.assertEqual(metric["status"], "error")
        self.assertIn("składni", metric["value"]["note"])

    def test_missing_json_ld_is_critical(self):
        metric = _service()._evaluate_schema_validity(
            {"schema": {"blocks_found": 0, "parse_errors": 0, "types_found": []}}
        )

        self.assertEqual(metric["status"], "error")

    def test_all_ai_relevant_types_present_is_ok(self):
        metric = _service()._evaluate_schema_validity(
            {"schema": {"blocks_found": 4, "parse_errors": 0, "types_found": list(AI_RELEVANT_SCHEMA_TYPES)}}
        )

        self.assertEqual(metric["status"], "ok")
        self.assertEqual(metric["value"]["types_missing"], [])

    def test_partial_type_coverage_is_warning(self):
        metric = _service()._evaluate_schema_validity(
            {"schema": {"blocks_found": 1, "parse_errors": 0, "types_found": ["Organization"]}}
        )

        self.assertEqual(metric["status"], "warning")
        self.assertIn("Organization", metric["value"]["types_present"])
        self.assertIn("FAQPage", metric["value"]["types_missing"])


class TwitterCardTests(SimpleTestCase):
    def setUp(self):
        self.scraper = SEOScraper()

    def _extract(self, html: str) -> dict:
        from bs4 import BeautifulSoup

        return self.scraper._extract_twitter_card(BeautifulSoup(html, "html.parser"))

    def test_extracts_tags_from_name_attribute(self):
        result = self._extract('<meta name="twitter:card" content="summary_large_image">')

        self.assertEqual(result["card_type"], "summary_large_image")
        self.assertTrue(result["has_card"])

    def test_extracts_tags_from_property_attribute(self):
        """Część CMS-ów wystawia twitter:* przez `property` zamiast `name`."""
        result = self._extract('<meta property="twitter:card" content="summary">')

        self.assertEqual(result["card_type"], "summary")

    def test_missing_card_type_is_warning(self):
        metric = _service()._evaluate_twitter_cards(
            {"twitter_card": {"tags": {"title": "X"}, "card_type": None}, "open_graph": {"image": "a.png"}}
        )

        self.assertEqual(metric["status"], "warning")
        self.assertIn("twitter:card", metric["value"]["note"])

    def test_no_social_tags_at_all_is_critical(self):
        metric = _service()._evaluate_twitter_cards(
            {"twitter_card": {"tags": {}, "card_type": None}, "open_graph": {}}
        )

        self.assertEqual(metric["status"], "error")

    def test_og_image_counts_as_fallback_image(self):
        metric = _service()._evaluate_twitter_cards({
            "twitter_card": {"tags": {"card": "summary"}, "card_type": "summary"},
            "open_graph": {"image": "https://example.com/og.png"},
        })

        self.assertEqual(metric["status"], "ok")
        self.assertTrue(metric["value"]["has_image"])


class FaviconTests(SimpleTestCase):
    def setUp(self):
        self.scraper = SEOScraper()

    def _extract(self, html: str) -> dict:
        from bs4 import BeautifulSoup

        return self.scraper._extract_favicon(BeautifulSoup(html, "html.parser"), "https://example.com/")

    def test_detects_standard_icon_link(self):
        result = self._extract('<link rel="icon" href="/favicon.png">')

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["declared"][0]["href"], "https://example.com/favicon.png")

    def test_detects_apple_touch_icon(self):
        result = self._extract('<link rel="apple-touch-icon" href="/apple.png">')

        self.assertTrue(result["has_apple_touch_icon"])

    def test_missing_icon_is_warning(self):
        metric = _service()._evaluate_favicon({"favicon": {"declared": [], "has_apple_touch_icon": False}})

        self.assertEqual(metric["status"], "warning")

    def test_declared_icon_is_ok(self):
        metric = _service()._evaluate_favicon({
            "favicon": {
                "declared": [{"rel": "icon", "href": "https://example.com/f.png", "sizes": ""}],
                "has_apple_touch_icon": False,
            }
        })

        self.assertEqual(metric["status"], "ok")


class ThinContentTests(SimpleTestCase):
    def test_sufficient_content_is_ok(self):
        metric = _service()._evaluate_thin_content({"word_count": THIN_CONTENT_MIN_WORDS + 50})

        self.assertEqual(metric["status"], "ok")

    def test_below_threshold_is_warning(self):
        metric = _service()._evaluate_thin_content({"word_count": 250})

        self.assertEqual(metric["status"], "warning")
        self.assertIn("250", metric["value"]["note"])

    def test_very_low_word_count_is_critical(self):
        metric = _service()._evaluate_thin_content({"word_count": 40})

        self.assertEqual(metric["status"], "error")

    def test_empty_page_is_critical(self):
        metric = _service()._evaluate_thin_content({"word_count": 0})

        self.assertEqual(metric["status"], "error")


class HeadingHierarchyTests(SimpleTestCase):
    def setUp(self):
        self.scraper = SEOScraper()

    def _analyze(self, html: str) -> dict:
        from bs4 import BeautifulSoup

        return self.scraper._analyze_heading_quality(BeautifulSoup(html, "html.parser"))

    def test_correct_hierarchy_has_no_problems(self):
        result = self._analyze("<h1>Tytuł</h1><h2>Sekcja</h2><h3>Podsekcja</h3><h2>Druga</h2>")

        self.assertEqual(result["empty_count"], 0)
        self.assertEqual(result["skip_count"], 0)

    def test_detects_empty_heading(self):
        result = self._analyze("<h1>Tytuł</h1><h2></h2><h2>Sekcja</h2>")

        self.assertEqual(result["empty_headings"], ["H2"])

    def test_detects_level_skip(self):
        result = self._analyze("<h1>Tytuł</h1><h3>Za głęboko</h3>")

        self.assertEqual(result["skip_count"], 1)
        self.assertEqual(result["level_skips"][0]["from"], "H1")
        self.assertEqual(result["level_skips"][0]["to"], "H3")

    def test_going_back_up_is_not_a_skip(self):
        """H3 -> H2 to normalny początek nowej sekcji, nie błąd hierarchii."""
        result = self._analyze("<h1>T</h1><h2>A</h2><h3>A1</h3><h2>B</h2>")

        self.assertEqual(result["skip_count"], 0)

    def test_metric_reports_all_hierarchy_problems_in_one_card(self):
        metric = _service()._evaluate_heading_order({
            "heading_noise": {"headings_before_h1": [{"tag": "h2", "text": "Menu"}]},
            "heading_quality": {
                "empty_headings": ["H3"],
                "level_skips": [{"from": "H1", "to": "H3", "text": "Sekcja"}],
            },
        })

        self.assertEqual(metric["status"], "warning")
        note = metric["value"]["note"]
        self.assertIn("przed głównym H1", note)
        self.assertIn("pustych nagłówków", note)
        self.assertIn("przeskoków", note)

    def test_clean_hierarchy_metric_is_ok(self):
        metric = _service()._evaluate_heading_order({
            "heading_noise": {"headings_before_h1": []},
            "heading_quality": {"empty_headings": [], "level_skips": []},
        })

        self.assertEqual(metric["status"], "ok")


class ScraperIntegrationTests(SimpleTestCase):
    """Nowe pola muszą trafić do słownika zwracanego przez parse()."""

    def test_parse_exposes_new_fields(self):
        html = """
        <html><head>
            <title>Testowa strona o odpowiedniej długości tytułu SEO</title>
            <meta name="twitter:card" content="summary_large_image">
            <link rel="icon" href="/favicon.ico">
        </head><body>
            <h1>Nagłówek</h1><h3>Przeskok</h3><h2></h2>
            <p>Trochę treści testowej.</p>
        </body></html>
        """

        data = SEOScraper().parse(html, "https://example.com")

        self.assertIn("twitter_card", data)
        self.assertIn("favicon", data)
        self.assertIn("heading_quality", data)
        self.assertIn("word_count", data)
        self.assertEqual(data["twitter_card"]["card_type"], "summary_large_image")
        self.assertEqual(data["favicon"]["count"], 1)
        self.assertEqual(data["heading_quality"]["skip_count"], 1)
        self.assertEqual(data["heading_quality"]["empty_count"], 1)
