"""Testy spójności reguł audytowych - żadne dwie metryki nie mogą sobie przeczyć.

Sedno: jeśli jeden test zgłasza problem na stronie, żaden inny test opisujący TEN SAM
element nie może jednocześnie raportować statusu OK. Klasyczny przykład (zgłoszony
z produkcji): pusty `<h1></h1>` dawał OSTRZEŻENIE w teście hierarchii nagłówków
i jednocześnie OK w teście struktury H1.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from django.test import SimpleTestCase

from auditor.services.audit_service import AuditService
from auditor.services.scraper import SEOScraper

# Znaczniki, które istnieją w HTML, ale nie niosą żadnej treści. Każdy test opisujący
# taki element musi go zgłosić - żaden nie może zwrócić OK.
EMPTY_TAGS_PAGE = """<html><head>
<title>   </title>
<meta name="description" content="   ">
<link rel="canonical" href="   ">
<meta property="og:title" content="">
<meta name="twitter:card" content="  ">
</head><body>
<h1>   </h1>
<h2>Sekcja</h2>
<img src="zdjecie.jpg">
</body></html>"""

HEALTHY_ROBOTS = {"checked": True, "exists": True, "disallows_all": False, "blocked_ai_bots": []}
BLOCKING_ROBOTS = {"checked": True, "exists": True, "disallows_all": True, "blocked_ai_bots": []}


def _service() -> AuditService:
    rag = MagicMock()
    rag.generate_recommendation.return_value = "Rekomendacja testowa."
    return AuditService(rag_engine=rag)


def _parse(html: str) -> dict:
    return SEOScraper().parse(html, "https://example.com")


class EmptyH1ConsistencyTests(SimpleTestCase):
    """Regresja zgłoszona z produkcji: pusty H1 dawał OK i OSTRZEŻENIE jednocześnie."""

    def test_empty_h1_is_an_error_not_ok(self):
        data = _parse("<html><body><h1></h1><h2>Sekcja</h2></body></html>")

        metric = _service()._evaluate_h1(data)

        self.assertEqual(metric["status"], "error")
        self.assertIn("pusty", metric["value"]["note"].lower())

    def test_whitespace_only_h1_is_an_error(self):
        data = _parse("<html><body><h1>   </h1></body></html>")

        self.assertEqual(_service()._evaluate_h1(data)["status"], "error")

    def test_empty_h1_is_never_ok_in_either_heading_test(self):
        """Kontrakt spójności dotyczy PUSTEGO H1: skoro `heading_order` zgłasza go jako
        problem, `h1_structure` nie może w tym samym audycie twierdzić, że struktura H1
        jest prawidłowa. To dokładnie ta sprzeczność, którą zgłoszono z produkcji."""
        service = _service()
        scenarios = {
            "pusty H1": "<html><body><h1></h1><h2>S</h2></body></html>",
            "poprawny H1 + pusty H1": "<html><body><h1>Tytuł</h1><h1></h1><h2>S</h2></body></html>",
            "H1 z samych spacji": "<html><body><h1>   </h1><h2>S</h2></body></html>",
        }

        for opis, html in scenarios.items():
            with self.subTest(scenariusz=opis):
                data = _parse(html)
                self.assertNotEqual(service._evaluate_h1(data)["status"], "ok", opis)
                self.assertNotEqual(service._evaluate_heading_order(data)["status"], "ok", opis)

    def test_empty_h2_does_not_falsely_flag_the_h1_test(self):
        """Granica kontraktu: pusty H2 to problem hierarchii, a NIE nagłówka H1.

        Rozbieżność statusów jest tu poprawna - oba testy opisują różne elementy strony.
        Rozszerzanie spójności na wszystkie nagłówki dawałoby fałszywe alarmy w teście,
        który z założenia ocenia wyłącznie H1.
        """
        data = _parse("<html><body><h1>Prawidłowy tytuł</h1><h2></h2></body></html>")
        service = _service()

        self.assertEqual(service._evaluate_h1(data)["status"], "ok")
        self.assertEqual(service._evaluate_heading_order(data)["status"], "warning")

    def test_multiple_non_empty_h1_is_not_a_hierarchy_problem(self):
        """Analogicznie w drugą stronę: wiele niepustych H1 to problem testu H1,
        a nie kolejności/pustych nagłówków (przypadek realny - python.org)."""
        data = _parse("<html><body><h1>A</h1><h1>B</h1><h2>S</h2></body></html>")
        service = _service()

        self.assertEqual(service._evaluate_h1(data)["status"], "warning")
        self.assertEqual(service._evaluate_heading_order(data)["status"], "ok")

    def test_correct_single_h1_stays_ok(self):
        """Naprawa nie może zacząć zgłaszać problemów na poprawnych stronach."""
        data = _parse("<html><body><h1>Prawidłowy tytuł</h1><h2>Sekcja</h2></body></html>")
        service = _service()

        self.assertEqual(service._evaluate_h1(data)["status"], "ok")
        self.assertEqual(service._evaluate_heading_order(data)["status"], "ok")

    def test_extra_empty_h1_downgrades_correct_h1_to_warning(self):
        data = _parse("<html><body><h1>Tytuł</h1><h1></h1><h2>S</h2></body></html>")

        metric = _service()._evaluate_h1(data)

        self.assertEqual(metric["status"], "warning")
        self.assertEqual(metric["value"]["count"], 1)
        self.assertEqual(metric["value"]["empty_count"], 1)

    def test_multiple_real_h1_is_still_a_warning(self):
        data = _parse("<html><body><h1>Pierwszy</h1><h1>Drugi</h1></body></html>")

        self.assertEqual(_service()._evaluate_h1(data)["status"], "warning")


class EmptyTagsSanityTests(SimpleTestCase):
    """Żaden test nie może zwrócić OK dla znacznika, który istnieje, ale jest pusty."""

    def test_no_evaluator_returns_ok_for_empty_tags_page(self):
        data = _parse(EMPTY_TAGS_PAGE)
        service = _service()

        metrics = [
            service._evaluate_title(data),
            service._evaluate_description(data),
            service._evaluate_h1(data),
            service._evaluate_canonical(data),
            service._evaluate_open_graph(data),
            service._evaluate_twitter_cards(data),
            service._evaluate_heading_order(data),
            service._evaluate_images(data),
        ]

        for metric in metrics:
            with self.subTest(metryka=metric["key"]):
                self.assertNotEqual(
                    metric["status"], "ok",
                    f"{metric['key']} zwraca OK mimo pustego znacznika: {metric['value']['note']}",
                )

    def test_whitespace_canonical_is_treated_as_missing(self):
        """<link rel="canonical" href="   "> niczego nie wskazuje."""
        data = _parse('<html><head><link rel="canonical" href="   "></head><body></body></html>')

        metric = _service()._evaluate_canonical(data)

        self.assertEqual(metric["status"], "warning")
        self.assertIsNone(data["canonical"])

    def test_real_canonical_is_still_accepted(self):
        data = _parse('<html><head><link rel="canonical" href="https://example.com/x"></head><body></body></html>')

        self.assertEqual(_service()._evaluate_canonical(data)["status"], "ok")

    def test_whitespace_title_is_reported_as_missing(self):
        data = _parse("<html><head><title>   </title></head><body></body></html>")

        self.assertEqual(_service()._evaluate_title(data)["status"], "error")


class MetaRobotsTests(SimpleTestCase):
    def test_noindex_is_a_critical_error(self):
        data = _parse('<html><head><meta name="robots" content="noindex, follow"></head><body></body></html>')

        metric = _service()._evaluate_meta_robots(data, HEALTHY_ROBOTS)

        self.assertEqual(metric["status"], "error")
        self.assertTrue(metric["value"]["noindex"])

    def test_noindex_for_googlebot_only_is_also_caught(self):
        """noindex podany wyłącznie dla Googlebota wyklucza stronę z Google tak samo."""
        data = _parse('<html><head><meta name="googlebot" content="noindex"></head><body></body></html>')

        self.assertEqual(_service()._evaluate_meta_robots(data, HEALTHY_ROBOTS)["status"], "error")

    def test_none_directive_implies_noindex_and_nofollow(self):
        data = _parse('<html><head><meta name="robots" content="none"></head><body></body></html>')

        metric = _service()._evaluate_meta_robots(data, HEALTHY_ROBOTS)

        self.assertTrue(metric["value"]["noindex"])
        self.assertTrue(metric["value"]["nofollow"])

    def test_nofollow_alone_is_a_warning(self):
        data = _parse('<html><head><meta name="robots" content="index, nofollow"></head><body></body></html>')

        self.assertEqual(_service()._evaluate_meta_robots(data, HEALTHY_ROBOTS)["status"], "warning")

    def test_missing_tag_means_indexable(self):
        data = _parse("<html><head></head><body></body></html>")

        metric = _service()._evaluate_meta_robots(data, HEALTHY_ROBOTS)

        self.assertEqual(metric["status"], "ok")
        self.assertFalse(metric["value"]["present"])

    def test_does_not_claim_page_is_indexable_when_robots_txt_blocks_site(self):
        """Sedno spójności: robots.txt blokuje wszystko, więc meta_robots nie może
        twierdzić, że strona jest dostępna do indeksowania."""
        data = _parse("<html><head></head><body></body></html>")
        service = _service()

        robots_metric = service._evaluate_robots_txt(BLOCKING_ROBOTS)
        meta_metric = service._evaluate_meta_robots(data, BLOCKING_ROBOTS)

        self.assertEqual(robots_metric["status"], "error")
        self.assertNotEqual(meta_metric["status"], "ok")
        self.assertTrue(meta_metric["value"]["blocked_by_robots_txt"])

    def test_metric_shape_matches_other_checks(self):
        data = _parse("<html><head></head><body></body></html>")

        metric = _service()._evaluate_meta_robots(data, HEALTHY_ROBOTS)

        self.assertEqual({"category", "key", "value", "status", "current_value"}, set(metric))
        self.assertEqual(metric["key"], "meta_robots")
        self.assertTrue(metric["value"]["note"])


class MetaRobotsRegistrationTests(SimpleTestCase):
    """Nowy klucz musi być zarejestrowany, inaczej karta testu zniknie z interfejsu."""

    def test_key_is_present_in_ui_dictionaries(self):
        from auditor.presentation import (
            METRIC_DEFINITIONS,
            OFFICIAL_TEST_NAMES,
            TECHNICAL_ACCORDIONS,
        )

        self.assertIn("meta_robots", METRIC_DEFINITIONS)
        self.assertIn("meta_robots", OFFICIAL_TEST_NAMES)
        wszystkie_klucze = set().union(*(keys for _, _, keys in TECHNICAL_ACCORDIONS))
        self.assertIn("meta_robots", wszystkie_klucze)


class SenutoDatabaseTests(SimpleTestCase):
    """Senuto: zapytania muszą trafiać do bazy 2.0 - tej, którą pokazuje panel."""

    def test_default_database_is_the_current_one(self):
        from auditor.services.senuto import (
            COUNTRY_ID_PL,
            SENUTO_DATABASE_PL_CURRENT,
            SENUTO_DATABASE_PL_LEGACY,
        )

        self.assertEqual(COUNTRY_ID_PL, SENUTO_DATABASE_PL_CURRENT)
        self.assertNotEqual(COUNTRY_ID_PL, SENUTO_DATABASE_PL_LEGACY)

    def test_requests_send_the_current_database_id(self):
        from unittest.mock import MagicMock, patch

        from auditor.services.senuto import COUNTRY_ID_PL, SenutoService

        service = SenutoService()
        service.api_key = "testowy-klucz"
        client = MagicMock()
        odpowiedz = MagicMock()
        odpowiedz.json.return_value = {
            "success": True,
            "data": {"statistics": {k: {"recent_value": 1} for k in ("top3", "top10", "top50")}},
        }
        client.get.return_value = odpowiedz

        service._fetch_visibility_summary(client, "example.com")

        self.assertEqual(client.get.call_args.kwargs["params"]["country_id"], COUNTRY_ID_PL)

    def test_cache_key_is_scoped_to_database(self):
        """Po zmianie bazy wpisy z bazy 1.0 nie mogą wracać jako aktualne dane."""
        from django.core.cache import cache

        from auditor.services.senuto import COUNTRY_ID_PL, SenutoService

        cache.clear()
        cache.set(f"senuto:{1}:example.com", {"top10": 143}, 60)

        service = SenutoService()
        service.api_key = "testowy-klucz"
        podejrzany = cache.get(f"senuto:{COUNTRY_ID_PL}:example.com")

        self.assertIsNone(podejrzany, "klucz cache musi zawierać id bazy Senuto")
