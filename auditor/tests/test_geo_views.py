"""Testy zakładki "Widoczność w AI (GEO)" - widoki, formularz i silnik pomiarowy.

Żaden test nie odpytuje prawdziwej wyszukiwarki AI: klient OpenAI jest podmieniany,
a badania uruchamiane synchronicznie (CELERY_TASK_ALWAYS_EAGER).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from auditor.models import Audit, GeoQuery, GeoRun, GeoStudy
from auditor.services.geo import normalize_domain

User = get_user_model()


def _annotation(url: str, title: str = ""):
    annotation = MagicMock()
    annotation.url = url
    annotation.title = title
    return annotation


def _response(urls: list[str], text: str = "Odpowiedź modelu."):
    """Atrapa odpowiedzi Responses API z przypisami `url_citation`."""
    content = MagicMock()
    content.annotations = [_annotation(u) for u in urls]
    message = MagicMock()
    message.content = [content]
    response = MagicMock()
    response.output = [message]
    response.output_text = text
    return response


def _client_returning(*responses):
    """Klient OpenAI oddający kolejne odpowiedzi z listy (ostatnia się powtarza)."""
    client = MagicMock()
    kolejka = list(responses)

    def create(**kwargs):
        return kolejka.pop(0) if len(kolejka) > 1 else kolejka[0]

    client.responses.create.side_effect = create
    return client


class DomainNormalizationTests(SimpleTestCase):
    def test_strips_scheme_path_and_www(self):
        for value, expected in [
            ("https://www.orlen.pl/pl/dla-biznesu", "orlen.pl"),
            ("http://orlen.pl", "orlen.pl"),
            ("www.orlen.pl", "orlen.pl"),
            ("ORLEN.PL", "orlen.pl"),
            ("orlen.pl/sciezka", "orlen.pl"),
        ]:
            with self.subTest(wejscie=value):
                self.assertEqual(normalize_domain(value), expected)


class GeoDashboardViewTests(TestCase):
    """Lista badań i formularz uruchomienia."""

    def setUp(self):
        self.user = User.objects.create_user(username="marek", password="haslo-testowe-1")
        self.client.force_login(self.user)
        self.url = reverse("auditor:geo_dashboard")

    def test_requires_login(self):
        self.client.logout()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_dashboard_renders_with_the_geo_tab_marked_active(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/geo_dashboard.html")
        self.assertContains(response, "Widoczność w wyszukiwarkach AI")
        self.assertEqual(response.context["nav_section"], "geo")

    def test_navigation_lists_all_three_sections(self):
        response = self.client.get(self.url)

        for label in ("Analityka i Ruch", "Audyt Techniczny", "Widoczność w AI"):
            with self.subTest(sekcja=label):
                self.assertContains(response, label)

    def test_history_shows_only_own_studies(self):
        """Badanie zawiera dane konkurencyjne klienta - nie może wyciekać."""
        obcy = User.objects.create_user(username="obcy", password="haslo-testowe-2")
        GeoStudy.objects.create(owner=self.user, domain="moja.pl")
        GeoStudy.objects.create(owner=obcy, domain="cudza.pl")

        response = self.client.get(self.url)

        self.assertContains(response, "moja.pl")
        self.assertNotContains(response, "cudza.pl")

    def test_empty_state_is_shown_without_studies(self):
        response = self.client.get(self.url)

        self.assertContains(response, "Brak badań")


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class GeoStudyCreationTests(TestCase):
    """Formularz startowy: co powstaje po kliknięciu "Uruchom Symulator GEO"."""

    def setUp(self):
        self.user = User.objects.create_user(username="marek", password="haslo-testowe-1")
        self.client.force_login(self.user)
        self.url = reverse("auditor:geo_dashboard")

    def _post(self, **data):
        payload = {"domain": "orlen.pl"}
        payload.update(data)
        with patch("auditor.services.geo.run_study") as run:
            response = self.client.post(self.url, payload)
        return response, run

    def test_manual_questions_are_saved_in_order(self):
        response, _ = self._post(questions=["Pytanie A", "Pytanie B", "Pytanie C"])

        study = GeoStudy.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(study.domain, "orlen.pl")
        self.assertEqual(
            [q.text for q in study.queries.all()], ["Pytanie A", "Pytanie B", "Pytanie C"]
        )
        self.assertEqual([q.position for q in study.queries.all()], [1, 2, 3])

    def test_url_is_normalized_to_a_bare_domain(self):
        self._post(domain="https://www.orlen.pl/pl/dla-biznesu", questions=["P"])

        self.assertEqual(GeoStudy.objects.get().domain, "orlen.pl")

    def test_questions_are_generated_when_none_are_given(self):
        with patch(
            "auditor.services.geo.generate_questions",
            return_value=["Wygenerowane 1", "Wygenerowane 2"],
        ):
            with patch("auditor.services.geo.run_study"):
                self.client.post(self.url, {"domain": "orlen.pl"})

        self.assertEqual(
            [q.text for q in GeoStudy.objects.get().queries.all()],
            ["Wygenerowane 1", "Wygenerowane 2"],
        )

    def test_blank_questions_are_ignored(self):
        self._post(questions=["Pytanie A", "   ", ""])

        self.assertEqual(GeoStudy.objects.get().queries.count(), 1)

    def test_study_can_be_linked_to_an_audit(self):
        audit = Audit.objects.create(url="https://orlen.pl/", owner=self.user)

        self._post(audit=audit.pk, questions=["P"])

        self.assertEqual(GeoStudy.objects.get().audit, audit)

    def test_foreign_audit_cannot_be_linked(self):
        obcy = User.objects.create_user(username="obcy", password="haslo-testowe-2")
        audit = Audit.objects.create(url="https://cudza.pl/", owner=obcy)

        self._post(audit=audit.pk, questions=["P"])

        self.assertIsNone(GeoStudy.objects.get().audit)

    def test_missing_domain_is_rejected(self):
        response = self.client.post(self.url, {"domain": "   "})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(GeoStudy.objects.count(), 0)

    def test_private_address_is_rejected(self):
        """Domena trafia do promptu i do raportu - przechodzi tę samą ochronę
        co każdy adres pobierany przez serwer."""
        response = self.client.post(self.url, {"domain": "127.0.0.1"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(GeoStudy.objects.count(), 0)


class GeoDetailViewTests(TestCase):
    """Dashboard pojedynczego badania."""

    def setUp(self):
        self.user = User.objects.create_user(username="marek", password="haslo-testowe-1")
        self.client.force_login(self.user)
        self.study = GeoStudy.objects.create(
            owner=self.user, domain="orlen.pl", status=GeoStudy.Status.COMPLETED,
            overall_score=72, repetitions=5,
        )
        self.query = GeoQuery.objects.create(
            study=self.study, text="Jakie firmy oferują karty paliwowe?", position=1,
            citation_rate=80, stability=GeoQuery.Stability.STABLE,
            cited_positions=[1, 1, 3, 2],
            competitors=[{"domain": "shell.pl", "count": 3}, {"domain": "bp.com", "count": 1}],
        )
        GeoRun.objects.create(
            query=self.query, attempt=1, answer="Odpowiedź z cytowaniem.",
            citations=[{"url": "https://orlen.pl/a", "domain": "orlen.pl", "title": "", "position": 1}],
            brand_cited=True, brand_position=1,
        )

    def test_detail_shows_the_summary_score(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertEqual(response.status_code, 200)
        # Wynik siedzi w pierścieniu postępu: liczba i znak procentu w osobnych
        # elementach, żeby dało się je wyskalować niezależnie.
        self.assertContains(response, "geo-ring-value")
        self.assertContains(response, "72")
        self.assertContains(response, "GEO Score")

    def test_dashboard_shows_the_main_sections(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        for naglowek in (
            "Analiza widoczności w wyszukiwarkach AI ukończona!",
            "Ogólna widoczność",
            "Powtarzalność w próbach",
            "Szczegółowe wyniki dla poszczególnych pytań intencyjnych",
            "Przegląd zacytowanych źródeł i konkurencji",
        ):
            with self.subTest(sekcja=naglowek):
                self.assertContains(response, naglowek)

    def test_header_shows_domain_and_actions(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, "orlen.pl")
        self.assertContains(response, "Powrót do GEO Tracker")
        self.assertContains(response, "Ponów badanie")

    def test_row_shows_rate_stability_position_and_competitors(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, "Jakie firmy oferują karty paliwowe?")
        self.assertContains(response, "STABLE")
        self.assertContains(response, "#1")      # najczęstsza pozycja w przypisach
        self.assertContains(response, "shell.pl")

    def test_accordion_holds_the_full_answer_and_citations(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        # Odpowiedzi są rozwijane na miejscu - modal z własnym przewijaniem odszedł.
        self.assertContains(response, f'id="pytanie-{self.query.pk}"')
        self.assertContains(response, "<details class=\"geo-question\"")
        self.assertContains(response, "Odpowiedź z cytowaniem.")
        self.assertContains(response, "https://orlen.pl/a")

    def test_citations_are_shown_as_domain_pills(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        # Zamiast pełnego adresu użytkownik widzi czytelną etykietę domeny.
        self.assertContains(response, "geo-source-pill")
        self.assertContains(response, "Orlen.pl")

    def test_sources_table_lists_cited_domains_with_filters(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, 'id="geo-source-search"')
        self.assertContains(response, 'id="geo-source-question"')
        self.assertContains(response, 'data-domain="orlen.pl"')
        self.assertContains(response, "Twoja domena")

    def test_foreign_study_returns_404(self):
        obcy = User.objects.create_user(username="obcy", password="haslo-testowe-2")
        cudze = GeoStudy.objects.create(owner=obcy, domain="cudza.pl")

        response = self.client.get(reverse("auditor:geo_detail", args=[cudze.pk]))

        self.assertEqual(response.status_code, 404)

    def test_progress_bar_is_shown_only_while_running(self):
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))
        self.assertNotContains(response, 'id="geo-progress"')

        self.study.status = GeoStudy.Status.PROCESSING
        self.study.save(update_fields=["status"])
        response = self.client.get(reverse("auditor:geo_detail", args=[self.study.pk]))

        self.assertContains(response, 'id="geo-progress"')


class GeoStatusEndpointTests(TestCase):
    """Punkt odpytywany przez pasek postępu."""

    def setUp(self):
        self.user = User.objects.create_user(username="marek", password="haslo-testowe-1")
        self.client.force_login(self.user)
        self.study = GeoStudy.objects.create(
            owner=self.user, domain="orlen.pl",
            status=GeoStudy.Status.PROCESSING, repetitions=5,
        )
        self.queries = [
            GeoQuery.objects.create(study=self.study, text=f"Pytanie {i}", position=i)
            for i in range(1, 6)
        ]

    def test_progress_reports_the_current_question(self):
        """"Badanie zapytania 3 z 5" - numer wynika z liczby wykonanych powtórzeń."""
        for attempt in range(1, 6):
            GeoRun.objects.create(query=self.queries[0], attempt=attempt)
        for attempt in range(1, 6):
            GeoRun.objects.create(query=self.queries[1], attempt=attempt)

        payload = self.client.get(reverse("auditor:geo_status", args=[self.study.pk])).json()

        self.assertEqual(payload["done"], 10)
        self.assertEqual(payload["total"], 25)
        self.assertEqual(payload["percent"], 40)
        self.assertEqual(payload["current_query"], 3)
        self.assertEqual(payload["query_count"], 5)

    def test_progress_never_exceeds_the_number_of_questions(self):
        for query in self.queries:
            for attempt in range(1, 6):
                GeoRun.objects.create(query=query, attempt=attempt)

        payload = self.client.get(reverse("auditor:geo_status", args=[self.study.pk])).json()

        self.assertEqual(payload["percent"], 100)
        self.assertLessEqual(payload["current_query"], 5)

    def test_foreign_study_status_returns_404(self):
        obcy = User.objects.create_user(username="obcy", password="haslo-testowe-2")
        cudze = GeoStudy.objects.create(owner=obcy, domain="cudza.pl")

        response = self.client.get(reverse("auditor:geo_status", args=[cudze.pk]))

        self.assertEqual(response.status_code, 404)


class GeoSuggestQuestionsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="marek", password="haslo-testowe-1")
        self.client.force_login(self.user)
        self.url = reverse("auditor:geo_suggest_questions")

    def test_returns_generated_questions(self):
        with patch("auditor.services.geo.generate_questions", return_value=["A", "B"]):
            payload = self.client.post(self.url, {"domain": "orlen.pl"}).json()

        self.assertEqual(payload["questions"], ["A", "B"])

    def test_missing_domain_is_rejected(self):
        response = self.client.post(self.url, {"domain": ""})

        self.assertEqual(response.status_code, 400)

    def test_private_address_is_rejected(self):
        response = self.client.post(self.url, {"domain": "127.0.0.1"})

        self.assertEqual(response.status_code, 400)


class GeoEngineTests(TestCase):
    """Silnik pomiarowy: cytowania, stabilność, konkurenci."""

    def setUp(self):
        self.study = GeoStudy.objects.create(domain="orlen.pl", repetitions=4)
        self.query = GeoQuery.objects.create(study=self.study, text="Pytanie", position=1)

    def _run(self, client):
        from auditor.services.geo import run_study

        run_study(self.study, client=client)
        self.study.refresh_from_db()
        self.query.refresh_from_db()

    def test_domain_cited_every_time_is_stable(self):
        self._run(_client_returning(_response(["https://orlen.pl/oferta", "https://shell.pl/x"])))

        self.assertEqual(self.query.citation_rate, 100)
        self.assertEqual(self.query.stability, GeoQuery.Stability.STABLE)
        self.assertEqual(self.study.overall_score, 100)
        self.assertEqual(self.study.status, GeoStudy.Status.COMPLETED)

    def test_domain_never_cited_is_absent(self):
        self._run(_client_returning(_response(["https://shell.pl/x", "https://bp.com/y"])))

        self.assertEqual(self.query.citation_rate, 0)
        self.assertEqual(self.query.stability, GeoQuery.Stability.ABSENT)

    def test_mixed_results_are_volatile(self):
        """Sedno powtórzeń: raz cytowany, raz nie - to inna sytuacja niż obie skrajne."""
        client = _client_returning(
            _response(["https://orlen.pl/a"]),
            _response(["https://shell.pl/x"]),
            _response(["https://orlen.pl/a"]),
            _response(["https://shell.pl/x"]),
        )
        self._run(client)

        self.assertEqual(self.query.citation_rate, 50)
        self.assertEqual(self.query.stability, GeoQuery.Stability.VOLATILE)

    def test_competitors_come_only_from_runs_without_the_brand(self):
        """Kolumna odpowiada na pytanie "kogo model cytuje, GDY nie cytuje nas"."""
        client = _client_returning(
            _response(["https://orlen.pl/a", "https://lotos.pl/x"]),   # marka obecna
            _response(["https://shell.pl/x", "https://bp.com/y"]),     # marka nieobecna
            _response(["https://shell.pl/z"]),                          # marka nieobecna
            _response(["https://orlen.pl/a"]),                          # marka obecna
        )
        self._run(client)

        domains = [c["domain"] for c in self.query.competitors]
        self.assertIn("shell.pl", domains)
        self.assertNotIn("lotos.pl", domains)
        self.assertEqual(self.query.competitors[0], {"domain": "shell.pl", "count": 2})

    def test_brand_position_in_citations_is_recorded(self):
        self._run(_client_returning(_response(["https://shell.pl/x", "https://orlen.pl/a"])))

        self.assertEqual(self.query.cited_positions, [2, 2, 2, 2])
        self.assertEqual(self.query.most_common_position, "#2 (4x)")

    def test_www_prefix_does_not_hide_the_brand(self):
        self._run(_client_returning(_response(["https://www.orlen.pl/oferta"])))

        self.assertEqual(self.query.citation_rate, 100)

    def test_repeated_domain_counts_as_one_source(self):
        """Serwis z pięcioma podstronami nie może zawyżać listy źródeł."""
        self._run(_client_returning(
            _response(["https://shell.pl/a", "https://shell.pl/b", "https://orlen.pl/x"])
        ))
        run = GeoRun.objects.filter(query=self.query).first()

        self.assertEqual([c["domain"] for c in run.citations], ["shell.pl", "orlen.pl"])

    def test_failed_calls_do_not_count_against_the_rate(self):
        """Błąd sieci to brak pomiaru, nie brak cytowania - inaczej awaria API
        wyglądałaby w raporcie jak utrata widoczności."""
        client = MagicMock()
        odpowiedzi = [_response(["https://orlen.pl/a"]), RuntimeError("API padło"),
                      _response(["https://orlen.pl/a"]), RuntimeError("API padło")]

        def create(**kwargs):
            item = odpowiedzi.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        client.responses.create.side_effect = create
        self._run(client)

        self.assertEqual(self.query.citation_rate, 100)

    def test_missing_api_key_marks_the_study_as_failed(self):
        from auditor.services.geo import GeoSimulatorError, run_study

        with patch("auditor.services.geo._client", side_effect=GeoSimulatorError("brak klucza")):
            run_study(self.study)
        self.study.refresh_from_db()

        self.assertEqual(self.study.status, GeoStudy.Status.FAILED)
        self.assertIn("brak klucza", self.study.error)

    def test_web_search_is_forced_so_answers_carry_citations(self):
        """Bez wymuszenia narzędzia model bywa odpowiada z pamięci - wtedy nie ma
        przypisów i pomiar widoczności traci podstawę."""
        client = _client_returning(_response(["https://orlen.pl/a"]))
        self._run(client)

        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["tool_choice"], "required")
        self.assertEqual(kwargs["tools"], [{"type": "web_search"}])
