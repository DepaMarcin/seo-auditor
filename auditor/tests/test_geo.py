"""Testy modułu GEO: dostępność dla botów AI oraz gotowość treści do cytowania.

Obejmują blokady w `robots.txt` i nagłówku `X-Robots-Tag`, wzorzec Answer-First
oraz gęstość elementów ustrukturyzowanych. Żaden test nie łączy się z siecią.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from django.test import SimpleTestCase

from auditor.services.audit_service import AuditService
from auditor.services.scraper import (
    AI_BOT_USER_AGENTS,
    ANSWER_FIRST_MAX_WORDS,
    ANSWER_FIRST_MIN_WORDS,
    SEOScraper,
)

HEALTHY_ROBOTS = {"checked": True, "exists": True, "disallows_all": False, "blocked_ai_bots": []}
NO_ROBOTS = {"checked": True, "exists": False, "disallows_all": False, "blocked_ai_bots": []}


def _service() -> AuditService:
    rag = MagicMock()
    rag.generate_recommendation.return_value = "Rekomendacja testowa."
    return AuditService(rag_engine=rag)


def _parse(html: str, headers: dict | None = None) -> dict:
    scraper = SEOScraper()
    scraper._last_response_headers = headers or {}
    return scraper.parse(html, "https://example.com")


def _paragraph(word_count: int) -> str:
    return " ".join(["słowo"] * word_count)


class AiBotCoverageTests(SimpleTestCase):
    def test_google_extended_is_covered(self):
        """Google-Extended steruje zgodą na użycie treści w Gemini i AI Overviews."""
        self.assertIn("Google-Extended", AI_BOT_USER_AGENTS)

    def test_all_required_bots_are_covered(self):
        for bot in ("GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended"):
            with self.subTest(bot=bot):
                self.assertIn(bot, AI_BOT_USER_AGENTS)

    def test_google_extended_blocked_in_robots_txt_is_detected(self):
        scraper = SEOScraper()

        blocked = scraper._robots_blocked_ai_bots("User-agent: Google-Extended\nDisallow: /\n")

        self.assertEqual(blocked, ["Google-Extended"])


class XRobotsTagTests(SimpleTestCase):
    def test_global_noindex_in_header_is_detected(self):
        data = _parse("<html><body></body></html>", {"X-Robots-Tag": "noindex, nofollow"})

        self.assertTrue(data["x_robots_tag"]["noindex"])
        self.assertTrue(data["x_robots_tag"]["nofollow"])

    def test_header_name_is_matched_case_insensitively(self):
        data = _parse("<html><body></body></html>", {"x-robots-tag": "noindex"})

        self.assertTrue(data["x_robots_tag"]["noindex"])

    def test_per_bot_directive_is_parsed(self):
        data = _parse("<html><body></body></html>", {"X-Robots-Tag": "GPTBot: noindex"})

        self.assertEqual(data["x_robots_tag"]["blocked_bots"], ["GPTBot"])
        self.assertFalse(data["x_robots_tag"]["noindex"])

    def test_missing_header_is_not_an_error(self):
        data = _parse("<html><body></body></html>")

        self.assertFalse(data["x_robots_tag"]["present"])
        self.assertFalse(data["x_robots_tag"]["noindex"])

    def test_noindex_in_header_makes_meta_robots_metric_critical(self):
        """Nagłówek jest równoważny znacznikowi meta - musi dawać ten sam skutek."""
        data = _parse("<html><head></head><body></body></html>", {"X-Robots-Tag": "noindex"})

        metric = _service()._evaluate_meta_robots(data, HEALTHY_ROBOTS)

        self.assertEqual(metric["status"], "error")
        self.assertIn("X-Robots-Tag", metric["value"]["note"])

    def test_header_blocking_ai_bot_is_reported_in_ai_metric(self):
        data = _parse("<html><body></body></html>", {"X-Robots-Tag": "ClaudeBot: noindex"})

        metric = _service()._evaluate_robots_ai_bots(HEALTHY_ROBOTS, data)

        self.assertEqual(metric["status"], "warning")
        self.assertIn("ClaudeBot", metric["value"]["blocked_bots"])
        self.assertIn("ClaudeBot", metric["value"]["blocked_in_http_header"])

    def test_global_header_noindex_blocks_every_ai_bot(self):
        data = _parse("<html><body></body></html>", {"X-Robots-Tag": "noindex"})

        metric = _service()._evaluate_robots_ai_bots(HEALTHY_ROBOTS, data)

        self.assertEqual(set(metric["value"]["blocked_bots"]), set(AI_BOT_USER_AGENTS))

    def test_header_block_is_caught_even_without_robots_txt(self):
        """Brak robots.txt nie może przykryć blokady ustawionej w nagłówku HTTP."""
        data = _parse("<html><body></body></html>", {"X-Robots-Tag": "GPTBot: noindex"})

        metric = _service()._evaluate_robots_ai_bots(NO_ROBOTS, data)

        self.assertEqual(metric["status"], "warning")
        self.assertIn("GPTBot", metric["value"]["blocked_bots"])

    def test_both_sources_are_merged_without_duplicates(self):
        robots = {**HEALTHY_ROBOTS, "blocked_ai_bots": ["GPTBot"]}
        data = _parse("<html><body></body></html>", {"X-Robots-Tag": "GPTBot: noindex, ClaudeBot: noindex"})

        blocked = _service()._evaluate_robots_ai_bots(robots, data)["value"]["blocked_bots"]

        self.assertEqual(blocked, [b for b in AI_BOT_USER_AGENTS if b in {"GPTBot", "ClaudeBot"}])


class AnswerFirstTests(SimpleTestCase):
    def _page(self, *paragraph_lengths: int) -> str:
        sekcje = "".join(
            f"<h2>Sekcja {i}</h2><p>{_paragraph(n)}</p>"
            for i, n in enumerate(paragraph_lengths, start=1)
        )
        return f"<html><body><h1>Tytuł</h1>{sekcje}</body></html>"

    def test_paragraph_in_range_counts_as_answer_first(self):
        data = _parse(self._page(ANSWER_FIRST_MIN_WORDS + 5))

        self.assertEqual(data["answer_first"]["sections_compliant"], 1)

    def test_paragraph_below_range_does_not_count(self):
        data = _parse(self._page(ANSWER_FIRST_MIN_WORDS - 5))

        self.assertEqual(data["answer_first"]["sections_compliant"], 0)

    def test_paragraph_above_range_does_not_count(self):
        data = _parse(self._page(ANSWER_FIRST_MAX_WORDS + 20))

        self.assertEqual(data["answer_first"]["sections_compliant"], 0)

    def test_majority_compliant_sections_is_ok(self):
        data = _parse(self._page(25, 30, 28, 120))

        self.assertEqual(_service()._evaluate_answer_first(data)["status"], "ok")

    def test_no_compliant_sections_is_a_warning(self):
        data = _parse(self._page(200, 150, 180))

        self.assertEqual(_service()._evaluate_answer_first(data)["status"], "warning")

    def test_page_without_sections_is_info_not_warning(self):
        """Landing page bez H2/H3 nie podlega temu wzorcowi - to nie usterka."""
        data = _parse("<html><body><h1>Tytuł</h1><p>Krótki tekst.</p></body></html>")

        metric = _service()._evaluate_answer_first(data)

        self.assertEqual(metric["status"], "info")
        self.assertEqual(metric["value"]["sections_total"], 0)

    def test_section_without_paragraph_is_counted_as_non_compliant(self):
        data = _parse("<html><body><h2>Sekcja</h2><ul><li>punkt</li></ul></body></html>")

        sections = data["answer_first"]["sections"]
        self.assertEqual(len(sections), 1)
        self.assertFalse(sections[0]["has_paragraph"])


class StructuredContentTests(SimpleTestCase):
    def test_tables_and_lists_are_counted(self):
        html = """<html><body>
            <table><tr><td>dane</td></tr></table>
            <ul><li>a</li><li>b</li></ul>
            <ol><li>c</li></ol>
            <p>Akapit</p>
        </body></html>"""

        structured = _parse(html)["structured_content"]

        self.assertEqual(structured["tables"], 1)
        self.assertEqual(structured["lists"], 2)
        self.assertEqual(structured["list_items"], 3)

    def test_navigation_lists_are_excluded(self):
        """Menu w <nav> nie jest treścią merytoryczną."""
        html = """<html><body>
            <nav><ul><li>Start</li><li>Kontakt</li></ul></nav>
            <p>Treść</p>
        </body></html>"""

        self.assertEqual(_parse(html)["structured_content"]["lists"], 0)

    def test_nested_lists_are_counted_once(self):
        html = "<html><body><ul><li>a<ul><li>b</li></ul></li></ul></body></html>"

        self.assertEqual(_parse(html)["structured_content"]["lists"], 1)

    def test_empty_elements_are_ignored(self):
        html = "<html><body><table></table><ul></ul><p>Treść</p></body></html>"

        structured = _parse(html)["structured_content"]

        self.assertEqual(structured["tables"], 0)
        self.assertEqual(structured["lists"], 0)

    def test_high_share_of_structure_is_ok(self):
        html = "<html><body>" + "<ul><li>x</li></ul>" * 3 + "<p>Akapit</p>" * 2 + "</body></html>"

        self.assertEqual(_service()._evaluate_structured_content(_parse(html))["status"], "ok")

    def test_prose_only_page_is_a_warning(self):
        html = "<html><body>" + "<p>Akapit treści.</p>" * 10 + "</body></html>"

        metric = _service()._evaluate_structured_content(_parse(html))

        self.assertEqual(metric["status"], "warning")
        self.assertEqual(metric["value"]["tables"], 0)

    def test_empty_page_is_info_not_warning(self):
        metric = _service()._evaluate_structured_content(_parse("<html><body></body></html>"))

        self.assertEqual(metric["status"], "info")


class GeoMetricRegistrationTests(SimpleTestCase):
    """Nowe klucze muszą być zarejestrowane, inaczej karty znikną z interfejsu."""

    def test_new_keys_are_registered_in_ui(self):
        from auditor.presentation import (
            METRIC_DEFINITIONS,
            OFFICIAL_TEST_NAMES,
            TECHNICAL_ACCORDIONS,
        )

        wszystkie = set().union(*(keys for _, _, keys in TECHNICAL_ACCORDIONS))
        for key in ("answer_first", "structured_content"):
            with self.subTest(metryka=key):
                self.assertIn(key, METRIC_DEFINITIONS)
                self.assertIn(key, OFFICIAL_TEST_NAMES)
                self.assertIn(key, wszystkie)

    def test_metric_shape_matches_other_checks(self):
        data = _parse("<html><body><h2>S</h2><p>" + _paragraph(25) + "</p></body></html>")
        service = _service()

        for metric in (service._evaluate_answer_first(data), service._evaluate_structured_content(data)):
            with self.subTest(metryka=metric["key"]):
                self.assertEqual({"category", "key", "value", "status", "current_value"}, set(metric))
                self.assertTrue(metric["value"]["note"])


class ScraperThreadStateTests(SimpleTestCase):
    """Stan scrapera musi być izolowany per wątek - szablony skanujemy równolegle."""

    def test_response_state_does_not_leak_between_threads(self):
        from concurrent.futures import ThreadPoolExecutor

        scraper = SEOScraper()
        scraper._last_response_headers = {"X-Robots-Tag": "noindex"}

        def odczyt_z_innego_watku() -> dict:
            return scraper._last_response_headers

        with ThreadPoolExecutor(max_workers=1) as executor:
            z_watku = executor.submit(odczyt_z_innego_watku).result()

        self.assertEqual(scraper._last_response_headers, {"X-Robots-Tag": "noindex"})
        self.assertEqual(z_watku, {}, "stan jednego wątku nie może wyciekać do drugiego")
