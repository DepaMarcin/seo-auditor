"""Testy pre-flightu dostępności dla robotów i wyłącznika bezpieczeństwa.

Problem, który te mechanizmy rozwiązują: dla serwisu renderowanego po stronie klienta
albo chronionego przez WAF parser dostaje pusty szkielet i audyt produkuje serię
fałszywych błędów - "Brak H1", "Brak Schema.org", "Thin content - 12 słów" - opisując
problemy, których na stronie nie ma.

Ani `httpx`, ani Playwright, ani DNS nie są tu prawdziwe: każde pobranie i każde
rozwiązanie nazwy jest podmienione.
"""
from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from auditor.services import renderer
from auditor.services.accessibility import (
    DIAGNOSIS_BLOCKED,
    DIAGNOSIS_CSR,
    DIAGNOSIS_OK,
    DIAGNOSIS_PRERENDER_GATED,
    DIAGNOSIS_UNKNOWN,
    GOOGLEBOT_USER_AGENT,
    check_bot_accessibility,
)
from auditor.services.scraper import ScraperError


# `validate_public_url` woła `socket.getaddrinfo`, czyli prawdziwy DNS. Przy dłuższym
# biegu całego zestawu potrafi on chwilowo nie odpowiedzieć - walidacja odrzuca wtedy
# adres, `check_bot_accessibility` kończy się wcześnie i zwraca pustą tabelę
# porównawczą. Test przestaje wtedy mierzyć to, co miał mierzyć, i wywala się losowo.
_dns_patch = None


def _fake_getaddrinfo(hostname, *args, **kwargs):
    """Nazwa hosta rozwiązuje się na stały adres publiczny; adresy IP zostają sobą.

    Dzięki temu sprawdzenie odrzucania loopbacku nadal działa - 127.0.0.1 przechodzi
    przez tę funkcję bez zmiany i wpada w blokadę adresów prywatnych.
    """
    import ipaddress

    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        address = "93.184.216.34"
    else:
        address = hostname
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]


def setUpModule():
    global _dns_patch
    _dns_patch = patch("auditor.services.url_guard.socket.getaddrinfo", _fake_getaddrinfo)
    _dns_patch.start()


def tearDownModule():
    _dns_patch.stop()

# Szkielet aplikacji CSR: kilkanaście słów nawigacji, treść powstaje dopiero w JS.
CSR_RAW = """
<html><head><title>Sklep</title></head>
<body><div id="root"></div><nav><a href="/oferta">Oferta</a></nav>
<script src="/app.js"></script></body></html>
"""

CSR_RENDERED = """
<html><head><title>Sklep</title>
<script type="application/ld+json">{"@type":"Organization"}</script></head>
<body>
  <h1>Paliwa i stacje</h1>
  <nav><a href="/oferta">Oferta</a><a href="/kontakt">Kontakt</a></nav>
  <p>%s</p>
</body></html>
""" % (" ".join(["slowo"] * 800))

SSR_HTML = """
<html><head><title>Sklep</title>
<script type="application/ld+json">{"@type":"Organization"}</script></head>
<body><h1>Paliwa i stacje</h1><a href="/oferta">Oferta</a><p>%s</p></body></html>
""" % (" ".join(["slowo"] * 800))


def _scraper(raw_html: str | None = None, error: Exception | None = None,
             bot_html: str | None = None, bot_error: Exception | None = None) -> MagicMock:
    """Atrapa scrapera rozróżniająca dwie tożsamości.

    `raw_html`/`error` dotyczą pomiaru nagłówkami audytu, `bot_html`/`bot_error` -
    sondy Googlebota. Bez osobnych wartości dla sondy obie tożsamości dostają to samo,
    czyli tak, jak zachowuje się serwis bez dynamic renderingu.
    """
    scraper = MagicMock()

    def fetch_raw(url, user_agent=None):
        if user_agent is None:
            if error is not None:
                raise error
            return raw_html
        if bot_error is not None:
            raise bot_error
        if bot_html is not None:
            return bot_html
        if error is not None:
            raise error
        return raw_html

    scraper.fetch_raw.side_effect = fetch_raw
    return scraper


class PreflightComparisonTests(SimpleTestCase):
    """Porównanie surowego HTML z wyrenderowanym DOM."""

    def _check(self, raw_html=None, rendered_html=None, raw_error=None, render_error=None):
        with patch("auditor.services.accessibility.renderer.render_html") as render:
            if render_error is not None:
                render.side_effect = render_error
            else:
                render.return_value = rendered_html
            return check_bot_accessibility(
                "https://przyklad.pl/", scraper=_scraper(raw_html, raw_error)
            )

    def test_raw_html_is_fetched_with_the_googlebot_user_agent(self):
        """Pre-flight odpowiada na pytanie "co widzi robot" - pyta jego tożsamością."""
        scraper = _scraper(SSR_HTML)
        with patch("auditor.services.accessibility.renderer.render_html", return_value=SSR_HTML):
            check_bot_accessibility("https://przyklad.pl/", scraper=scraper)

        user_agents = [c.kwargs["user_agent"] for c in scraper.fetch_raw.call_args_list]

        # Pierwszy pomiar nagłówkami audytu (to on zasila wyłącznik), drugi - sonda.
        self.assertEqual(user_agents, [None, GOOGLEBOT_USER_AGENT])

    def test_server_side_rendered_page_is_accessible(self):
        report = self._check(raw_html=SSR_HTML, rendered_html=SSR_HTML)

        self.assertEqual(report.diagnosis, DIAGNOSIS_OK)
        self.assertTrue(report.is_accessible)

    def test_client_side_rendering_is_detected(self):
        report = self._check(raw_html=CSR_RAW, rendered_html=CSR_RENDERED)

        self.assertEqual(report.diagnosis, DIAGNOSIS_CSR)
        self.assertFalse(report.is_accessible)
        self.assertIn("CSR", report.note)

    def test_csr_diagnosis_quotes_both_word_counts(self):
        report = self._check(raw_html=CSR_RAW, rendered_html=CSR_RENDERED)

        self.assertLess(report.raw.word_count, 50)
        self.assertGreater(report.rendered.word_count, 300)
        self.assertIn(str(report.raw.word_count), report.note)
        self.assertIn(str(report.rendered.word_count), report.note)

    def test_waf_block_is_detected_when_only_the_browser_gets_through(self):
        report = self._check(
            raw_error=ScraperError("Nie udało się pobrać: HTTP 403 Forbidden"),
            rendered_html=SSR_HTML,
        )

        self.assertEqual(report.diagnosis, DIAGNOSIS_BLOCKED)
        self.assertFalse(report.is_accessible)
        self.assertEqual(report.raw.status_code, 403)
        self.assertIn("403", report.note)

    def test_both_sides_empty_is_reported_as_a_challenge_page(self):
        pusty = "<html><body><div>Sprawdzanie przeglądarki</div></body></html>"
        report = self._check(raw_html=pusty, rendered_html=pusty)

        self.assertEqual(report.diagnosis, DIAGNOSIS_BLOCKED)
        self.assertFalse(report.is_accessible)

    def test_total_failure_of_both_paths_is_a_block(self):
        report = self._check(
            raw_error=ScraperError("timeout"), render_error=renderer.RendererError("padło")
        )

        self.assertEqual(report.diagnosis, DIAGNOSIS_BLOCKED)
        self.assertFalse(report.is_accessible)

    def test_rich_raw_html_needs_no_browser_to_be_declared_accessible(self):
        """Gdy audyt sam dostaje treść, renderowanie nie jest do niczego potrzebne -
        instalacja bez Playwrighta działa wtedy w pełni."""
        report = self._check(
            raw_html=SSR_HTML,
            render_error=renderer.RendererUnavailableError("brak playwrighta"),
        )

        self.assertEqual(report.diagnosis, DIAGNOSIS_OK)
        self.assertTrue(report.is_accessible)

    def test_stub_without_any_comparison_stays_undecided(self):
        """Pusta powłoka, której nie ma z czym zestawić, to diagnoza niepewna -
        a niepewność nie może wyłączać testów."""
        scraper = _scraper(CSR_RAW, bot_error=ScraperError("sonda padła"))
        with patch(
            "auditor.services.accessibility.renderer.render_html",
            side_effect=renderer.RendererUnavailableError("brak playwrighta"),
        ):
            report = check_bot_accessibility("https://przyklad.pl/", scraper=scraper)

        self.assertEqual(report.diagnosis, DIAGNOSIS_UNKNOWN)
        self.assertTrue(report.is_accessible)

    def test_content_served_only_to_declared_bots_is_detected(self):
        """Wzorzec Prerender.io: audyt dostaje powłokę, Googlebot - pełny snapshot.

        Regresja z produkcji (shell.pl): wersja mierząca surowy HTML wyłącznie
        tożsamością Googlebota trafiała w snapshot, stawiała diagnozę "ok" i wyłącznik
        nie działał, choć audyt liczył metryki z 12-słownej powłoki.
        """
        scraper = _scraper(raw_html=CSR_RAW, bot_html=SSR_HTML)
        with patch(
            "auditor.services.accessibility.renderer.render_html",
            side_effect=renderer.RendererUnavailableError("brak playwrighta"),
        ):
            report = check_bot_accessibility("https://przyklad.pl/", scraper=scraper)

        self.assertEqual(report.diagnosis, DIAGNOSIS_PRERENDER_GATED)
        self.assertFalse(report.is_accessible)
        self.assertTrue(report.is_prerender_gated)
        self.assertIn("zadeklarowanym botom", report.note)

    def test_prerender_gating_is_detected_without_a_browser(self):
        """Diagnoza opiera się na różnicy audyt vs Googlebot - Playwright zbędny."""
        scraper = _scraper(raw_html=CSR_RAW, bot_html=SSR_HTML)
        with patch(
            "auditor.services.accessibility.renderer.render_html",
            side_effect=renderer.RendererUnavailableError("brak playwrighta"),
        ):
            report = check_bot_accessibility("https://przyklad.pl/", scraper=scraper)

        self.assertFalse(report.rendered.available)
        self.assertEqual(report.diagnosis, DIAGNOSIS_PRERENDER_GATED)

    def test_unsafe_url_is_rejected_before_any_request(self):
        scraper = _scraper(SSR_HTML)
        report = check_bot_accessibility("http://127.0.0.1/admin", scraper=scraper)

        scraper.fetch_raw.assert_not_called()
        self.assertFalse(report.checked)


class ComparisonTableTests(SimpleTestCase):
    """Tabela "co widzi prosty robot vs co widzi użytkownik"."""

    def setUp(self):
        with patch(
            "auditor.services.accessibility.renderer.render_html", return_value=CSR_RENDERED
        ):
            self.report = check_bot_accessibility(
                "https://przyklad.pl/", scraper=_scraper(CSR_RAW)
            )

    def test_table_covers_words_h1_schema_and_links(self):
        labels = [row["label"] for row in self.report.comparison]

        self.assertEqual(len(labels), 4)
        for expected in ("Liczba słów", "H1", "JSON-LD", "Linki wewnętrzne"):
            with self.subTest(row=expected):
                self.assertTrue(any(expected in label for label in labels))

    def test_rows_that_differ_are_flagged(self):
        by_label = {row["label"]: row for row in self.report.comparison}

        self.assertTrue(by_label["Nagłówek H1"]["differs"])
        self.assertEqual(by_label["Nagłówek H1"]["raw"], "Brak")
        self.assertIn("Znaleziono", by_label["Nagłówek H1"]["rendered"])

    def test_schema_present_only_after_rendering_is_visible_in_the_table(self):
        schema = next(r for r in self.report.comparison if "JSON-LD" in r["label"])

        self.assertEqual(schema["raw"], "Brak")
        self.assertIn("Znaleziono", schema["rendered"])
        self.assertTrue(schema["differs"])


class CircuitBreakerTests(TestCase):
    """Wyłącznik: jeden trafny błąd zamiast kilkunastu fałszywych."""

    def _service(self):
        from auditor.services.audit_service import AuditService

        service = AuditService.__new__(AuditService)
        service.rag_engine = MagicMock()
        service.rag_engine.generate_recommendation.return_value = ""
        service.site_domain = "przyklad.pl"
        return service

    def _report(self, diagnosis):
        from auditor.services.accessibility import AccessibilityReport

        report = AccessibilityReport(url="https://przyklad.pl/", diagnosis=diagnosis)
        report.note = "opis diagnozy"
        return report

    def _metrics(self):
        service = self._service()
        return [
            service._make_metric("seo", "h1_structure", {"note": "Brak H1."}, "error"),
            service._make_metric("seo", "thin_content", {"note": "12 słów."}, "error"),
            service._make_metric("technical", "internal_linking", {"note": "0 linków."}, "error"),
            service._make_metric("structure", "schema_page_type", {"note": "Brak."}, "error"),
            service._make_metric("seo", "title", {"note": "Tytuł za długi."}, "warning"),
            service._make_metric("performance", "mobile_lcp", {"note": "Wolno."}, "error"),
        ]

    def test_accessible_page_keeps_every_metric_untouched(self):
        service = self._service()
        metrics = self._metrics()

        result = service._apply_circuit_breaker(metrics, self._report(DIAGNOSIS_OK))

        self.assertEqual(result, metrics)

    def test_csr_page_marks_content_metrics_as_skipped(self):
        service = self._service()

        result = service._apply_circuit_breaker(self._metrics(), self._report(DIAGNOSIS_CSR))
        by_key = {m["key"]: m for m in result}

        for key in ("h1_structure", "thin_content", "internal_linking", "schema_page_type"):
            with self.subTest(metric=key):
                self.assertEqual(by_key[key]["status"], "skipped")

    def test_head_level_and_performance_metrics_are_still_evaluated(self):
        """Sekcja <head> i PageSpeed działają niezależnie od treści w <body> -
        wyłączanie ich odebrałoby audytowi informacje, które są prawdziwe."""
        service = self._service()

        result = service._apply_circuit_breaker(self._metrics(), self._report(DIAGNOSIS_CSR))
        by_key = {m["key"]: m for m in result}

        self.assertEqual(by_key["title"]["status"], "warning")
        self.assertEqual(by_key["mobile_lcp"]["status"], "error")

    def test_skipped_metrics_carry_an_explanation(self):
        service = self._service()

        result = service._apply_circuit_breaker(self._metrics(), self._report(DIAGNOSIS_BLOCKED))
        skipped = next(m for m in result if m["key"] == "h1_structure")

        self.assertIn("Test pominięty", skipped["value"]["note"])
        self.assertEqual(skipped["value"]["skipped_reason"], DIAGNOSIS_BLOCKED)

    def test_no_metric_disappears_from_the_result(self):
        """Metryki nie znikają, tylko zmieniają status - inaczej w interfejsie
        po cichu ubyłoby kart, a rejestr metryk przestałby się zgadzać."""
        service = self._service()
        metrics = self._metrics()

        result = service._apply_circuit_breaker(metrics, self._report(DIAGNOSIS_CSR))

        self.assertEqual([m["key"] for m in result], [m["key"] for m in metrics])

    def test_unknown_diagnosis_does_not_disable_anything(self):
        service = self._service()
        metrics = self._metrics()

        result = service._apply_circuit_breaker(metrics, self._report(DIAGNOSIS_UNKNOWN))

        self.assertEqual(result, metrics)


class SkippedMetricsScoringTests(TestCase):
    """Pominięta metryka nie może zaniżać wyniku - to byłby ten sam defekt,
    który wyłącznik ma usunąć, tylko wyrażony liczbą zamiast listą błędów."""

    def _service(self):
        from auditor.services.audit_service import AuditService

        return AuditService.__new__(AuditService)

    def test_skipped_metrics_are_excluded_from_the_score(self):
        service = self._service()
        metrics = [
            {"key": "a", "status": "ok"},
            {"key": "b", "status": "ok"},
            {"key": "c", "status": "skipped"},
            {"key": "d", "status": "skipped"},
        ]

        self.assertEqual(service._calculate_score(metrics), 100)

    def test_score_without_skipping_would_be_lower(self):
        """Kontrola wprost: gdyby pominięte liczyły się jak błędy, wynik spadłby o połowę."""
        service = self._service()
        skipped = [{"key": "a", "status": "ok"}, {"key": "b", "status": "skipped"}]
        errors = [{"key": "a", "status": "ok"}, {"key": "b", "status": "error"}]

        self.assertEqual(service._calculate_score(skipped), 100)
        self.assertEqual(service._calculate_score(errors), 50)

    def test_all_metrics_skipped_yields_zero_instead_of_crashing(self):
        service = self._service()

        self.assertEqual(service._calculate_score([{"key": "a", "status": "skipped"}]), 0)

    def test_category_scores_also_ignore_skipped_metrics(self):
        from auditor.models import Audit, AuditMetric
        from auditor.presentation import compute_category_scores

        audit = Audit.objects.create(url="https://przyklad.pl/", status="completed")
        metrics = [
            AuditMetric(audit=audit, category="seo", key="title", status="ok", value={}),
            AuditMetric(audit=audit, category="seo", key="h1_structure", status="skipped", value={}),
        ]
        AuditMetric.objects.bulk_create(metrics)

        scores = {row["key"]: row for row in compute_category_scores(list(audit.metrics.all()))}

        self.assertEqual(scores["seo"]["score"], 100)


class AccessibilityMetricTests(TestCase):
    """Metryka zbiorcza zapisywana w audycie."""

    def _service(self):
        from auditor.services.audit_service import AuditService

        service = AuditService.__new__(AuditService)
        service.rag_engine = MagicMock()
        service.rag_engine.generate_recommendation.return_value = ""
        service.site_domain = "przyklad.pl"
        return service

    def _report(self, diagnosis):
        from auditor.services.accessibility import AccessibilityReport, RenderSnapshot

        report = AccessibilityReport(url="https://przyklad.pl/", diagnosis=diagnosis)
        report.note = "Wykryto CSR."
        report.raw = RenderSnapshot(available=True, word_count=12)
        report.rendered = RenderSnapshot(available=True, word_count=1250, h1_count=1)
        report.comparison = [
            {"label": "Liczba słów treści", "raw": "12", "rendered": "1250", "differs": True}
        ]
        return report

    def test_csr_is_reported_as_an_error(self):
        metric = self._service()._evaluate_bot_accessibility(self._report(DIAGNOSIS_CSR))

        self.assertEqual(metric["key"], "bot_accessibility")
        self.assertEqual(metric["status"], "error")
        self.assertFalse(metric["value"]["is_accessible"])

    def test_accessible_page_is_reported_as_ok(self):
        metric = self._service()._evaluate_bot_accessibility(self._report(DIAGNOSIS_OK))

        self.assertEqual(metric["status"], "ok")

    def test_unknown_diagnosis_is_informational_not_an_error(self):
        """Brak Playwrighta to ograniczenie narzędzia, nie wada audytowanej strony."""
        metric = self._service()._evaluate_bot_accessibility(self._report(DIAGNOSIS_UNKNOWN))

        self.assertEqual(metric["status"], "info")

    def test_metric_carries_the_comparison_table(self):
        metric = self._service()._evaluate_bot_accessibility(self._report(DIAGNOSIS_CSR))

        self.assertEqual(len(metric["value"]["comparison"]), 1)
        self.assertIn("Liczba słów", metric["current_value"])

    def test_metric_is_registered_in_the_ui_dictionaries(self):
        from auditor.presentation import (
            METRIC_DEFINITIONS,
            OFFICIAL_TEST_NAMES,
            TECHNICAL_ACCORDIONS,
        )

        self.assertIn("bot_accessibility", METRIC_DEFINITIONS)
        self.assertIn("bot_accessibility", OFFICIAL_TEST_NAMES)
        all_keys = set().union(*(keys for _, _, keys in TECHNICAL_ACCORDIONS))
        self.assertIn("bot_accessibility", all_keys)


class DomainConsistencyTests(TestCase):
    """Rekomendacje muszą używać domeny klienta, nie zaślepek z bazy wiedzy."""

    def _engine(self):
        from auditor.services.rag import RAGEngine

        engine = RAGEngine()
        engine.api_key = "sk-test"
        engine._embeddings = MagicMock()
        engine._collection = MagicMock()
        engine._collection.query.return_value = {"ids": [[]]}
        return engine

    def _prompt_for(self, **kwargs) -> str:
        answer = MagicMock()
        answer.content = "### 1. DIAGNOZA"
        client = MagicMock()
        client.invoke.return_value = answer

        from auditor.services.rag import RAGEngine

        with patch.object(RAGEngine, "_build_llm", return_value=client):
            self._engine().generate_recommendation("Problem.", **kwargs)
        return client.invoke.call_args.args[0][0].content

    def test_audited_domain_reaches_the_system_prompt(self):
        prompt = self._prompt_for(site_domain="shell.pl")

        self.assertIn("shell.pl", prompt)

    def test_prompt_forbids_the_anonymised_placeholders(self):
        prompt = self._prompt_for(site_domain="shell.pl")

        for placeholder in ("klient-a.pl", "klient-b.pl", "Marka A", "Metodyk A"):
            with self.subTest(placeholder=placeholder):
                self.assertIn(placeholder, prompt)
        self.assertIn("Nie wolno ich przenieść", prompt)

    def test_without_a_domain_the_prompt_stays_unchanged(self):
        """Starsze wywołania nie przekazują domeny - nie mogą przez to dostać
        instrukcji odnoszącej się do niczego."""
        prompt = self._prompt_for()

        self.assertNotIn("Audytowana witryna to", prompt)

    def test_audit_service_passes_the_domain_to_the_generator(self):
        from auditor.services.audit_service import AuditService

        service = AuditService.__new__(AuditService)
        service.rag_engine = MagicMock()
        service.rag_engine.generate_recommendation.return_value = "rekomendacja"
        service.site_domain = "shell.pl"

        service._make_metric("seo", "title", {"note": "Brak tytułu."}, "error")

        self.assertEqual(
            service.rag_engine.generate_recommendation.call_args.kwargs["site_domain"],
            "shell.pl",
        )

    def test_domain_is_extracted_without_the_www_prefix(self):
        from auditor.services.audit_service import AuditService

        self.assertEqual(AuditService._extract_domain("https://www.shell.pl/oferta"), "shell.pl")
        self.assertEqual(AuditService._extract_domain("https://shell.pl/"), "shell.pl")
        self.assertIsNone(AuditService._extract_domain(""))


class GooglebotRetryTests(SimpleTestCase):
    """Gdy serwer oddaje audytowi powłokę, a Googlebotowi treść - audyt sięga po treść.

    Samo pominięcie testów daje raport bez fałszywych błędów, ale i bez zawartości.
    Snapshot serwowany Googlebotowi jest publicznie dostępny, więc audyt może go
    pobrać i ocenić dokładnie to, co indeksuje wyszukiwarka.
    """

    def setUp(self):
        sleep_patcher = patch("auditor.services.scraper.time.sleep")
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

        guard_patcher = patch(
            "auditor.services.scraper.validate_public_url", side_effect=lambda url: url
        )
        guard_patcher.start()
        self.addCleanup(guard_patcher.stop)

        available_patcher = patch("auditor.services.renderer.is_available", return_value=False)
        available_patcher.start()
        self.addCleanup(available_patcher.stop)

    def _scraper_with_responses(self, by_user_agent: dict):
        """Scraper, którego `_fetch_once` zwraca HTML zależny od User-Agenta."""
        from auditor.services.scraper import SEOScraper

        scraper = SEOScraper()
        calls = []

        def fetch_once(url, headers, timeout=None):
            user_agent = headers["User-Agent"]
            calls.append(user_agent)
            for fragment, html in by_user_agent.items():
                if fragment in user_agent:
                    return html
            return by_user_agent["default"]

        scraper._fetch_once = fetch_once
        return scraper, calls

    def test_stub_triggers_a_retry_with_the_googlebot_identity(self):
        from auditor.services.scraper import GOOGLEBOT_RETRY_USER_AGENT

        scraper, calls = self._scraper_with_responses(
            {"Googlebot": SSR_HTML, "default": CSR_RAW}
        )

        data = scraper.scrape("https://przyklad.pl/")

        self.assertGreater(data["word_count"], 300)
        self.assertEqual(data["h1_non_empty_count"], 1)
        self.assertTrue(data["served_to_declared_bots_only"])
        self.assertIn(GOOGLEBOT_RETRY_USER_AGENT, calls)

    def test_page_with_content_is_not_retried(self):
        """Ponowienie kosztuje żądanie - nie robimy go, gdy nie ma czego naprawiać."""
        scraper, calls = self._scraper_with_responses({"default": SSR_HTML})

        data = scraper.scrape("https://przyklad.pl/")

        self.assertEqual(len(calls), 1)
        self.assertNotIn("served_to_declared_bots_only", data)

    def test_retry_that_changes_nothing_keeps_the_original_result(self):
        """Zwykły serwis CSR oddaje powłokę każdemu - wtedy zostaje ścieżka
        renderowania, a nie podmiana wyniku na identyczny."""
        scraper, _ = self._scraper_with_responses({"default": CSR_RAW})

        data = scraper.scrape("https://przyklad.pl/")

        self.assertLess(data["word_count"], 50)
        self.assertNotIn("served_to_declared_bots_only", data)

    def test_explicit_user_agent_disables_the_retry(self):
        """Skoro ktoś narzucił tożsamość jawnie, podmiana byłaby zaskoczeniem."""
        from auditor.services.scraper import SEOScraper

        scraper = SEOScraper(user_agent="WlasnyBot/1.0")
        calls = []

        def fetch_once(url, headers, timeout=None):
            calls.append(headers["User-Agent"])
            return CSR_RAW

        scraper._fetch_once = fetch_once
        scraper.scrape("https://przyklad.pl/")

        self.assertEqual(calls, ["WlasnyBot/1.0"])

    def test_failed_retry_does_not_break_the_audit(self):
        from auditor.services.scraper import ScraperError, SEOScraper

        scraper = SEOScraper()

        def fetch_once(url, headers, timeout=None):
            if "Googlebot" in headers["User-Agent"]:
                raise ScraperError("sonda odrzucona")
            return CSR_RAW

        scraper._fetch_once = fetch_once
        data = scraper.scrape("https://przyklad.pl/")

        self.assertLess(data["word_count"], 50)

