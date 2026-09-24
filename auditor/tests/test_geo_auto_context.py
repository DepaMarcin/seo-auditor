"""Automatyczne rozpoznawanie branży strony przed ułożeniem pytań GEO.

Problem, który ten mechanizm rozwiązuje: mając samą nazwę domeny model zgaduje, czym
firma się zajmuje, i regularnie wraca z pytaniami o SEO, pozycjonowanie i marketing -
bo tak wygląda większość stron, które zna. Dopiero treść strony przed oczami modelu
daje pytania z właściwej branży.

Ani sieć, ani OpenAI nie są tu prawdziwe: każde pobranie i każde wywołanie modelu
jest podmienione.
"""
from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from auditor.models import Audit, AuditMetric
from auditor.services.geo import (
    CONTEXT_SNIPPET_WORDS,
    SiteContext,
    _parse_question_list,
    generate_questions,
    get_or_fetch_site_context,
)
from auditor.services.scraper import ScraperError

# Strona z częściami do pojazdów użytkowych - branża odległa od SEO na tyle, że
# generyczne pytania marketingowe od razu rzucają się w oczy.
ARCOORE_HTML = """
<html>
<head>
    <title>Arcoore — części zamienne do samochodów ciężarowych i naczep</title>
    <meta name="description" content="Hurtownia części do ciężarówek: układy hamulcowe,
          zawieszenie pneumatyczne, filtry i sprzęgła do DAF, MAN, Scania i Volvo.">
</head>
<body>
    <h1>Części zamienne do pojazdów użytkowych</h1>
    <h2>Układy hamulcowe do naczep</h2>
    <h2>Zawieszenie pneumatyczne</h2>
    <h3>Filtry i sprzęgła</h3>
    <script>var analytics = "to nie jest treść strony";</script>
    <p>
        Dostarczamy części zamienne do samochodów ciężarowych, ciągników siodłowych
        i naczep. W magazynie trzymamy tarcze i klocki hamulcowe, miechy powietrzne,
        amortyzatory, filtry paliwa oraz sprzęgła do najpopularniejszych marek.
    </p>
</body>
</html>
"""

MOTORYZACYJNE_PYTANIA = [
    "Jakie klocki hamulcowe do naczepy wybrać do intensywnej eksploatacji?",
    "Gdzie kupić miechy powietrzne do zawieszenia pneumatycznego ciężarówki?",
    "Ile kosztuje wymiana sprzęgła w ciągniku siodłowym?",
    "Czym różnią się filtry paliwa do silników DAF i Scania?",
    "Jak rozpoznać zużyte amortyzatory w samochodzie ciężarowym?",
]


def _fake_getaddrinfo(hostname, *args, **kwargs):
    """Deterministyczne DNS - testy nie mogą zależeć od odpowiedzi sieci."""
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


def _model_returning(questions) -> MagicMock:
    """Atrapa klienta OpenAI zwracająca podane pytania jako tablicę JSON."""
    client = MagicMock()
    client.responses.create.return_value = SimpleNamespace(
        output_text=json.dumps(questions, ensure_ascii=False)
    )
    return client


class SiteScanTests(TestCase):
    """Pobranie treści strony, gdy nie ma z czego skorzystać w bazie."""

    def test_scan_extracts_title_description_headings_and_body(self):
        with patch("auditor.services.scraper.SEOScraper.fetch", return_value=ARCOORE_HTML):
            context = get_or_fetch_site_context("https://arcoore.com/")

        self.assertEqual(context.source, "scan")
        self.assertIn("części zamienne", context.title.lower())
        self.assertIn("hurtownia części", context.meta_description.lower())
        self.assertIn("Części zamienne do pojazdów użytkowych", context.headings)
        self.assertIn("Układy hamulcowe do naczep", context.headings)
        self.assertIn("miechy powietrzne", context.body_snippet)

    def test_scripts_do_not_leak_into_the_content_snippet(self):
        # Kod JS w treści zająłby limit słów i zaśmiecił prompt.
        with patch("auditor.services.scraper.SEOScraper.fetch", return_value=ARCOORE_HTML):
            context = get_or_fetch_site_context("https://arcoore.com/")

        self.assertNotIn("analytics", context.body_snippet)

    def test_snippet_is_capped(self):
        dlugi = "<html><body><p>" + ("słowo " * 900) + "</p></body></html>"

        with patch("auditor.services.scraper.SEOScraper.fetch", return_value=dlugi):
            context = get_or_fetch_site_context("https://arcoore.com/")

        self.assertEqual(len(context.body_snippet.split()), CONTEXT_SNIPPET_WORDS)

    def test_failed_download_is_reported_not_raised(self):
        with patch(
            "auditor.services.scraper.SEOScraper.fetch",
            side_effect=ScraperError("HTTP 404"),
        ):
            context = get_or_fetch_site_context("https://arcoore.com/nie-ma/")

        self.assertFalse(context.usable)
        self.assertIn("404", context.error)

    def test_empty_page_is_not_usable(self):
        with patch("auditor.services.scraper.SEOScraper.fetch", return_value="<html></html>"):
            context = get_or_fetch_site_context("https://arcoore.com/")

        self.assertFalse(context.usable)


class ContextFromAuditTests(TestCase):
    """Świeży audyt tej samej domeny oszczędza pobranie."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="geo-kontekst",
            password="haslo-kontrolne-1",
        )

    def _audit_with_metrics(self, **kwargs) -> Audit:
        audit = Audit.objects.create(
            url="https://arcoore.com/",
            owner=self.user,
            status=Audit.Status.COMPLETED,
            **kwargs,
        )
        AuditMetric.objects.create(
            audit=audit, category="seo", key="title",
            value={"value": "Arcoore — części do ciężarówek"},
        )
        AuditMetric.objects.create(
            audit=audit, category="seo", key="meta_description",
            value={"value": "Hurtownia części do naczep i ciągników siodłowych."},
        )
        AuditMetric.objects.create(
            audit=audit, category="technical", key="h1_structure",
            value={"headings": ["Części zamienne do pojazdów użytkowych"]},
        )
        return audit

    def test_recent_audit_is_used_without_fetching(self):
        self._audit_with_metrics()

        with patch("auditor.services.scraper.SEOScraper.fetch") as fetch:
            context = get_or_fetch_site_context("https://arcoore.com/")

        fetch.assert_not_called()
        self.assertEqual(context.source, "audit")
        self.assertIn("ciężarówek", context.title)
        self.assertIn("naczep", context.meta_description)
        self.assertEqual(context.headings, ["Części zamienne do pojazdów użytkowych"])

    def test_stale_audit_falls_back_to_a_fresh_scan(self):
        audit = self._audit_with_metrics()
        Audit.objects.filter(pk=audit.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=60)
        )

        with patch("auditor.services.scraper.SEOScraper.fetch", return_value=ARCOORE_HTML):
            context = get_or_fetch_site_context("https://arcoore.com/")

        self.assertEqual(context.source, "scan")

    def test_unfinished_audit_is_ignored(self):
        # Audyt w trakcie nie ma jeszcze kompletu metryk.
        self._audit_with_metrics()
        Audit.objects.update(status=Audit.Status.PENDING)

        with patch("auditor.services.scraper.SEOScraper.fetch", return_value=ARCOORE_HTML):
            context = get_or_fetch_site_context("https://arcoore.com/")

        self.assertEqual(context.source, "scan")

    def test_audit_of_a_different_domain_is_ignored(self):
        self._audit_with_metrics()

        with patch("auditor.services.scraper.SEOScraper.fetch", return_value=ARCOORE_HTML):
            context = get_or_fetch_site_context("https://zupelnie-inna-domena.pl/")

        self.assertEqual(context.source, "scan")


class IndustryPromptTests(SimpleTestCase):
    """Treść strony trafia do promptu i wyznacza branżę pytań."""

    def _context(self) -> SiteContext:
        return SiteContext(
            url="https://arcoore.com/",
            title="Arcoore — części zamienne do samochodów ciężarowych",
            meta_description="Hurtownia części do ciężarówek: hamulce, zawieszenie, filtry.",
            headings=["Części zamienne do pojazdów użytkowych", "Układy hamulcowe do naczep"],
            body_snippet="Dostarczamy części zamienne do ciągników siodłowych i naczep.",
            source="scan",
        )

    def test_prompt_carries_the_scraped_context(self):
        client = _model_returning(MOTORYZACYJNE_PYTANIA)

        with patch("auditor.services.geo._client", return_value=client):
            generate_questions("arcoore.com", context=self._context())

        prompt = client.responses.create.call_args.kwargs["input"]
        self.assertIn("https://arcoore.com/", prompt)
        self.assertIn("części zamienne do samochodów ciężarowych", prompt)
        self.assertIn("Układy hamulcowe do naczep", prompt)
        self.assertIn("ciągników siodłowych", prompt)

    def test_prompt_forbids_brand_name_and_seo_topics(self):
        client = _model_returning(MOTORYZACYJNE_PYTANIA)

        with patch("auditor.services.geo._client", return_value=client):
            generate_questions("arcoore.com", context=self._context())

        prompt = client.responses.create.call_args.kwargs["input"]
        self.assertIn("ZAKAZ używania nazwy marki", prompt)
        self.assertIn("ZAKAZ pytań o pozycjonowanie, SEO ani marketing", prompt)
        # Pytania poradnikowe model odpowiada artykułami z blogów, więc firma nie ma
        # szansy zostać zacytowana - pomiar mierzyłby wtedy co innego.
        self.assertIn("ZAKAZ pytań poradnikowych i teoretycznych", prompt)

    def test_generated_questions_are_about_vehicle_parts(self):
        client = _model_returning(MOTORYZACYJNE_PYTANIA)

        with patch("auditor.services.geo._client", return_value=client):
            questions = generate_questions("arcoore.com", context=self._context())

        self.assertEqual(len(questions), 5)
        motoryzacyjne = (
                    "hamulc", "ciężar", "naczep", "sprzęgł",
                    "amortyzator", "filtr", "siodłow", "zawieszeni",
                )
        for question in questions:
            with self.subTest(question=question):
                self.assertTrue(
                    any(slowo in question.lower() for slowo in motoryzacyjne),
                    f"pytanie spoza branży motoryzacyjnej: {question}",
                )

    def test_generated_questions_avoid_seo_vocabulary(self):
        client = _model_returning(MOTORYZACYJNE_PYTANIA)

        with patch("auditor.services.geo._client", return_value=client):
            questions = generate_questions("arcoore.com", context=self._context())

        for question in questions:
            with self.subTest(question=question):
                for zakazane in ("seo", "pozycjonowanie", "marketing"):
                    self.assertNotIn(zakazane, question.lower())

    def test_generated_questions_do_not_name_the_brand(self):
        client = _model_returning(MOTORYZACYJNE_PYTANIA)

        with patch("auditor.services.geo._client", return_value=client):
            questions = generate_questions("arcoore.com", context=self._context())

        for question in questions:
            with self.subTest(question=question):
                self.assertNotIn("arcoore", question.lower())

    def test_context_free_prompt_is_used_without_context(self):
        client = _model_returning(MOTORYZACYJNE_PYTANIA)

        with patch("auditor.services.geo._client", return_value=client):
            generate_questions("arcoore.com")

        prompt = client.responses.create.call_args.kwargs["input"]
        self.assertIn("strategiem widoczności", prompt)
        self.assertNotIn("FRAGMENT TREŚCI", prompt)


class QuestionParsingTests(SimpleTestCase):
    """Model prosimy o JSON, ale odpowiada różnie."""

    def test_plain_json_array(self):
        self.assertEqual(_parse_question_list('["Pierwsze?", "Drugie?"]'), ["Pierwsze?", "Drugie?"])

    def test_json_array_in_a_code_fence(self):
        raw = '```json\n["Pierwsze?", "Drugie?"]\n```'

        self.assertEqual(_parse_question_list(raw), ["Pierwsze?", "Drugie?"])

    def test_plain_lines_still_work(self):
        # Starszy format odpowiedzi - lista w liniach zamiast JSON-a.
        raw = "- Pierwsze?\n- Drugie?"

        self.assertEqual(_parse_question_list(raw), ["Pierwsze?", "Drugie?"])

    def test_empty_answer_gives_no_questions(self):
        self.assertEqual(_parse_question_list(""), [])


class SuggestEndpointTests(TestCase):
    """Przycisk "✨ Wygeneruj przez AI" w panelu GEO."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="geo-endpoint",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_endpoint_returns_industry_questions(self):
        client = _model_returning(MOTORYZACYJNE_PYTANIA)

        with patch("auditor.services.scraper.SEOScraper.fetch", return_value=ARCOORE_HTML), \
             patch("auditor.services.geo._client", return_value=client):
            response = self.client.post(
                reverse("auditor:geo_suggest_questions"),
                {"domain": "https://arcoore.com/"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["questions"], MOTORYZACYJNE_PYTANIA)
        self.assertEqual(payload["context_source"], "scan")
        self.assertIn("Arcoore", payload["detected_title"])

    def test_unreachable_page_returns_the_documented_message(self):
        with patch(
            "auditor.services.scraper.SEOScraper.fetch",
            side_effect=ScraperError("HTTP 404"),
        ):
            response = self.client.post(
                reverse("auditor:geo_suggest_questions"),
                {"domain": "https://arcoore.com/nie-ma/"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn(
            "Nie udało się pobrać treści podanego adresu URL",
            response.json()["error"],
        )

    def test_path_is_kept_when_fetching_context(self):
        # `normalize_domain` obcina ścieżkę - kontekst ma pochodzić z tej podstrony,
        # którą wskazał użytkownik, a nie ze strony głównej.
        client = _model_returning(MOTORYZACYJNE_PYTANIA)

        with patch("auditor.services.scraper.SEOScraper.fetch", return_value=ARCOORE_HTML) as fetch, \
             patch("auditor.services.geo._client", return_value=client):
            self.client.post(
                reverse("auditor:geo_suggest_questions"),
                {"domain": "https://arcoore.com/czesci/hamulce/"},
            )

        self.assertEqual(fetch.call_args.args[0], "https://arcoore.com/czesci/hamulce/")

    def test_local_address_is_still_rejected(self):
        response = self.client.post(
            reverse("auditor:geo_suggest_questions"),
            {"domain": "http://127.0.0.1/admin"},
        )

        self.assertEqual(response.status_code, 400)

    def test_missing_domain_is_rejected(self):
        response = self.client.post(reverse("auditor:geo_suggest_questions"), {"domain": ""})

        self.assertEqual(response.status_code, 400)
