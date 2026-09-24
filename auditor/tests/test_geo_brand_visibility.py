"""Marka, dwupoziomowa widoczność i podsumowanie wykonawcze.

Sam przypis to za wąska miara: model regularnie poleca firmę z nazwy, nie podlinkowując
jej. Taka odpowiedź nie daje ruchu, ale znaczy coś zupełnie innego niż nieobecność -
marka jest w odpowiedziach, brakuje tylko odnośnika. Te dwa stany muszą być rozdzielone,
bo prowadzą do innych działań.
"""
from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.models import GeoQuery, GeoRun, GeoStudy
from auditor.presentation import (
    build_geo_executive_summary,
    build_geo_questions,
    build_geo_visibility_totals,
)
from auditor.services.geo import (
    COMMERCIAL_QUESTION_PROMPT,
    SiteContext,
    ask_once,
    detect_brand_mention,
    extract_brand_name,
    generate_questions,
    get_or_fetch_site_context,
)

# Szkoła językowa - branża, w której model chętnie wymienia marki z nazwy, często
# bez podawania odnośnika.
EARLYSTAGE_HTML = """
<html>
<head>
    <title>Early Stage | Szkoła języka angielskiego dla dzieci i młodzieży</title>
    <meta name="description" content="Kursy angielskiego dla dzieci od 3 lat.
          Autorska metoda nauczania, ponad 300 placówek w całej Polsce.">
    <meta property="og:site_name" content="Early Stage">
</head>
<body>
    <h1>Szkoła języka angielskiego dla dzieci</h1>
    <h2>Kursy dla przedszkolaków</h2>
    <h2>Zapisy na rok szkolny</h2>
    <p>Prowadzimy kursy angielskiego dla dzieci i młodzieży w ponad 300 placówkach.</p>
</body>
</html>
"""

KOMERCYJNE_PYTANIA = [
    "Jaka szkoła językowa dla dzieci jest polecana w Warszawie?",
    "Gdzie zapisać sześciolatka na angielski?",
    "Ranking najlepszych szkół języka angielskiego dla dzieci",
    "Które kursy angielskiego dla przedszkolaków są polecane?",
    "Jaka firma prowadzi kursy angielskiego dla młodzieży?",
]


def _fake_getaddrinfo(hostname, *args, **kwargs):
    import ipaddress

    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        address = "93.184.216.34"
    else:
        address = hostname
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]


_dns_patch = None


def setUpModule():
    global _dns_patch
    _dns_patch = patch("auditor.services.url_guard.socket.getaddrinfo", _fake_getaddrinfo)
    _dns_patch.start()


def tearDownModule():
    _dns_patch.stop()


class BrandExtractionTests(SimpleTestCase):
    """Nazwa marki odczytana ze strony - model pisze "Early Stage", nie "earlystage.pl"."""

    def test_site_name_wins(self):
        brand = extract_brand_name(
            "https://earlystage.pl/",
            title="Kursy angielskiego | Coś zupełnie innego",
            site_name="Early Stage",
        )

        self.assertEqual(brand, "Early Stage")

    def test_brand_is_taken_from_the_title_before_the_separator(self):
        brand = extract_brand_name(
            "https://earlystage.pl/",
            title="Early Stage | Szkoła języka angielskiego dla dzieci",
        )

        self.assertEqual(brand, "Early Stage")

    def test_brand_is_taken_from_the_title_after_a_dash(self):
        brand = extract_brand_name(
            "https://earlystage.pl/",
            title="Szkoła języka angielskiego dla dzieci - Early Stage",
        )

        self.assertEqual(brand, "Early Stage")

    def test_domain_is_the_last_resort(self):
        self.assertEqual(extract_brand_name("https://earlystage.pl/"), "Earlystage")

    def test_hyphenated_domain_becomes_words(self):
        self.assertEqual(extract_brand_name("https://early-stage.pl/"), "Early stage")

    def test_no_url_gives_no_brand(self):
        self.assertEqual(extract_brand_name(""), "")


class BrandMentionDetectionTests(SimpleTestCase):
    """Wzmianka w treści odpowiedzi, niezależnie od przypisów."""

    def test_plain_mention_is_found(self):
        self.assertTrue(detect_brand_mention("Polecam szkołę Early Stage.", "Early Stage"))

    def test_case_is_ignored(self):
        self.assertTrue(detect_brand_mention("warto sprawdzić early stage", "Early Stage"))

    def test_name_written_without_a_space_is_found(self):
        # Model bywa skleja nazwę: "EarlyStage".
        self.assertTrue(detect_brand_mention("Sprawdź EarlyStage.", "Early Stage"))

    def test_other_brands_do_not_count(self):
        self.assertFalse(
            detect_brand_mention("Polecam Helen Doron oraz Berlitz.", "Early Stage")
        )

    def test_name_inside_a_longer_word_does_not_count(self):
        self.assertFalse(detect_brand_mention("Earlystageous", "Early Stage"))

    def test_empty_brand_never_matches(self):
        self.assertFalse(detect_brand_mention("dowolna treść", ""))


class SiteContextBrandTests(TestCase):
    """Marka trafia do kontekstu razem z resztą danych strony."""

    def test_scan_reads_the_brand_from_og_site_name(self):
        with patch("auditor.services.scraper.SEOScraper.fetch", return_value=EARLYSTAGE_HTML):
            context = get_or_fetch_site_context("https://earlystage.pl/")

        self.assertEqual(context.brand_name, "Early Stage")
        self.assertIn("angielskiego", context.title)


class CommercialPromptTests(SimpleTestCase):
    """Prompt wymusza pytania zakupowe i zakazuje poradnikowych."""

    def _context(self) -> SiteContext:
        return SiteContext(
            url="https://earlystage.pl/",
            title="Early Stage | Szkoła języka angielskiego dla dzieci",
            meta_description="Kursy angielskiego dla dzieci od 3 lat.",
            headings=["Szkoła języka angielskiego dla dzieci", "Zapisy na rok szkolny"],
            body_snippet="Prowadzimy kursy angielskiego w ponad 300 placówkach.",
            brand_name="Early Stage",
            source="scan",
        )

    def _prompt_for(self, context: SiteContext) -> str:
        client = MagicMock()
        client.responses.create.return_value = SimpleNamespace(
            output_text=json.dumps(KOMERCYJNE_PYTANIA, ensure_ascii=False)
        )
        with patch("auditor.services.geo._client", return_value=client):
            generate_questions("earlystage.pl", context=context)
        return client.responses.create.call_args.kwargs["input"]

    def test_prompt_names_the_brand_so_the_model_can_avoid_it(self):
        prompt = self._prompt_for(self._context())

        self.assertIn("Marka: Early Stage", prompt)
        self.assertIn("ZAKAZ używania nazwy marki (Early Stage)", prompt)

    def test_prompt_demands_recommendation_questions(self):
        prompt = self._prompt_for(self._context())

        self.assertIn("wymuszać na AI rekomendację konkretnych firm", prompt)
        self.assertIn("Ranking najlepszych", prompt)

    def test_prompt_forbids_how_to_questions(self):
        prompt = self._prompt_for(self._context())

        self.assertIn("ZAKAZ pytań poradnikowych i teoretycznych", prompt)
        self.assertIn("Jak nauczyć", prompt)

    def test_generated_questions_are_commercial_not_instructional(self):
        client = MagicMock()
        client.responses.create.return_value = SimpleNamespace(
            output_text=json.dumps(KOMERCYJNE_PYTANIA, ensure_ascii=False)
        )
        with patch("auditor.services.geo._client", return_value=client):
            questions = generate_questions("earlystage.pl", context=self._context())

        self.assertEqual(len(questions), 5)
        zakupowe = ("polecan", "ranking", "gdzie zapisać", "jaka szkoła", "jaka firma", "które kursy")
        for question in questions:
            with self.subTest(question=question):
                self.assertTrue(
                    any(fraza in question.lower() for fraza in zakupowe),
                    f"pytanie bez intencji zakupowej: {question}",
                )
                self.assertNotIn("early stage", question.lower())

    def test_prompt_is_the_commercial_one(self):
        prompt = self._prompt_for(self._context())

        self.assertTrue(prompt.startswith(COMMERCIAL_QUESTION_PROMPT.split("\n")[0]))


class TwoLevelDetectionTests(SimpleTestCase):
    """`ask_once` rozróżnia link, wzmiankę i nieobecność."""

    def _client_returning(self, answer: str, citations: list[dict]) -> MagicMock:
        client = MagicMock()
        annotations = [
            SimpleNamespace(type="url_citation", url=c["url"], title=c.get("title", ""))
            for c in citations
        ]
        content = SimpleNamespace(annotations=annotations)
        message = SimpleNamespace(content=[content])
        client.responses.create.return_value = SimpleNamespace(
            output_text=answer, output=[message]
        )
        return client

    def test_linked_citation_when_domain_is_in_the_sources(self):
        client = self._client_returning(
            "Polecam Early Stage.",
            [{"url": "https://earlystage.pl/kursy", "title": "Kursy"}],
        )

        result = ask_once("Jaka szkoła?", "earlystage.pl", client=client, brand_name="Early Stage")

        self.assertTrue(result.brand_cited)
        self.assertEqual(result.visibility, GeoRun.Visibility.LINKED_CITATION)

    def test_brand_mention_when_named_without_a_link(self):
        # Najciekawszy przypadek: model poleca firmę, ale linkuje konkurenta.
        client = self._client_returning(
            "Warto rozważyć Early Stage oraz Helen Doron.",
            [{"url": "https://helendoron.pl/", "title": "Helen Doron"}],
        )

        result = ask_once("Jaka szkoła?", "earlystage.pl", client=client, brand_name="Early Stage")

        self.assertFalse(result.brand_cited)
        self.assertTrue(result.brand_mentioned)
        self.assertEqual(result.visibility, GeoRun.Visibility.BRAND_MENTION)

    def test_absent_when_neither_linked_nor_named(self):
        client = self._client_returning(
            "Polecam Helen Doron i Berlitz.",
            [{"url": "https://helendoron.pl/", "title": "Helen Doron"}],
        )

        result = ask_once("Jaka szkoła?", "earlystage.pl", client=client, brand_name="Early Stage")

        self.assertFalse(result.brand_cited)
        self.assertFalse(result.brand_mentioned)
        self.assertEqual(result.visibility, GeoRun.Visibility.ABSENT)

    def test_link_wins_over_mention(self):
        # Cytowanie z linkiem przynosi ruch, sama wzmianka nie - więc ma pierwszeństwo.
        client = self._client_returning(
            "Early Stage prowadzi kursy.",
            [{"url": "https://earlystage.pl/", "title": "Early Stage"}],
        )

        result = ask_once("Jaka szkoła?", "earlystage.pl", client=client, brand_name="Early Stage")

        self.assertTrue(result.brand_cited)
        self.assertTrue(result.brand_mentioned)
        self.assertEqual(result.visibility, GeoRun.Visibility.LINKED_CITATION)

    def test_without_brand_name_only_links_are_detected(self):
        client = self._client_returning("Early Stage prowadzi kursy.", [])

        result = ask_once("Jaka szkoła?", "earlystage.pl", client=client)

        self.assertFalse(result.brand_mentioned)


class VisibilityTotalsTests(TestCase):
    """Liczniki i podsumowanie wykonawcze."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="geo-marka",
            password="haslo-kontrolne-1",
        )
        cls.study = GeoStudy.objects.create(
            owner=cls.user, domain="earlystage.pl", brand_name="Early Stage",
            status=GeoStudy.Status.COMPLETED, overall_score=40, repetitions=5,
        )
        cls.query = GeoQuery.objects.create(
            study=cls.study, text="Jaka szkoła językowa dla dzieci?", position=1,
            citation_rate=40, stability=GeoQuery.Stability.VOLATILE,
            competitors=[{"domain": "helendoron.pl", "count": 3}, {"domain": "berlitz.pl", "count": 1}],
        )
        # 2 linki, 2 wzmianki bez linku, 1 nieobecność.
        for attempt, (cited, mentioned) in enumerate(
            [(True, True), (True, False), (False, True), (False, True), (False, False)], start=1
        ):
            GeoRun.objects.create(
                query=cls.query, attempt=attempt,
                answer="Polecam Early Stage." if mentioned else "Polecam Helen Doron.",
                citations=[{"url": "https://earlystage.pl/", "domain": "earlystage.pl", "position": 1}]
                if cited else [{"url": "https://helendoron.pl/", "domain": "helendoron.pl", "position": 1}],
                brand_cited=cited, brand_mentioned=mentioned,
                brand_position=1 if cited else None,
                visibility=(
                    GeoRun.Visibility.LINKED_CITATION if cited
                    else GeoRun.Visibility.BRAND_MENTION if mentioned
                    else GeoRun.Visibility.ABSENT
                ),
            )

    def _queries(self):
        return list(self.study.queries.prefetch_related("runs").all())

    def test_totals_separate_links_from_mentions(self):
        totals = build_geo_visibility_totals(self._queries())

        self.assertEqual(totals["linked"], 2)
        self.assertEqual(totals["mentions"], 2)
        self.assertEqual(totals["visible"], 4)
        self.assertEqual(totals["absent"], 1)
        self.assertEqual(totals["total"], 5)

    def test_mention_counter_excludes_linked_runs(self):
        # Próba z linkiem ORAZ wzmianką liczy się tylko jako link.
        questions = build_geo_questions(self.study, self._queries())

        self.assertEqual(questions[0]["mention_runs"], 2)
        self.assertEqual(questions[0]["cited_runs"], 2)
        self.assertEqual(questions[0]["visible_runs"], 4)

    def test_failed_runs_are_not_counted(self):
        GeoRun.objects.create(
            query=self.query, attempt=6, error="Timeout", visibility=GeoRun.Visibility.ABSENT
        )

        totals = build_geo_visibility_totals(self._queries())

        self.assertEqual(totals["total"], 5)

    def test_summary_carries_brand_and_numbers(self):
        summary = build_geo_executive_summary(self.study, self._queries())

        self.assertEqual(summary["brand_name"], "Early Stage")
        self.assertEqual(summary["domain"], "earlystage.pl")
        self.assertEqual(summary["overall_score"], 40)
        self.assertEqual(summary["totals"]["linked"], 2)
        self.assertEqual(summary["totals"]["mentions"], 2)

    def test_summary_lists_top_competitors(self):
        summary = build_geo_executive_summary(self.study, self._queries())

        self.assertEqual(summary["top_competitors_list"], "helendoron.pl, berlitz.pl")

    def test_summary_falls_back_to_domain_without_a_brand(self):
        bez_marki = GeoStudy.objects.create(owner=self.user, domain="bezmarki.pl")

        summary = build_geo_executive_summary(bez_marki, [])

        self.assertEqual(summary["brand_name"], "bezmarki.pl")
        self.assertEqual(summary["top_competitors_list"], "")


class ExecutiveSummaryRenderingTests(TestCase):
    """Sekcja podsumowania na dole dashboardu."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="geo-podsumowanie",
            password="haslo-kontrolne-1",
        )
        cls.study = GeoStudy.objects.create(
            owner=cls.user, domain="earlystage.pl", brand_name="Early Stage",
            status=GeoStudy.Status.COMPLETED, overall_score=40, repetitions=2,
        )
        query = GeoQuery.objects.create(
            study=cls.study, text="Jaka szkoła językowa dla dzieci?", position=1,
            citation_rate=50, stability=GeoQuery.Stability.VOLATILE,
            competitors=[{"domain": "helendoron.pl", "count": 2}],
        )
        GeoRun.objects.create(
            query=query, attempt=1, answer="Polecam Early Stage.",
            citations=[{"url": "https://earlystage.pl/", "domain": "earlystage.pl", "position": 1}],
            brand_cited=True, brand_mentioned=True, brand_position=1,
            visibility=GeoRun.Visibility.LINKED_CITATION,
        )
        GeoRun.objects.create(
            query=query, attempt=2, answer="Warto rozważyć Early Stage oraz Helen Doron.",
            citations=[{"url": "https://helendoron.pl/", "domain": "helendoron.pl", "position": 1}],
            brand_cited=False, brand_mentioned=True,
            visibility=GeoRun.Visibility.BRAND_MENTION,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_summary_section_is_rendered(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, "Podsumowanie Widoczności AI")
        self.assertContains(response, "Early Stage")
        self.assertContains(response, "earlystage.pl")

    def test_summary_separates_links_from_mentions(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, "bezpośrednich odnośników URL")
        self.assertContains(response, "wzmianek o marce bez bezpośredniego linku")
        self.assertContains(response, "geo-executive-tile-mention")

    def test_summary_names_competitors(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, "helendoron.pl")

    def test_mention_badge_is_shown_for_the_run(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, "Wzmianka o marce")
        self.assertContains(response, "geo-run-brand_mention")

    def test_linked_run_is_labelled_as_a_link(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, "Cytowanie z linkiem")
        self.assertContains(response, "geo-run-linked_citation")
