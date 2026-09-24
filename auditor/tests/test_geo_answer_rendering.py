"""Formatowanie odpowiedzi modelu i dane dashboardu GEO.

Model odpowiada Markdownem. Wstawiony do szablonu wprost pokazuje użytkownikowi
"**[CHEERS Sp. z o.o.]**" i adresy URL na pół ekranu zamiast czytelnego tekstu ze
źródłami. Tekst jest przy tym NIEZAUFANY - powstaje z treści cytowanych stron.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.models import GeoQuery, GeoRun, GeoStudy
from auditor.presentation import (
    build_geo_questions,
    build_geo_repetition_stats,
    build_geo_sources,
    citation_label,
    render_ai_answer,
)


class AnswerFormattingTests(SimpleTestCase):
    """Markdown z odpowiedzi zamienia się w HTML, a nie trafia do tekstu dosłownie."""

    def test_bold_becomes_strong(self):
        html = render_ai_answer("**[CHEERS Sp. z o.o.]** oferuje karty paliwowe.")

        self.assertIn("<strong>[CHEERS Sp. z o.o.]</strong>", html)
        self.assertNotIn("**", html)

    def test_bare_url_becomes_a_domain_pill(self):
        html = render_ai_answer("Szczegóły: https://www.orlen.pl/pl/dla-biznesu/karty-flotowe")

        self.assertIn("geo-source-pill", html)
        self.assertIn("Orlen.pl", html)
        # Długi adres nie rozbija już akapitu.
        self.assertNotIn(">https://www.orlen.pl/pl/dla-biznesu/karty-flotowe<", html)

    def test_markdown_link_keeps_its_label(self):
        html = render_ai_answer("Zobacz [ofertę Shell](https://shell.pl/biznes).")

        self.assertIn("ofertę Shell", html)
        self.assertIn('href="https://shell.pl/biznes"', html)
        self.assertNotIn("](", html)

    def test_lists_become_list_items(self):
        html = render_ai_answer("Dostawcy:\n- Pierwszy\n- Drugi")

        self.assertIn("<ul", html)
        self.assertEqual(html.count("<li>"), 2)

    def test_headings_become_headings(self):
        html = render_ai_answer("## Podsumowanie\nTreść akapitu.")

        self.assertIn("geo-answer-heading", html)
        self.assertNotIn("##", html)

    def test_paragraphs_are_split_on_blank_lines(self):
        html = render_ai_answer("Pierwszy akapit.\n\nDrugi akapit.")

        self.assertEqual(html.count("<p>"), 2)

    def test_empty_answer_renders_nothing(self):
        self.assertEqual(render_ai_answer(""), "")
        self.assertEqual(render_ai_answer(None), "")


class AnswerSafetyTests(SimpleTestCase):
    """Treść pochodzi z cytowanych stron, więc nie może wnieść własnego HTML-a."""

    def test_html_in_the_answer_is_escaped(self):
        html = render_ai_answer("<script>alert(1)</script> zwykły tekst")

        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_javascript_link_is_not_turned_into_a_link(self):
        html = render_ai_answer("[kliknij](javascript:alert(1))")

        self.assertNotIn('href="javascript:', html.lower())
        self.assertNotIn("<a ", html)

    def test_image_and_event_handlers_do_not_survive(self):
        html = render_ai_answer('<img src=x onerror="alert(1)">')

        # Znaczniki zostają tekstem w akapicie, nie stają się elementami.
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)


class CitationLabelTests(SimpleTestCase):
    """Etykieta źródła zamiast adresu."""

    def test_www_is_dropped_and_first_letter_capitalised(self):
        self.assertEqual(citation_label("https://www.orlen.pl/oferta"), "Orlen.pl")

    def test_subdomain_is_kept(self):
        self.assertEqual(citation_label("https://biznes.shell.pl/karty"), "Biznes.shell.pl")

    def test_broken_url_does_not_raise(self):
        self.assertTrue(citation_label("to nie jest adres"))
        self.assertEqual(citation_label(""), "źródło")


class DashboardDataTests(TestCase):
    """Dane kart metryk i tabeli źródeł."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="geo-dashboard",
            password="haslo-kontrolne-1",
        )
        cls.study = GeoStudy.objects.create(
            owner=cls.user, domain="orlen.pl",
            status=GeoStudy.Status.COMPLETED, overall_score=60, repetitions=2,
        )
        cls.first = GeoQuery.objects.create(
            study=cls.study, text="Jakie firmy oferują karty paliwowe?", position=1,
            citation_rate=100, stability=GeoQuery.Stability.STABLE,
        )
        cls.second = GeoQuery.objects.create(
            study=cls.study, text="Która karta flotowa jest najtańsza?", position=2,
            citation_rate=0, stability=GeoQuery.Stability.ABSENT,
        )
        # Pierwsze pytanie: cytowanie w obu próbach.
        for attempt in (1, 2):
            GeoRun.objects.create(
                query=cls.first, attempt=attempt, answer="**Orlen** oferuje karty.",
                citations=[
                    {"url": "https://orlen.pl/a", "domain": "orlen.pl", "title": "Karty", "position": 1},
                    {"url": "https://shell.pl/b", "domain": "shell.pl", "title": "Shell", "position": 2},
                ],
                brand_cited=True, brand_position=1,
            )
        # Drugie pytanie: ani razu. Przypis tylko w pierwszej próbie.
        GeoRun.objects.create(
            query=cls.second, attempt=1, answer="Najtańsza jest karta X.",
            citations=[{"url": "https://bp.com/c", "domain": "bp.com", "title": "BP", "position": 1}],
            brand_cited=False,
        )
        GeoRun.objects.create(
            query=cls.second, attempt=2, answer="Trudno wskazać jedną.", citations=[],
            brand_cited=False,
        )

    def _queries(self):
        return list(self.study.queries.prefetch_related("runs").all())

    def test_questions_carry_rendered_answers(self):
        questions = build_geo_questions(self.study, self._queries())

        self.assertEqual(len(questions), 2)
        self.assertEqual(questions[0]["number"], 1)
        self.assertIn("<strong>Orlen</strong>", questions[0]["runs"][0]["answer_html"])

    def test_brand_citation_is_marked(self):
        questions = build_geo_questions(self.study, self._queries())

        citations = questions[0]["runs"][0]["citations"]
        self.assertTrue(citations[0]["is_brand"])
        self.assertFalse(citations[1]["is_brand"])
        self.assertEqual(citations[0]["label"], "Orlen.pl")

    def test_repetition_stats_split_by_attempt(self):
        stats = build_geo_repetition_stats(self.study, self._queries())

        self.assertEqual(len(stats), 2)
        for stat in stats:
            with self.subTest(attempt=stat["attempt"]):
                # Jedno z dwóch pytań uzyskało cytowanie w każdej próbie.
                self.assertEqual(stat["cited"], 1)
                self.assertEqual(stat["total"], 2)
                self.assertEqual(stat["percent"], 50)

    def test_sources_are_aggregated_and_sorted(self):
        sources = build_geo_sources(self.study, self._queries())

        domains = [s["domain"] for s in sources]
        self.assertEqual(domains, ["orlen.pl", "shell.pl", "bp.com"])
        self.assertEqual(sources[0]["count"], 2)

    def test_brand_domain_is_flagged_in_sources(self):
        sources = build_geo_sources(self.study, self._queries())
        by_domain = {s["domain"]: s for s in sources}

        self.assertTrue(by_domain["orlen.pl"]["is_brand"])
        self.assertFalse(by_domain["shell.pl"]["is_brand"])

    def test_sources_record_which_questions_cited_them(self):
        sources = build_geo_sources(self.study, self._queries())
        by_domain = {s["domain"]: s for s in sources}

        self.assertEqual(by_domain["orlen.pl"]["question_filter"], f"q{self.first.pk}")
        self.assertEqual(by_domain["bp.com"]["question_count"], 1)

    def test_empty_study_produces_no_stats(self):
        puste = GeoStudy.objects.create(owner=self.user, domain="pusta.pl")

        self.assertEqual(build_geo_repetition_stats(puste, []), [])
        self.assertEqual(build_geo_sources(puste, []), [])


class RerunTests(TestCase):
    """Przycisk "Ponów badanie"."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="geo-rerun",
            password="haslo-kontrolne-1",
        )
        cls.study = GeoStudy.objects.create(
            owner=cls.user, domain="orlen.pl",
            status=GeoStudy.Status.COMPLETED, repetitions=3,
        )
        GeoQuery.objects.create(study=cls.study, text="Pierwsze pytanie?", position=1)
        GeoQuery.objects.create(study=cls.study, text="Drugie pytanie?", position=2)

    def setUp(self):
        self.client.force_login(self.user)

    def test_rerun_creates_a_new_study_with_the_same_questions(self):
        response = self.client.post(reverse("auditor:geo_rerun", args=[self.study.pk]))

        self.assertEqual(GeoStudy.objects.count(), 2)
        nowe = GeoStudy.objects.exclude(pk=self.study.pk).get()
        self.assertEqual(nowe.domain, "orlen.pl")
        self.assertEqual(nowe.repetitions, 3)
        self.assertEqual(
            list(nowe.queries.values_list("text", flat=True)),
            ["Pierwsze pytanie?", "Drugie pytanie?"],
        )
        self.assertRedirects(response, reverse("auditor:geo_detail", args=[nowe.pk]))

    def test_previous_study_is_kept(self):
        # Sens pomiaru GEO to porównanie kolejnych pomiarów w czasie.
        self.client.post(reverse("auditor:geo_rerun", args=[self.study.pk]))

        self.study.refresh_from_db()
        self.assertEqual(self.study.status, GeoStudy.Status.COMPLETED)
        self.assertEqual(self.study.queries.count(), 2)

    def test_get_is_rejected(self):
        response = self.client.get(reverse("auditor:geo_rerun", args=[self.study.pk]))

        self.assertEqual(response.status_code, 405)

    def test_foreign_study_cannot_be_rerun(self):
        obcy = get_user_model().objects.create_user(username="obcy", password="haslo-kontrolne-2")
        cudze = GeoStudy.objects.create(owner=obcy, domain="cudza.pl")

        response = self.client.post(reverse("auditor:geo_rerun", args=[cudze.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(GeoStudy.objects.filter(domain="cudza.pl").count(), 1)

    def test_rerun_requires_login(self):
        self.client.logout()

        response = self.client.post(reverse("auditor:geo_rerun", args=[self.study.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)
