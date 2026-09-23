"""Testy intencji podstrony i warunkowej walidacji Person/Organization.

Problem, który to rozwiązuje: audyt bezwarunkowo żądał encji Person, więc na
stronach ofertowych B2B zgłaszał brak autora tekstu - podczas gdy podmiotem
odpowiedzialnym za taką treść jest firma, nie osoba.

Regresja z produkcji (orlen.pl/pl/dla-biznesu/karty-i-uslugi-flotowe): strona
ofertowa trafiała do typu "article", bo CMS użył ośmiu znaczników <article> jako
kontenerów kafelków, a jeden taki znacznik wystarczał do uznania strony za artykuł.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup
from django.test import SimpleTestCase, TestCase

from auditor.services.scraper import (
    PAGE_INTENT_COMMERCIAL,
    PAGE_INTENT_EDITORIAL,
    SEOScraper,
)


def _intent(url: str, html: str = "<html><body></body></html>", schema: dict | None = None) -> str:
    scraper = SEOScraper()
    soup = BeautifulSoup(html, "html.parser")
    page_type = scraper._detect_page_type(url, soup)
    return scraper._detect_page_intent(url, soup, schema or {}, page_type)


class PageIntentFromSchemaTests(SimpleTestCase):
    """Deklaracja typu w Schema.org jest sygnałem najmocniejszym."""

    def test_article_family_types_are_editorial(self):
        for schema_type in ("Article", "BlogPosting", "NewsArticle", "TechArticle"):
            with self.subTest(typ=schema_type):
                intent = _intent("https://firma.pl/cokolwiek", schema={"types": [schema_type]})
                self.assertEqual(intent, PAGE_INTENT_EDITORIAL)

    def test_commercial_types_are_commercial(self):
        for schema_type in ("Product", "Service", "Offer", "FinancialProduct"):
            with self.subTest(typ=schema_type):
                intent = _intent("https://firma.pl/cokolwiek", schema={"types": [schema_type]})
                self.assertEqual(intent, PAGE_INTENT_COMMERCIAL)

    def test_schema_wins_over_the_url_path(self):
        """Adres bywa mylący - deklaracja wydawcy rozstrzyga."""
        intent = _intent("https://firma.pl/oferta/raport", schema={"types": ["Article"]})

        self.assertEqual(intent, PAGE_INTENT_EDITORIAL)

    def test_webpage_type_alone_does_not_decide(self):
        """WebPage to typ domyślny, występuje też na artykułach - nie rozstrzyga."""
        intent = _intent("https://firma.pl/blog/wpis", schema={"types": ["WebPage"]})

        self.assertEqual(intent, PAGE_INTENT_EDITORIAL)


class PageIntentFromUrlTests(SimpleTestCase):
    """Ścieżka adresu, gdy Schema nie rozstrzyga."""

    def test_editorial_path_segments(self):
        for path in ("/blog/wpis", "/poradnik/jak-wybrac", "/aktualnosci/nowosc",
                     "/baza-wiedzy/temat", "/artykuly/tekst", "/wiedza/przewodnik"):
            with self.subTest(sciezka=path):
                self.assertEqual(_intent(f"https://firma.pl{path}"), PAGE_INTENT_EDITORIAL)

    def test_commercial_path_segments(self):
        for path in ("/dla-biznesu/karty-i-uslugi-flotowe", "/oferta/pakiety",
                     "/uslugi/serwis", "/produkty/karta", "/cennik", "/dla-firm/flota"):
            with self.subTest(sciezka=path):
                self.assertEqual(_intent(f"https://firma.pl{path}"), PAGE_INTENT_COMMERCIAL)

    def test_orlen_fleet_card_page_is_commercial(self):
        """Regresja: dokładnie ten adres generował fałszywy błąd o braku autora."""
        intent = _intent("https://orlen.pl/pl/dla-biznesu/karty-i-uslugi-flotowe")

        self.assertEqual(intent, PAGE_INTENT_COMMERCIAL)

    def test_homepage_is_commercial(self):
        self.assertEqual(_intent("https://firma.pl/"), PAGE_INTENT_COMMERCIAL)

    def test_unknown_path_defaults_to_commercial(self):
        """W razie wątpliwości nie żądamy autora - fałszywy alarm na stronie
        ofertowej kosztuje więcej niż pominięte ostrzeżenie na nietypowym tekście."""
        self.assertEqual(_intent("https://firma.pl/xyz/123"), PAGE_INTENT_COMMERCIAL)


class ArticleTagHeuristicTests(SimpleTestCase):
    """Znacznik <article> jako sygnał redakcyjny - i jego granice."""

    def test_many_article_tags_are_layout_cards_not_an_article(self):
        """Regresja orlen.pl: osiem kontenerów kafelków to nie jest tekst redakcyjny."""
        html = "<html><body>" + "<article>Kafelek</article>" * 8 + "</body></html>"

        self.assertFalse(SEOScraper()._has_article_signals(BeautifulSoup(html, "html.parser")))

    def test_single_article_tag_still_counts(self):
        html = "<html><body><article><h1>Tekst</h1></article></body></html>"

        self.assertTrue(SEOScraper()._has_article_signals(BeautifulSoup(html, "html.parser")))

    def test_editorial_metadata_counts_regardless_of_article_tags(self):
        """Kafelek layoutu nigdy nie niesie daty publikacji - metadane rozstrzygają."""
        html = (
            '<html><head><meta property="article:published_time" content="2026-01-01"></head>'
            "<body>" + "<article>Kafelek</article>" * 6 + "</body></html>"
        )

        self.assertTrue(SEOScraper()._has_article_signals(BeautifulSoup(html, "html.parser")))

    def test_page_without_any_editorial_signal(self):
        html = "<html><body><div><h1>Oferta</h1></div></body></html>"

        self.assertFalse(SEOScraper()._has_article_signals(BeautifulSoup(html, "html.parser")))


class PersonEntityCheckTests(SimpleTestCase):
    """Warunkowa walidacja: Person na treści redakcyjnej, Organization na ofertowej."""

    def _service(self):
        from auditor.services.audit_service import AuditService

        service = AuditService.__new__(AuditService)
        service.rag_engine = MagicMock()
        service.rag_engine.generate_recommendation.return_value = ""
        return service

    def _data(self, entities: list[dict], intent: str) -> dict:
        return {
            "page_intent": intent,
            "page_type": "article" if intent == PAGE_INTENT_EDITORIAL else "generic",
            "schema": {"entities": entities, "types": []},
        }

    def _metric(self, entities, intent):
        with patch.object(
            type(self._service()), "_schema_entities", return_value=entities, create=True
        ):
            service = self._service()
            service._schema_entities = lambda data: entities
            return service._evaluate_authorship_depth(self._data(entities, intent))

    # ---------- strony redakcyjne ----------
    def test_editorial_page_without_person_is_flagged(self):
        metric = self._metric([{"@type": "BlogPosting"}], PAGE_INTENT_EDITORIAL)

        self.assertEqual(metric["status"], "warning")
        self.assertIn("Person", metric["value"]["note"])

    def test_editorial_page_with_complete_person_passes(self):
        person = {
            "@type": "Person", "name": "Ekspert SEO 1",
            "jobTitle": "Specjalista", "sameAs": ["https://www.linkedin.com/in/x"],
        }
        metric = self._metric([person], PAGE_INTENT_EDITORIAL)

        self.assertEqual(metric["status"], "ok")

    # ---------- strony komercyjne ----------
    def test_commercial_page_is_not_flagged_for_a_missing_author(self):
        """Sedno zmiany: brak encji Person na stronie ofertowej to nie jest błąd."""
        metric = self._metric(
            [{"@type": "Service", "provider": {"@id": "https://firma.pl/#org"}}],
            PAGE_INTENT_COMMERCIAL,
        )

        self.assertNotEqual(metric["status"], "warning")
        self.assertNotEqual(metric["status"], "error")
        self.assertIn("Person nie jest wymagana", metric["value"]["note"])

    def test_commercial_page_verifies_the_organization_instead(self):
        organization = {
            "@type": "Organization", "name": "Firma A", "url": "https://firma.pl/",
            "logo": "https://firma.pl/logo.svg", "address": {"@type": "PostalAddress"},
        }
        metric = self._metric([organization], PAGE_INTENT_COMMERCIAL)

        self.assertEqual(metric["status"], "ok")
        self.assertIn("Firma A", metric["value"]["note"])
        self.assertIn("address", metric["value"]["trust_signals"])

    def test_incomplete_organization_is_reported(self):
        metric = self._metric(
            [{"@type": "Organization", "name": "Firma A"}], PAGE_INTENT_COMMERCIAL
        )

        self.assertEqual(metric["status"], "warning")
        self.assertIn("url", metric["value"]["missing_properties"])
        self.assertIn("logo", metric["value"]["missing_properties"])

    def test_organization_without_trust_signals_is_reported(self):
        organization = {
            "@type": "Organization", "name": "Firma A",
            "url": "https://firma.pl/", "logo": "https://firma.pl/logo.svg",
        }
        metric = self._metric([organization], PAGE_INTENT_COMMERCIAL)

        self.assertEqual(metric["status"], "warning")
        self.assertIn("sygnału wiarygodności", metric["value"]["note"])

    def test_graph_reference_is_accepted_instead_of_a_full_entity(self):
        """Poprawny graf deklaruje Organization raz - podstrony ją referencjonują."""
        metric = self._metric(
            [{"@type": "Product", "name": "Karta", "publisher": {"@id": "https://firma.pl/#org"}}],
            PAGE_INTENT_COMMERCIAL,
        )

        self.assertEqual(metric["status"], "info")
        self.assertIn("publisher", metric["value"]["organization_references"])

    def test_commercial_page_with_no_entity_at_all_is_reported(self):
        metric = self._metric([{"@type": "WebPage"}], PAGE_INTENT_COMMERCIAL)

        self.assertEqual(metric["status"], "warning")
        self.assertIn("Organization", metric["value"]["note"])

    def test_person_on_a_commercial_page_is_still_evaluated(self):
        """Gdy strona ofertowa JEDNAK deklaruje autora, oceniamy go normalnie -
        nie ma powodu ignorować danych, które ktoś świadomie dodał."""
        person = {"@type": "Person", "name": "Ekspert SEO 1"}
        metric = self._metric([person], PAGE_INTENT_COMMERCIAL)

        self.assertIn(metric["status"], ("warning", "ok"))
        self.assertEqual(metric["value"]["persons"], 1)


class EeatGatingTests(SimpleTestCase):
    """Pozostałe testy E-E-A-T też muszą respektować intencję podstrony."""

    def _service(self):
        from auditor.services.audit_service import AuditService

        service = AuditService.__new__(AuditService)
        service.rag_engine = MagicMock()
        service.rag_engine.generate_recommendation.return_value = ""
        return service

    def test_commercial_page_without_author_signal_is_informational(self):
        data = {"eeat": {"has_author_signal": False}, "page_type": "generic",
                "page_intent": PAGE_INTENT_COMMERCIAL}

        metric = self._service()._evaluate_eeat_authorship(data)

        self.assertEqual(metric["status"], "info")
        self.assertIn("ofertowym", metric["value"]["note"])

    def test_editorial_page_without_author_signal_is_a_warning(self):
        data = {"eeat": {"has_author_signal": False}, "page_type": "article",
                "page_intent": PAGE_INTENT_EDITORIAL}

        metric = self._service()._evaluate_eeat_authorship(data)

        self.assertEqual(metric["status"], "warning")

    def test_commercial_page_without_freshness_marker_is_informational(self):
        data = {"eeat": {"modified_time": None}, "page_type": "generic",
                "page_intent": PAGE_INTENT_COMMERCIAL}

        metric = self._service()._evaluate_eeat_freshness(data)

        self.assertEqual(metric["status"], "info")


class RagPromptIntentTests(TestCase):
    """Generator rekomendacji nie może proponować autora dla strony ofertowej."""

    def _prompt_for(self, **kwargs) -> str:
        from auditor.services.rag import RAGEngine

        engine = RAGEngine()
        engine.api_key = "sk-test"
        engine._embeddings = MagicMock()
        engine._collection = MagicMock()
        engine._collection.query.return_value = {"ids": [[]]}

        answer = MagicMock()
        answer.content = "### 1. DIAGNOZA"
        client = MagicMock()
        client.invoke.return_value = answer

        with patch.object(RAGEngine, "_build_llm", return_value=client):
            engine.generate_recommendation("Problem.", **kwargs)
        return client.invoke.call_args.args[0][0].content

    def test_commercial_intent_forbids_suggesting_a_named_author(self):
        prompt = self._prompt_for(page_intent=PAGE_INTENT_COMMERCIAL)

        self.assertIn("OFERTOWY", prompt)
        self.assertIn("NIE sugeruj dodawania imiennego autora", prompt)
        self.assertIn("Organization", prompt)

    def test_editorial_intent_allows_author_recommendations(self):
        prompt = self._prompt_for(page_intent=PAGE_INTENT_EDITORIAL)

        self.assertIn("REDAKCYJNY", prompt)
        self.assertIn("Person", prompt)

    def test_without_intent_the_prompt_stays_unchanged(self):
        prompt = self._prompt_for()

        self.assertNotIn("OFERTOWY", prompt)
        self.assertNotIn("REDAKCYJNY", prompt)

    def test_audit_service_passes_the_intent_to_the_generator(self):
        from auditor.services.audit_service import AuditService

        service = AuditService.__new__(AuditService)
        service.rag_engine = MagicMock()
        service.rag_engine.generate_recommendation.return_value = "rekomendacja"
        service.site_domain = "orlen.pl"
        service.page_intent = PAGE_INTENT_COMMERCIAL

        service._make_metric("structure", "authorship_depth", {"note": "Brak."}, "warning")

        self.assertEqual(
            service.rag_engine.generate_recommendation.call_args.kwargs["page_intent"],
            PAGE_INTENT_COMMERCIAL,
        )
