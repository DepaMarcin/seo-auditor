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
    build_geo_benchmark,
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
    parse_competitors_input,
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
        # Wynik liczony z prób (2 linki + 2 wzmianki na 5 prób = 80%), a nie
        # odczytany z `study.overall_score` - ten trzyma jeszcze starą punktację
        # sprzed ujednolicenia.
        self.assertEqual(summary["overall_score"], 80)
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


class CompetitorInputParsingTests(SimpleTestCase):
    """Domeny konkurentów wklejane są w dowolnym formacie."""

    def test_comma_separated_domains(self):
        self.assertEqual(
            parse_competitors_input("autos.com.pl, opoltrans.com.pl"),
            ["autos.com.pl", "opoltrans.com.pl"],
        )

    def test_full_urls_and_duplicates_are_normalised(self):
        wynik = parse_competitors_input(
            "https://www.autos.com.pl/oferta\nopoltrans.com.pl; AUTOS.COM.PL"
        )

        self.assertEqual(wynik, ["autos.com.pl", "opoltrans.com.pl"])

    def test_own_domain_is_dropped(self):
        # Porównanie z samym sobą zafałszowałoby średnią konkurencji.
        wynik = parse_competitors_input("moja.pl, rywal.pl", exclude="https://moja.pl/")

        self.assertEqual(wynik, ["rywal.pl"])

    def test_at_most_five_competitors(self):
        wynik = parse_competitors_input("a.pl,b.pl,c.pl,d.pl,e.pl,f.pl,g.pl")

        self.assertEqual(len(wynik), 5)

    def test_empty_input_gives_empty_list(self):
        self.assertEqual(parse_competitors_input(""), [])
        self.assertEqual(parse_competitors_input(None), [])


class CompetitorBenchmarkTests(TestCase):
    """Wynik każdej domeny liczony z tych samych prób."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="geo-benchmark",
            password="haslo-kontrolne-1",
        )

    def _study(self, competitors=None) -> GeoStudy:
        """Badanie o znanym rozkładzie: 4 próby, każda z innym układem widoczności."""
        study = GeoStudy.objects.create(
            owner=self.user, domain="earlystage.pl", brand_name="Early Stage",
            competitors_input=competitors or [],
            status=GeoStudy.Status.COMPLETED, overall_score=50, repetitions=4,
        )
        query = GeoQuery.objects.create(
            study=study, text="Jaka szkoła językowa dla dzieci?", position=1,
        )

        # 1: my z linkiem, Helen Doron z linkiem
        # 2: my z linkiem, Helen Doron tylko z nazwy
        # 3: my tylko z nazwy, Helen Doron z linkiem
        # 4: nas nie ma, Helen Doron z linkiem, Berlitz z nazwy
        uklad = [
            ("Polecam Early Stage oraz Helen Doron.",
             ["https://earlystage.pl/", "https://helendoron.pl/"], True, True),
            ("Polecam Early Stage, alternatywą jest Helen Doron.",
             ["https://earlystage.pl/"], True, True),
            ("Warto rozważyć Early Stage.", ["https://helendoron.pl/"], False, True),
            ("Polecam Helen Doron oraz Berlitz.", ["https://helendoron.pl/"], False, False),
        ]
        for attempt, (answer, urls, cited, mentioned) in enumerate(uklad, start=1):
            GeoRun.objects.create(
                query=query, attempt=attempt, answer=answer,
                citations=[
                    {"url": u, "domain": u.split("//")[1].strip("/"), "position": i}
                    for i, u in enumerate(urls, start=1)
                ],
                brand_cited=cited, brand_mentioned=mentioned,
                brand_position=1 if cited else None,
                visibility=(
                    GeoRun.Visibility.LINKED_CITATION if cited
                    else GeoRun.Visibility.BRAND_MENTION if mentioned
                    else GeoRun.Visibility.ABSENT
                ),
            )
        return study

    def _queries(self, study):
        return list(study.queries.prefetch_related("runs").all())

    def test_declared_competitor_score_counts_links_and_mentions(self):
        study = self._study(competitors=["helendoron.pl"])

        benchmark = build_geo_benchmark(study, self._queries(study))
        rywal = benchmark["competitors"][0]

        self.assertEqual(rywal["domain"], "helendoron.pl")
        self.assertEqual(rywal["linked"], 3)   # próby 1, 3, 4
        self.assertEqual(rywal["mentions"], 1)  # próba 2 - nazwa bez linku
        self.assertEqual(rywal["score"], 100)

    def test_own_score_counts_links_and_mentions(self):
        study = self._study(competitors=["helendoron.pl"])

        benchmark = build_geo_benchmark(study, self._queries(study))

        self.assertEqual(benchmark["own"]["linked"], 2)
        self.assertEqual(benchmark["own"]["mentions"], 1)
        self.assertEqual(benchmark["own"]["score"], 75)

    def test_average_and_delta_for_two_competitors(self):
        study = self._study(competitors=["helendoron.pl", "berlitz.pl"])

        benchmark = build_geo_benchmark(study, self._queries(study))

        # helendoron 100%, berlitz 25% (sama nazwa w próbie 4) -> 62.5%, które
        # `round()` sprowadza do parzystej 62 - tak samo jak wszystkie inne
        # wskaźniki procentowe w tym module.
        self.assertEqual(benchmark["average_competitor_score"], 62)
        self.assertEqual(benchmark["score_delta"], 75 - 62)

    def test_negative_delta_when_competition_is_stronger(self):
        study = self._study(competitors=["helendoron.pl"])

        benchmark = build_geo_benchmark(study, self._queries(study))

        self.assertEqual(benchmark["average_competitor_score"], 100)
        self.assertEqual(benchmark["score_delta"], -25)

    def test_top_three_competitors_are_picked_automatically(self):
        study = self._study()

        benchmark = build_geo_benchmark(study, self._queries(study))

        self.assertTrue(benchmark["auto_selected"])
        self.assertEqual(
            [r["domain"] for r in benchmark["competitors"]],
            ["helendoron.pl"],
        )

    def test_declared_competitors_switch_off_auto_selection(self):
        study = self._study(competitors=["berlitz.pl"])

        benchmark = build_geo_benchmark(study, self._queries(study))

        self.assertFalse(benchmark["auto_selected"])
        self.assertEqual([r["domain"] for r in benchmark["competitors"]], ["berlitz.pl"])

    def test_own_domain_is_always_the_first_row(self):
        study = self._study(competitors=["helendoron.pl"])

        benchmark = build_geo_benchmark(study, self._queries(study))

        self.assertTrue(benchmark["rows"][0]["is_brand"])
        self.assertEqual(benchmark["rows"][0]["domain"], "earlystage.pl")

    def test_study_without_any_citations_has_no_benchmark(self):
        puste = GeoStudy.objects.create(
            owner=self.user, domain="pusta.pl", status=GeoStudy.Status.COMPLETED,
        )

        benchmark = build_geo_benchmark(puste, [])

        self.assertFalse(benchmark["has_competitors"])
        self.assertEqual(benchmark["average_competitor_score"], 0)
        self.assertEqual(benchmark["score_delta"], 0)

    def test_failed_runs_are_excluded_from_the_denominator(self):
        study = self._study(competitors=["helendoron.pl"])
        query = study.queries.first()
        GeoRun.objects.create(
            query=query, attempt=5, error="Timeout", visibility=GeoRun.Visibility.ABSENT
        )

        benchmark = build_geo_benchmark(study, self._queries(study))

        self.assertEqual(benchmark["own"]["total"], 4)


class BenchmarkRenderingTests(TestCase):
    """Karta KPI konkurencji i tabela porównawcza na dashboardzie."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="geo-benchmark-ui",
            password="haslo-kontrolne-1",
        )
        cls.study = GeoStudy.objects.create(
            owner=cls.user, domain="earlystage.pl", brand_name="Early Stage",
            competitors_input=["helendoron.pl"],
            status=GeoStudy.Status.COMPLETED, overall_score=50, repetitions=2,
        )
        query = GeoQuery.objects.create(study=cls.study, text="Jaka szkoła?", position=1)
        GeoRun.objects.create(
            query=query, attempt=1, answer="Polecam Early Stage.",
            citations=[{"url": "https://earlystage.pl/", "domain": "earlystage.pl", "position": 1}],
            brand_cited=True, brand_mentioned=True, brand_position=1,
            visibility=GeoRun.Visibility.LINKED_CITATION,
        )
        GeoRun.objects.create(
            query=query, attempt=2, answer="Polecam Helen Doron.",
            citations=[{"url": "https://helendoron.pl/", "domain": "helendoron.pl", "position": 1}],
            brand_cited=False, brand_mentioned=False,
            visibility=GeoRun.Visibility.ABSENT,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_third_kpi_card_is_rendered(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, "Widoczność Konkurencji")
        self.assertContains(response, "Średni GEO Score konkurencji")

    def test_delta_pill_shows_the_direction(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        # Obie domeny po 50% - wynik równy średniej.
        self.assertContains(response, "geo-delta-even")

    def test_comparison_table_lists_both_domains(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, "Porównanie bezpośrednie z konkurencją")
        self.assertContains(response, "geo-benchmark-table")
        self.assertContains(response, "helendoron.pl")
        self.assertContains(response, "Cytowania z linkiem")
        self.assertContains(response, "Wzmianki bez linku")

    def test_own_row_is_highlighted(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, "geo-benchmark-row-brand")
        self.assertContains(response, "Twoja domena")


class CompetitorFormTests(TestCase):
    """Pole konkurentów w formularzu startowym."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="geo-formularz",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_field_is_present_with_its_hint(self):
        html = self.client.get(reverse("auditor:geo_dashboard")).content.decode()

        self.assertIn('name="competitors"', html)
        self.assertIn("Konkurenci do porównania (opcjonalnie)", html)
        self.assertIn("autos.com.pl, opoltrans.com.pl", html)
        self.assertIn("TOP 3 najczęstszych", html)

    def test_submitted_competitors_are_stored_on_the_study(self):
        with patch("auditor.tasks.enqueue_geo_study"):
            self.client.post(reverse("auditor:geo_dashboard"), {
                "domain": "earlystage.pl",
                "questions": ["Jaka szkoła językowa?"],
                "competitors": "helendoron.pl, https://www.berlitz.pl/kursy",
            })

        study = GeoStudy.objects.get()
        self.assertEqual(study.competitors_input, ["helendoron.pl", "berlitz.pl"])

    def test_empty_field_leaves_automatic_selection(self):
        with patch("auditor.tasks.enqueue_geo_study"):
            self.client.post(reverse("auditor:geo_dashboard"), {
                "domain": "earlystage.pl",
                "questions": ["Jaka szkoła językowa?"],
                "competitors": "",
            })

        self.assertEqual(GeoStudy.objects.get().competitors_input, [])


class UnifiedScoringTests(TestCase):
    """Jedna próba = jeden punkt za obecność, niezależnie od formy.

    Rozkład w danych: 25 prób, w tym 7 z linkiem, 6 z samą nazwą marki, 12 bez
    obecności - czyli 13 obecności i 52%. Ta liczba musi wyjść identycznie w karcie
    KPI, w tabeli porównawczej i w podsumowaniu; osobne "procenty za linki" i
    "procenty za markę" nie istnieją.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="geo-punktacja",
            password="haslo-kontrolne-1",
        )
        cls.study = GeoStudy.objects.create(
            owner=cls.user, domain="earlystage.pl", brand_name="Early Stage",
            competitors_input=["helendoron.pl"],
            status=GeoStudy.Status.COMPLETED, repetitions=5,
        )

        # 5 pytań po 5 prób. Układ na pytanie: link, link, wzmianka, wzmianka, brak -
        # poza ostatnim pytaniem, gdzie sama wzmianka wypada na brak. Razem 7 linków
        # i 6 wzmianek.
        uklady = [
            [("link", 1), ("link", 2), ("nazwa", 3), ("nazwa", 4), ("brak", 5)],
            [("link", 1), ("link", 2), ("nazwa", 3), ("brak", 4), ("brak", 5)],
            [("link", 1), ("nazwa", 2), ("nazwa", 3), ("brak", 4), ("brak", 5)],
            [("link", 1), ("nazwa", 2), ("brak", 3), ("brak", 4), ("brak", 5)],
            [("link", 1), ("brak", 2), ("brak", 3), ("brak", 4), ("brak", 5)],
        ]
        for numer, uklad in enumerate(uklady, start=1):
            query = GeoQuery.objects.create(
                study=cls.study, text=f"Jaka szkoła językowa - pytanie {numer}?",
                position=numer,
            )
            for rodzaj, attempt in uklad:
                cited = rodzaj == "link"
                mentioned = rodzaj in ("link", "nazwa")
                GeoRun.objects.create(
                    query=query, attempt=attempt,
                    answer="Polecam Early Stage." if mentioned else "Polecam Helen Doron.",
                    citations=[{"url": "https://earlystage.pl/", "domain": "earlystage.pl", "position": 1}]
                    if cited
                    else [{"url": "https://helendoron.pl/", "domain": "helendoron.pl", "position": 1}],
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

    def test_link_and_bare_mention_both_score_a_point(self):
        totals = build_geo_visibility_totals(self._queries())

        self.assertEqual(totals["linked"], 7)
        self.assertEqual(totals["mentions"], 6)
        self.assertEqual(totals["visible"], 13)
        self.assertEqual(totals["total"], 25)
        self.assertEqual(totals["score"], 52)

    def test_run_with_both_counts_only_once(self):
        # Próba z linkiem ORAZ nazwą to nadal jeden punkt - inaczej suma obecności
        # przekroczyłaby liczbę prób.
        totals = build_geo_visibility_totals(self._queries())

        self.assertEqual(totals["linked"] + totals["mentions"], totals["visible"])
        self.assertLessEqual(totals["visible"], totals["total"])

    def test_engine_writes_the_same_score(self):
        from auditor.services.geo import _overall_score, _summarize_query

        for query in self._queries():
            _summarize_query(query, self.study.domain)

        self.assertEqual(_overall_score(self._queries()), 52)

    def test_kpi_card_matches_the_comparison_table(self):
        queries = self._queries()

        totals = build_geo_visibility_totals(queries)
        benchmark = build_geo_benchmark(self.study, queries)

        self.assertEqual(totals["score"], benchmark["own"]["score"])
        self.assertEqual(benchmark["own"]["linked"], 7)
        self.assertEqual(benchmark["own"]["mentions"], 6)

    def test_executive_summary_matches_the_kpi_card(self):
        queries = self._queries()

        totals = build_geo_visibility_totals(queries)
        summary = build_geo_executive_summary(self.study, queries)

        self.assertEqual(summary["overall_score"], totals["score"])
        self.assertEqual(summary["overall_score"], 52)

    def test_competitor_score_uses_the_same_rule(self):
        # Helen Doron: link w 12 próbach bez naszej obecności -> ta sama reguła.
        benchmark = build_geo_benchmark(self.study, self._queries())
        rywal = benchmark["competitors"][0]

        self.assertEqual(rywal["domain"], "helendoron.pl")
        self.assertEqual(rywal["visible"], rywal["linked"] + rywal["mentions"])
        self.assertEqual(
            rywal["score"], round(rywal["visible"] * 100 / rywal["total"])
        )

    def test_delta_is_the_difference_of_the_two_unified_scores(self):
        benchmark = build_geo_benchmark(self.study, self._queries())

        self.assertEqual(
            benchmark["score_delta"],
            benchmark["own"]["score"] - benchmark["average_competitor_score"],
        )


class ScoreConsistencyRenderingTests(TestCase):
    """Ta sama liczba w trzech miejscach wyrenderowanej strony."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="geo-spojnosc",
            password="haslo-kontrolne-1",
        )
        # Zapisany `overall_score` celowo błędny - strona ma pokazać wynik liczony
        # z prób, a nie odziedziczoną liczbę ze starej punktacji.
        cls.study = GeoStudy.objects.create(
            owner=cls.user, domain="earlystage.pl", brand_name="Early Stage",
            competitors_input=["helendoron.pl"],
            status=GeoStudy.Status.COMPLETED, overall_score=25, repetitions=4,
        )
        query = GeoQuery.objects.create(study=cls.study, text="Jaka szkoła?", position=1)
        uklad = [(True, True), (False, True), (False, True), (False, False)]
        for attempt, (cited, mentioned) in enumerate(uklad, start=1):
            GeoRun.objects.create(
                query=query, attempt=attempt,
                answer="Polecam Early Stage." if mentioned else "Polecam Helen Doron.",
                citations=[{"url": "https://earlystage.pl/", "domain": "earlystage.pl", "position": 1}]
                if cited
                else [{"url": "https://helendoron.pl/", "domain": "helendoron.pl", "position": 1}],
                brand_cited=cited, brand_mentioned=mentioned,
                brand_position=1 if cited else None,
                visibility=(
                    GeoRun.Visibility.LINKED_CITATION if cited
                    else GeoRun.Visibility.BRAND_MENTION if mentioned
                    else GeoRun.Visibility.ABSENT
                ),
            )

    def setUp(self):
        self.client.force_login(self.user)

    def test_all_three_sections_show_the_same_number(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        # 3 obecności na 4 próby = 75%.
        self.assertEqual(response.context["visibility"]["score"], 75)
        self.assertEqual(response.context["benchmark"]["own"]["score"], 75)
        self.assertEqual(response.context["summary"]["overall_score"], 75)

    def test_stale_saved_score_is_not_displayed(self):
        html = self.client.get(
            reverse("auditor:geo_detail", args=[self.study.pk])
        ).content.decode()

        pierscien = html.split('class="geo-ring-value"', 1)[1][:60]
        self.assertIn("75", pierscien)
        self.assertNotIn("25", pierscien)

    def test_card_shows_the_split_into_links_and_mentions(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, "Obecna w")
        self.assertContains(response, "3/4")
        self.assertContains(response, "1 z linkiem")
        self.assertContains(response, "2 wzmianek bez linku")

    def test_competitor_card_lists_the_analysed_domains(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, "geo-competitor-list")
        self.assertContains(response, "helendoron.pl")

    def test_sections_appear_in_the_required_order(self):
        html = self.client.get(
            reverse("auditor:geo_detail", args=[self.study.pk])
        ).content.decode()

        kolejnosc = [
            "Ogólna widoczność",
            "Powtarzalność w próbach",
            "Widoczność Konkurencji",
            "Porównanie bezpośrednie z konkurencją",
            "Szczegółowe wyniki dla poszczególnych pytań intencyjnych",
            "Podsumowanie Widoczności AI",
        ]
        pozycje = [html.index(naglowek) for naglowek in kolejnosc]

        self.assertEqual(pozycje, sorted(pozycje), "sekcje w złej kolejności")

    def test_brand_name_is_in_the_header(self):
        html = self.client.get(
            reverse("auditor:geo_detail", args=[self.study.pk])
        ).content.decode()

        naglowek = html.split('class="geo-header-subtitle"', 1)[1][:300]
        self.assertIn("earlystage.pl", naglowek)
        self.assertIn("Early Stage", naglowek)
