"""Moduł agentowy: przepływ stanu i odporność na awarie pojedynczych agentów.

Założenie całej konstrukcji: badanie z trzech źródeł, z których jedno zawiodło, jest
użyteczne — brak raportu nie jest. Dlatego żaden agent nie podnosi wyjątku na
zewnątrz, a orkiestrator zawsze oddaje stan z raportem.

Ani sieć, ani OpenAI nie są tu prawdziwe.
"""
from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from django.urls import reverse

from auditor.agents.orchestrator import run_seo_investigation
from auditor.agents.specialists import (
    AnalyticsAgent,
    GeoAgent,
    ReporterAgent,
    TechnicalAgent,
    _fallback_report,
    _percent_change,
)
from auditor.agents.state import SEOInvestigatorState
from auditor.agents.tools import find_audit_for_domain, get_technical_health
from auditor.services.domains import recent_domains_for
from auditor.models import Audit, AuditMetric, GeoQuery, GeoRun, GeoStudy

User = get_user_model()


def _przesun_w_czasie(obiekt, minut_temu: int):
    """Ustawia `created_at` wprost - `auto_now_add` ignoruje wartość przy tworzeniu.

    Testy kolejności nie mogą opierać się na rozdzielczości zegara: dwa rekordy
    założone w tej samej mikrosekundzie dałyby remis, a stabilny sort rozstrzygnąłby
    go kolejnością tabel, nie czasem.
    """
    moment = timezone.now() - timedelta(minutes=minut_temu)
    type(obiekt).objects.filter(pk=obiekt.pk).update(created_at=moment)
    obiekt.refresh_from_db()
    return obiekt


class _Reporter:
    """Agent syntezy bez sieci - składa raport z ustaleń."""

    name = "ReporterAgent(test)"

    def __init__(self):
        self.calls = 0

    def run(self, state):
        self.calls += 1
        state.final_synthesis_report = _fallback_report(state)
        return state


class StateTests(SimpleTestCase):
    """Wspólny stan badania."""

    def test_fresh_state_has_no_findings(self):
        state = SEOInvestigatorState(domain="enova.pl")

        self.assertFalse(state.has_findings)
        self.assertEqual(state.errors, [])

    def test_any_list_counts_as_findings(self):
        for pole in ("technical_issues", "analytics_insights", "geo_visibility_notes"):
            with self.subTest(pole=pole):
                state = SEOInvestigatorState(domain="enova.pl")
                getattr(state, pole).append("cokolwiek")
                self.assertTrue(state.has_findings)

    def test_error_is_recorded_with_the_agent_name(self):
        state = SEOInvestigatorState(domain="enova.pl")

        state.record_error("TechnicalAgent", ValueError("coś padło"))

        self.assertEqual(state.errors, ["TechnicalAgent: ValueError: coś padło"])

    def test_error_accepts_plain_text(self):
        state = SEOInvestigatorState(domain="enova.pl")

        state.record_error("AnalyticsAgent", "Brak autoryzacji GA4/GSC.")

        self.assertEqual(state.errors, ["AnalyticsAgent: Brak autoryzacji GA4/GSC."])

    def test_as_dict_carries_everything(self):
        state = SEOInvestigatorState(domain="enova.pl", audit_id=7)
        state.technical_issues.append("Brak H1")
        state.final_synthesis_report = "# Raport"

        dane = state.as_dict()

        self.assertEqual(dane["domain"], "enova.pl")
        self.assertEqual(dane["audit_id"], 7)
        self.assertEqual(dane["technical_issues"], ["Brak H1"])
        self.assertEqual(dane["final_synthesis_report"], "# Raport")

    def test_percent_change_without_a_baseline_is_unknown(self):
        # Zero rok temu nie znaczy wzrostu nieskończonego, tylko brak odniesienia.
        self.assertIsNone(_percent_change(100, 0))
        self.assertIsNone(_percent_change(None, 50))
        self.assertEqual(_percent_change(50, 100), -50)
        self.assertEqual(_percent_change(150, 100), 50)


class ToolsTests(TestCase):
    """Narzędzia: opakowania na istniejące serwisy."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="agenci@przyklad.pl",
            email="agenci@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(
            url="https://enova.pl/", owner=cls.user, score=66
        )
        AuditMetric.objects.create(
            audit=cls.audit, category="seo", key="meta_description",
            value={"note": "Brak meta description."}, status="error",
        )
        AuditMetric.objects.create(
            audit=cls.audit, category="technical", key="image_alt",
            value={"note": "9/46 obrazków bez atrybutu ALT."}, status="warning",
        )
        AuditMetric.objects.create(
            audit=cls.audit, category="technical", key="h1_structure",
            value={"note": "H1 jest poprawny."}, status="ok",
        )
        AuditMetric.objects.create(
            audit=cls.audit, category="technical", key="schema_org",
            value={"note": "Nie dało się zbadać - pusty HTML."}, status="skipped",
        )

    def test_audit_is_found_by_domain(self):
        self.assertEqual(find_audit_for_domain("enova.pl"), self.audit)
        self.assertEqual(find_audit_for_domain("https://www.enova.pl/cennik"), self.audit)

    def test_analytics_only_records_are_not_audits(self):
        # Rekord analityczny nie ma metryk - jako źródło wniosków byłby pustką.
        Audit.objects.create(
            url="https://amso.eu/", owner=self.user, analytics_only=True
        )

        self.assertIsNone(find_audit_for_domain("amso.eu"))

    def test_health_from_the_audit_lists_problems(self):
        zdrowie = get_technical_health("enova.pl")

        self.assertEqual(zdrowie["source"], "audit")
        self.assertEqual(zdrowie["audit_id"], self.audit.pk)
        self.assertEqual(zdrowie["score"], 66)
        klucze = {p["key"] for p in zdrowie["problems"]}
        self.assertEqual(klucze, {"meta_description", "image_alt"})

    def test_skipped_metrics_are_not_problems(self):
        # `skipped` znaczy "nie dało się zbadać", a nie "jest źle" - raportowanie
        # tego jako błędu byłoby nieprawdą.
        zdrowie = get_technical_health("enova.pl")

        klucze = {p["key"] for p in zdrowie["problems"]}
        self.assertNotIn("schema_org", klucze)
        self.assertNotIn("h1_structure", klucze)

    def test_health_falls_back_to_a_live_scan(self):
        html = "<html><head><title>Sklep</title></head><body><p>krótko</p></body></html>"

        with patch("auditor.services.scraper.SEOScraper.fetch", return_value=html):
            zdrowie = get_technical_health("bez-audytu.example")

        self.assertEqual(zdrowie["source"], "scan")
        self.assertIsNone(zdrowie["audit_id"])
        klucze = {p["key"] for p in zdrowie["problems"]}
        self.assertIn("meta_description", klucze)
        self.assertIn("h1_structure", klucze)

    def test_scan_failure_is_reported_not_raised(self):
        from auditor.services.scraper import ScraperError

        with patch(
            "auditor.services.scraper.SEOScraper.fetch",
            side_effect=ScraperError("HTTP 404"),
        ):
            zdrowie = get_technical_health("nie-istnieje.example")

        self.assertIn("404", zdrowie["error"])
        self.assertEqual(zdrowie["problems"], [])


class TechnicalAgentTests(TestCase):
    """Agent techniczny przekłada metryki na zdania."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="tech@przyklad.pl",
            email="tech@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(url="https://enova.pl/", owner=cls.user, score=66)
        AuditMetric.objects.create(
            audit=cls.audit, category="seo", key="meta_description",
            value={"note": "Brak meta description."}, status="error",
        )
        AuditMetric.objects.create(
            audit=cls.audit, category="technical", key="image_alt",
            value={"note": "9/46 obrazków bez ALT."}, status="warning",
        )

    def test_findings_land_in_the_state(self):
        state = SEOInvestigatorState(domain="enova.pl")

        TechnicalAgent().run(state)

        self.assertEqual(state.audit_id, self.audit.pk)
        self.assertTrue(any("66/100" in w for w in state.technical_issues))
        self.assertTrue(any("Brak meta description" in w for w in state.technical_issues))

    def test_errors_come_before_warnings(self):
        state = SEOInvestigatorState(domain="enova.pl")

        TechnicalAgent().run(state)

        wnioski = [w for w in state.technical_issues if w.startswith(("Błąd", "Ostrzeżenie"))]
        self.assertTrue(wnioski[0].startswith("Błąd"))

    def test_clean_audit_says_so_explicitly(self):
        AuditMetric.objects.filter(audit=self.audit).delete()
        state = SEOInvestigatorState(domain="enova.pl")

        TechnicalAgent().run(state)

        self.assertTrue(any("Nie znaleziono błędów" in w for w in state.technical_issues))

    def test_tool_failure_becomes_a_state_error(self):
        state = SEOInvestigatorState(domain="enova.pl")

        with patch(
            "auditor.agents.specialists.get_technical_health",
            side_effect=RuntimeError("baza padła"),
        ):
            TechnicalAgent().run(state)

        self.assertEqual(state.technical_issues, [])
        self.assertTrue(any("TechnicalAgent" in e for e in state.errors))


class AnalyticsAgentTests(TestCase):
    """Agent analityczny i brak autoryzacji Google."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="analit@przyklad.pl",
            email="analit@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        Audit.objects.create(url="https://enova.pl/", owner=cls.user)

    def test_missing_authorization_is_not_a_crash(self):
        state = SEOInvestigatorState(domain="enova.pl")

        with patch(
            "auditor.agents.specialists.get_traffic_trends",
            return_value={"authorized": False, "ga4": None, "gsc": None, "error": ""},
        ):
            AnalyticsAgent().run(state)

        self.assertTrue(any("Brak autoryzacji GA4/GSC" in w for w in state.analytics_insights))
        # Zapisujemy to również jako ograniczenie, żeby raport nie udawał kompletnego.
        self.assertTrue(any("AnalyticsAgent" in e for e in state.errors))

    def test_traffic_drop_is_described(self):
        trendy = {
            "authorized": True,
            "ga4": {
                "property_id": "1",
                "sessions": 14284,
                "window_days": 30,
                "channels": {
                    "current": {"Organic Search": 14257},
                    "previous": {"Organic Search": 38756},
                },
                "error": "",
            },
            "gsc": None,
            "error": "",
        }
        state = SEOInvestigatorState(domain="enova.pl")

        with patch("auditor.agents.specialists.get_traffic_trends", return_value=trendy):
            AnalyticsAgent().run(state)

        self.assertTrue(any("14284" in w for w in state.analytics_insights))
        spadek = [w for w in state.analytics_insights if "spadek" in w]
        self.assertTrue(spadek)
        self.assertIn("63%", spadek[0])
        self.assertIn("istotny", spadek[0])

    def test_small_change_is_marked_as_insignificant(self):
        trendy = {
            "authorized": True,
            "ga4": {
                "property_id": "1", "sessions": 105, "window_days": 30,
                "channels": {"current": {"Organic Search": 105},
                             "previous": {"Organic Search": 100}},
                "error": "",
            },
            "gsc": None, "error": "",
        }
        state = SEOInvestigatorState(domain="enova.pl")

        with patch("auditor.agents.specialists.get_traffic_trends", return_value=trendy):
            AnalyticsAgent().run(state)

        self.assertTrue(any("nieznaczny" in w for w in state.analytics_insights))

    def test_search_console_queries_are_reported(self):
        trendy = {
            "authorized": True,
            "ga4": None,
            "gsc": {
                "site_url": "sc-domain:enova.pl",
                "clicks_current": 15353,
                "clicks_previous": 12000,
                "yoy_change_percent": 27.9,
                "gainers": [{"query": "enova365"}, {"query": "enova"}],
                "losers": [{"query": "program kadrowy"}],
                "error": "",
            },
            "error": "",
        }
        state = SEOInvestigatorState(domain="enova.pl")

        with patch("auditor.agents.specialists.get_traffic_trends", return_value=trendy):
            AnalyticsAgent().run(state)

        tekst = " ".join(state.analytics_insights)
        self.assertIn("15353", tekst)
        self.assertIn("enova365", tekst)
        self.assertIn("program kadrowy", tekst)
        self.assertIn("wzrost o 27.9%", tekst)

    def test_partial_api_failure_is_recorded(self):
        trendy = {
            "authorized": True,
            "ga4": {"error": "ServiceUnavailable: 503"},
            "gsc": None,
            "error": "",
        }
        state = SEOInvestigatorState(domain="enova.pl")

        with patch("auditor.agents.specialists.get_traffic_trends", return_value=trendy):
            AnalyticsAgent().run(state)

        self.assertTrue(any("GA4: ServiceUnavailable" in e for e in state.errors))


class GeoAgentTests(TestCase):
    """Agent GEO korzysta z ostatniego pomiaru, nie zleca nowego."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="geo-agent@przyklad.pl",
            email="geo-agent@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def test_unmeasured_domain_says_so(self):
        state = SEOInvestigatorState(domain="enova.pl")

        GeoAgent().run(state)

        self.assertTrue(any("nie była dotąd mierzona" in w for w in state.geo_visibility_notes))

    def test_existing_study_is_summarised(self):
        study = GeoStudy.objects.create(
            owner=self.user, domain="enova.pl", brand_name="Enova",
            status=GeoStudy.Status.COMPLETED, overall_score=52, repetitions=2,
        )
        query = GeoQuery.objects.create(study=study, text="Jaki program kadrowy?", position=1)
        GeoRun.objects.create(
            query=query, attempt=1, answer="Polecam Enova.",
            citations=[{"url": "https://enova.pl/", "domain": "enova.pl", "position": 1}],
            brand_cited=True, brand_mentioned=True, brand_position=1,
            visibility=GeoRun.Visibility.LINKED_CITATION,
        )
        GeoRun.objects.create(
            query=query, attempt=2, answer="Polecam Comarch.",
            citations=[{"url": "https://comarch.pl/", "domain": "comarch.pl", "position": 1}],
            brand_cited=False, brand_mentioned=False,
            visibility=GeoRun.Visibility.ABSENT,
        )
        GeoQuery.objects.filter(pk=query.pk).update(
            competitors=[{"domain": "comarch.pl", "count": 1}]
        )

        state = SEOInvestigatorState(domain="enova.pl")
        GeoAgent().run(state)

        tekst = " ".join(state.geo_visibility_notes)
        self.assertIn("Widoczność w wyszukiwarkach AI", tekst)
        self.assertIn("comarch.pl", tekst)

    def test_tool_failure_becomes_a_state_error(self):
        state = SEOInvestigatorState(domain="enova.pl")

        with patch(
            "auditor.agents.specialists.get_geo_visibility",
            side_effect=RuntimeError("baza padła"),
        ):
            GeoAgent().run(state)

        self.assertTrue(any("GeoAgent" in e for e in state.errors))


class ReporterAgentTests(SimpleTestCase):
    """Agent syntezy i jego zachowanie bez modelu."""

    def _state_with_findings(self) -> SEOInvestigatorState:
        state = SEOInvestigatorState(domain="enova.pl")
        state.technical_issues.append("Błąd: Brak meta description.")
        state.analytics_insights.append("Ruch organiczny: spadek o 63%.")
        return state

    def test_llm_output_becomes_the_report(self):
        client = MagicMock()
        client.responses.create.return_value = MagicMock(output_text="## Podsumowanie\nTreść.")
        state = self._state_with_findings()

        ReporterAgent(client=client).run(state)

        self.assertIn("## Podsumowanie", state.final_synthesis_report)

    def test_prompt_carries_every_finding(self):
        client = MagicMock()
        client.responses.create.return_value = MagicMock(output_text="raport")
        state = self._state_with_findings()
        state.geo_visibility_notes.append("Widoczność w AI: 52%.")
        state.errors.append("AnalyticsAgent: Brak autoryzacji GA4/GSC.")

        ReporterAgent(client=client).run(state)

        prompt = client.responses.create.call_args.kwargs["input"]
        self.assertIn("Brak meta description", prompt)
        self.assertIn("spadek o 63%", prompt)
        self.assertIn("Widoczność w AI: 52%", prompt)
        self.assertIn("Brak autoryzacji", prompt)
        self.assertIn("enova.pl", prompt)

    def test_llm_failure_leaves_a_report_built_from_findings(self):
        client = MagicMock()
        client.responses.create.side_effect = RuntimeError("OpenAI nie odpowiada")
        state = self._state_with_findings()

        ReporterAgent(client=client).run(state)

        # Badanie nie może kończyć się pustką tylko dlatego, że jeden krok wymagał sieci.
        self.assertIn("Brak meta description", state.final_synthesis_report)
        self.assertTrue(any("ReporterAgent" in e for e in state.errors))

    def test_empty_llm_answer_falls_back_too(self):
        client = MagicMock()
        client.responses.create.return_value = MagicMock(output_text="   ")
        state = self._state_with_findings()

        ReporterAgent(client=client).run(state)

        self.assertIn("Ustalenia techniczne", state.final_synthesis_report)

    def test_no_findings_means_no_llm_call(self):
        client = MagicMock()
        state = SEOInvestigatorState(domain="enova.pl")

        ReporterAgent(client=client).run(state)

        client.responses.create.assert_not_called()
        self.assertIn("Nie udało się zebrać", state.final_synthesis_report)


class OrchestratorTests(TestCase):
    """Przepływ od inicjalizacji do syntezy."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="orkiestra@przyklad.pl",
            email="orkiestra@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(url="https://enova.pl/", owner=cls.user, score=66)
        AuditMetric.objects.create(
            audit=cls.audit, category="seo", key="meta_description",
            value={"note": "Brak meta description."}, status="error",
        )

    def _run(self, **kwargs):
        reporter = kwargs.pop("reporter", None) or _Reporter()
        with patch(
            "auditor.agents.specialists.get_traffic_trends",
            return_value={"authorized": False, "ga4": None, "gsc": None, "error": ""},
        ):
            state = run_seo_investigation(
                "enova.pl", owner=self.user, reporter=reporter, **kwargs
            )
        return state, reporter

    def test_full_flow_fills_the_state(self):
        state, reporter = self._run()

        self.assertEqual(state.domain, "enova.pl")
        self.assertEqual(state.audit_id, self.audit.pk)
        self.assertTrue(state.technical_issues)
        self.assertTrue(state.analytics_insights)
        self.assertTrue(state.geo_visibility_notes)
        self.assertTrue(state.final_synthesis_report)
        self.assertEqual(reporter.calls, 1)

    def test_url_is_reduced_to_a_domain(self):
        state, _ = self._run()

        self.assertEqual(state.domain, "enova.pl")

        with patch(
            "auditor.agents.specialists.get_traffic_trends",
            return_value={"authorized": False, "ga4": None, "gsc": None, "error": ""},
        ):
            inny = run_seo_investigation(
                "https://www.enova.pl/cennik", owner=self.user, reporter=_Reporter()
            )
        self.assertEqual(inny.domain, "enova.pl")

    def test_missing_domain_is_refused_without_crashing(self):
        state = run_seo_investigation("", reporter=_Reporter())

        self.assertEqual(state.domain, "")
        self.assertTrue(any("Nie podano domeny" in e for e in state.errors))

    def test_default_run_is_sequential_and_complete(self):
        # Domyślny tryb musi zwracać komplet. Równoległy gubi ustalenia na SQLite
        # ("database table is locked"), dlatego nie jest domyślny - patrz docstring
        # modułu orkiestratora.
        state, _ = self._run()

        self.assertTrue(state.technical_issues)
        self.assertEqual(state.audit_id, self.audit.pk)

    def test_parallel_mode_never_crashes_the_run(self):
        # Na SQLite wątki rywalizują o tabelę, więc część ustaleń może przepaść -
        # ale badanie kończy się stanem i raportem, nie wyjątkiem.
        state, reporter = self._run(parallel=True)

        self.assertEqual(state.domain, "enova.pl")
        self.assertTrue(state.final_synthesis_report)
        self.assertEqual(reporter.calls, 1)

    def test_one_agent_crashing_does_not_stop_the_rest(self):
        reporter = _Reporter()

        with patch(
            "auditor.agents.specialists.get_technical_health",
            side_effect=RuntimeError("skaner padł"),
        ), patch(
            "auditor.agents.specialists.get_traffic_trends",
            return_value={"authorized": False, "ga4": None, "gsc": None, "error": ""},
        ):
            state = run_seo_investigation("enova.pl", owner=self.user, reporter=reporter)

        self.assertEqual(state.technical_issues, [])
        # Pozostali agenci dokończyli pracę, a raport powstał.
        self.assertTrue(state.analytics_insights)
        self.assertTrue(state.geo_visibility_notes)
        self.assertTrue(state.final_synthesis_report)
        self.assertEqual(reporter.calls, 1)
        self.assertTrue(any("TechnicalAgent" in e for e in state.errors))

    def test_every_agent_crashing_still_returns_a_state(self):
        reporter = _Reporter()

        with patch(
            "auditor.agents.specialists.get_technical_health", side_effect=RuntimeError("a")
        ), patch(
            "auditor.agents.specialists.get_traffic_trends", side_effect=RuntimeError("b")
        ), patch(
            "auditor.agents.specialists.get_geo_visibility", side_effect=RuntimeError("c")
        ):
            state = run_seo_investigation("enova.pl", owner=self.user, reporter=reporter)

        self.assertFalse(state.has_findings)
        # Trzech zbieraczy; testowy agent syntezy nie dokłada własnego błędu.
        self.assertEqual(len(state.errors), 3)
        self.assertIn("Nie udało się zebrać", state.final_synthesis_report)

    def test_progress_is_reported_for_each_agent(self):
        wywolania = []

        with patch(
            "auditor.agents.specialists.get_traffic_trends",
            return_value={"authorized": False, "ga4": None, "gsc": None, "error": ""},
        ):
            run_seo_investigation(
                "enova.pl",
                owner=self.user,
                reporter=_Reporter(),
                on_progress=lambda nazwa, state: wywolania.append(nazwa),
            )

        self.assertEqual(len(wywolania), 4)  # trzech zbieraczy + synteza

    def test_other_users_audit_is_invisible(self):
        obcy = User.objects.create_user(
            username="obcy6@przyklad.pl",
            email="obcy6@przyklad.pl",
            password="haslo-kontrolne-2",
        )

        with patch(
            "auditor.agents.specialists.get_traffic_trends",
            return_value={"authorized": False, "ga4": None, "gsc": None, "error": ""},
        ), patch("auditor.services.scraper.SEOScraper.fetch", return_value="<html></html>"):
            state = run_seo_investigation("enova.pl", owner=obcy, reporter=_Reporter())

        # Cudzy audyt nie może trafić do badania - agent sięga po świeży skan.
        self.assertIsNone(state.audit_id)


class CommandTests(TestCase):
    """Komenda CLI."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="cli@przyklad.pl",
            email="cli@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(url="https://enova.pl/", owner=cls.user, score=66)
        AuditMetric.objects.create(
            audit=cls.audit, category="seo", key="meta_description",
            value={"note": "Brak meta description."}, status="error",
        )

    def test_command_prints_findings_and_report(self):
        wyjscie = StringIO()

        with patch(
            "auditor.agents.specialists.get_traffic_trends",
            return_value={"authorized": False, "ga4": None, "gsc": None, "error": ""},
        ):
            call_command("run_investigator", "--domain", "enova.pl", "--no-llm", stdout=wyjscie)

        tekst = wyjscie.getvalue()
        self.assertIn("Badanie domeny: enova.pl", tekst)
        self.assertIn("ZEBRANE USTALENIA", tekst)
        self.assertIn("Brak meta description", tekst)
        self.assertIn("RAPORT", tekst)

    def test_command_reports_progress(self):
        wyjscie = StringIO()

        with patch(
            "auditor.agents.specialists.get_traffic_trends",
            return_value={"authorized": False, "ga4": None, "gsc": None, "error": ""},
        ):
            call_command("run_investigator", "--domain", "enova.pl", "--no-llm", stdout=wyjscie)

        tekst = wyjscie.getvalue()
        self.assertIn("[TechnicalAgent]", tekst)
        self.assertIn("[AnalyticsAgent]", tekst)

    def test_owner_filter_rejects_unknown_login(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command(
                "run_investigator", "--domain", "enova.pl", "--owner", "nie-ma-takiego",
                stdout=StringIO(),
            )

    def test_no_llm_skips_the_model(self):
        wyjscie = StringIO()

        with patch("auditor.agents.specialists._llm_client") as klient, patch(
            "auditor.agents.specialists.get_traffic_trends",
            return_value={"authorized": False, "ga4": None, "gsc": None, "error": ""},
        ):
            call_command("run_investigator", "--domain", "enova.pl", "--no-llm", stdout=wyjscie)

        klient.assert_not_called()


class SourceTrackingTests(TestCase):
    """Stan zapamiętuje, które źródła wniosły dane - stąd odznaki w interfejsie."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="zrodla@przyklad.pl",
            email="zrodla@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(url="https://enova.pl/", owner=cls.user, score=66)
        AuditMetric.objects.create(
            audit=cls.audit, category="seo", key="meta_description",
            value={"note": "Brak meta description."}, status="error",
        )

    def test_technical_source_is_marked_available(self):
        state = SEOInvestigatorState(domain="enova.pl")

        TechnicalAgent().run(state, owner=self.user)

        self.assertTrue(state.has_source("technical"))

    def test_failed_agent_leaves_its_source_unavailable(self):
        state = SEOInvestigatorState(domain="enova.pl")

        with patch(
            "auditor.agents.specialists.get_technical_health",
            side_effect=RuntimeError("skaner padł"),
        ):
            TechnicalAgent().run(state, owner=self.user)

        self.assertFalse(state.has_source("technical"))

    def test_unauthorized_analytics_is_not_a_source(self):
        # Agent wpisuje do ustaleń zdanie o braku autoryzacji, więc niepusta lista
        # nie znaczy, że źródło było dostępne - stąd osobne pole `sources`.
        state = SEOInvestigatorState(domain="enova.pl")

        with patch(
            "auditor.agents.specialists.get_traffic_trends",
            return_value={"authorized": False, "ga4": None, "gsc": None, "error": ""},
        ):
            AnalyticsAgent().run(state, owner=self.user)

        self.assertTrue(state.analytics_insights)
        self.assertFalse(state.has_source("analytics"))

    def test_authorized_analytics_with_data_is_a_source(self):
        trendy = {
            "authorized": True,
            "ga4": {"property_id": "1", "sessions": 100, "window_days": 30,
                    "channels": {}, "error": ""},
            "gsc": None,
            "error": "",
        }
        state = SEOInvestigatorState(domain="enova.pl")

        with patch("auditor.agents.specialists.get_traffic_trends", return_value=trendy):
            AnalyticsAgent().run(state, owner=self.user)

        self.assertTrue(state.has_source("analytics"))

    def test_unmeasured_geo_is_not_a_source(self):
        state = SEOInvestigatorState(domain="enova.pl")

        GeoAgent().run(state, owner=self.user)

        self.assertTrue(state.geo_visibility_notes)
        self.assertFalse(state.has_source("geo"))

    def test_source_count_reflects_available_sources(self):
        state = SEOInvestigatorState(domain="enova.pl")
        state.mark_source("technical", True)
        state.mark_source("analytics", True)
        state.mark_source("geo", False)

        self.assertEqual(state.source_count, 2)


class InvestigateViewTests(TestCase):
    """Widok `/audits/<pk>/investigate/`."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="widok@przyklad.pl",
            email="widok@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(
            url="https://www.enova.pl/cennik", owner=cls.user, score=66,
            status=Audit.Status.COMPLETED,
        )
        AuditMetric.objects.create(
            audit=cls.audit, category="seo", key="meta_description",
            value={"note": "Brak meta description."}, status="error",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _open(self, authorized=True):
        """Otwiera syntezę z atrapami: analityka dostępna, GEO nie - czyli 2 z 3."""
        trendy = {
            "authorized": authorized,
            "ga4": {
                "property_id": "316375346",
                "sessions": 14284,
                "window_days": 30,
                "channels": {"current": {"Organic Search": 14257},
                             "previous": {"Organic Search": 38756}},
                "error": "",
            },
            "gsc": None,
            "error": "",
        }
        if not authorized:
            trendy = {"authorized": False, "ga4": None, "gsc": None, "error": ""}

        with patch("auditor.agents.specialists.get_traffic_trends", return_value=trendy), patch(
            "auditor.agents.specialists.ReporterAgent.run",
            side_effect=lambda state: _Reporter().run(state),
        ):
            return self.client.get(reverse("auditor:investigate", args=[self.audit.pk]))

    def test_view_returns_200_with_two_of_three_sources(self):
        response = self._open()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/audit_investigation.html")
        state = response.context["state"]
        self.assertEqual(state.source_count, 2)
        self.assertTrue(state.has_source("technical"))
        self.assertTrue(state.has_source("analytics"))
        self.assertFalse(state.has_source("geo"))

    def test_report_is_rendered(self):
        response = self._open()

        self.assertTrue(response.context["report_html"])
        self.assertContains(response, "Brak meta description")
        self.assertContains(response, "14284")

    def test_badges_show_which_sources_were_used(self):
        response = self._open()
        html = response.content.decode()

        self.assertIn("Audyt Techniczny", html)
        self.assertIn("Analityka GA4/GSC", html)
        self.assertIn("Widoczność w AI (GEO)", html)
        # Dwie odznaki włączone, jedna wyłączona. Liczymy pełny atrybut `class`,
        # bo same nazwy klas występują też w arkuszu stylów w base.html.
        self.assertEqual(html.count('"source-badge source-badge-on"'), 2)
        self.assertEqual(html.count('"source-badge source-badge-off"'), 1)

    def test_header_states_how_many_sources(self):
        response = self._open()

        self.assertContains(response, "źródła: 2 z 3")

    def test_limitations_section_appears_when_something_failed(self):
        response = self._open(authorized=False)

        self.assertContains(response, "Ewentualne ograniczenia badania")
        self.assertContains(response, "Brak autoryzacji")

    def test_single_source_still_renders(self):
        # Jedno źródło to nadal użyteczna synteza - raport powstaje.
        response = self._open(authorized=False)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["state"].source_count, 1)
        self.assertTrue(response.context["report_html"])

    def test_domain_is_taken_from_the_audit_url(self):
        # Model trzyma pełny adres; agenci pracują na domenie, bo analityka i GEO
        # opisują cały serwis, nie jedną podstronę.
        response = self._open()

        self.assertEqual(response.context["state"].domain, "enova.pl")

    def test_back_link_returns_to_the_report(self):
        response = self._open()

        self.assertContains(response, f'href="/audits/{self.audit.pk}/"')
        self.assertContains(response, "Powrót do raportu technicznego")

    def test_findings_are_listed_under_the_report(self):
        html = self._open().content.decode()

        self.assertIn("Zebrane ustalenia", html)
        self.assertIn("Techniczne (", html)

    def test_foreign_audit_returns_404(self):
        obcy = User.objects.create_user(
            username="obcy7@przyklad.pl",
            email="obcy7@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        cudzy = Audit.objects.create(url="https://cudzy.pl/", owner=obcy)

        response = self.client.get(reverse("auditor:investigate", args=[cudzy.pk]))

        self.assertEqual(response.status_code, 404)

    def test_view_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse("auditor:investigate", args=[self.audit.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)


class InvestigateButtonTests(TestCase):
    """Przycisk w raporcie audytu."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="przycisk@przyklad.pl",
            email="przycisk@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.gotowy = Audit.objects.create(
            url="https://enova.pl/", owner=cls.user, status=Audit.Status.COMPLETED, score=66
        )
        cls.w_toku = Audit.objects.create(
            url="https://inna.pl/", owner=cls.user, status=Audit.Status.PROCESSING
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_button_is_present_in_a_finished_report(self):
        html = self.client.get(reverse("auditor:detail", args=[self.gotowy.pk])).content.decode()

        self.assertIn("🤖 Podsumuj dane witryny", html)
        self.assertIn(f'href="/audits/{self.gotowy.pk}/investigate/"', html)

    def test_button_links_to_its_own_audit(self):
        html = self.client.get(reverse("auditor:detail", args=[self.gotowy.pk])).content.decode()

        self.assertNotIn(f'href="/audits/{self.w_toku.pk}/investigate/"', html)

    def test_unfinished_audit_has_no_button(self):
        # Bez metryk synteza nie miałaby z czego powstać.
        html = self.client.get(reverse("auditor:detail", args=[self.w_toku.pk])).content.decode()

        self.assertNotIn("Podsumuj dane witryny", html)

    def test_loading_state_is_wired_up(self):
        html = self.client.get(reverse("auditor:detail", args=[self.gotowy.pk])).content.decode()

        self.assertIn('id="investigate-link"', html)
        self.assertIn("Agenci badają domenę", html)


class RecentDomainsTests(TestCase):
    """Lista ostatnich domen - wspólna dla trzech narzędzi."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="domeny@przyklad.pl",
            email="domeny@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.obcy = User.objects.create_user(
            username="obcy9@przyklad.pl",
            email="obcy9@przyklad.pl",
            password="haslo-kontrolne-2",
        )

    def test_empty_without_any_research(self):
        self.assertEqual(recent_domains_for(self.user), [])

    def test_anonymous_user_gets_nothing(self):
        self.assertEqual(recent_domains_for(AnonymousUser()), [])
        self.assertEqual(recent_domains_for(None), [])

    def test_audit_url_is_reduced_to_a_domain(self):
        Audit.objects.create(url="https://www.enova.pl/cennik", owner=self.user)

        self.assertEqual(
            recent_domains_for(self.user), [{"domain": "enova.pl", "sources": ["audyt"]}]
        )

    def test_same_domain_from_two_audits_appears_once(self):
        Audit.objects.create(url="https://enova.pl/", owner=self.user)
        Audit.objects.create(url="https://enova.pl/kontakt", owner=self.user)

        self.assertEqual([wpis["domain"] for wpis in recent_domains_for(self.user)], ["enova.pl"])

    def test_analytics_record_is_labelled_separately(self):
        # Rekordy analityczne i skany leżą w tej samej tabeli - różni je flaga.
        Audit.objects.create(url="https://enova.pl/", owner=self.user, analytics_only=True)

        self.assertEqual(recent_domains_for(self.user), [
            {"domain": "enova.pl", "sources": ["analityka"]},
        ])

    def test_geo_study_counts_as_research(self):
        GeoStudy.objects.create(domain="enova.pl", owner=self.user)

        self.assertEqual(recent_domains_for(self.user), [
            {"domain": "enova.pl", "sources": ["GEO"]},
        ])

    def test_all_three_sources_merge_into_one_entry(self):
        Audit.objects.create(url="https://enova.pl/", owner=self.user)
        Audit.objects.create(url="https://enova.pl/", owner=self.user, analytics_only=True)
        GeoStudy.objects.create(domain="enova.pl", owner=self.user)

        self.assertEqual(recent_domains_for(self.user), [
            {"domain": "enova.pl", "sources": ["audyt", "analityka", "GEO"]},
        ])

    def test_labels_keep_a_stable_order(self):
        # Kolejność etykiet nie może zależeć od tego, które badanie było świeższe.
        GeoStudy.objects.create(domain="enova.pl", owner=self.user)
        Audit.objects.create(url="https://enova.pl/", owner=self.user)

        self.assertEqual(recent_domains_for(self.user)[0]["sources"], ["audyt", "GEO"])

    def test_newest_activity_comes_first_across_modules(self):
        # Badanie GEO jest nowsze od audytu, więc jego domena jest wyżej - inaczej
        # domena znana tylko z GEO spadłaby pod stare skany.
        _przesun_w_czasie(Audit.objects.create(url="https://stara.pl/", owner=self.user), 60)
        _przesun_w_czasie(GeoStudy.objects.create(domain="nowa.pl", owner=self.user), 1)

        self.assertEqual(
            [wpis["domain"] for wpis in recent_domains_for(self.user)], ["nowa.pl", "stara.pl"]
        )

    def test_other_users_domains_are_invisible(self):
        Audit.objects.create(url="https://cudza.pl/", owner=self.obcy)
        GeoStudy.objects.create(domain="cudza-geo.pl", owner=self.obcy)

        self.assertEqual(recent_domains_for(self.user), [])

    def test_list_is_capped_at_ten(self):
        for numer in range(14):
            Audit.objects.create(url=f"https://sklep{numer}.pl/", owner=self.user)

        self.assertEqual(len(recent_domains_for(self.user)), 10)

    def test_cap_keeps_the_newest(self):
        for numer in range(12):
            _przesun_w_czasie(
                Audit.objects.create(url=f"https://sklep{numer}.pl/", owner=self.user),
                12 - numer,
            )

        domeny = [wpis["domain"] for wpis in recent_domains_for(self.user)]

        self.assertEqual(domeny[0], "sklep11.pl")
        self.assertNotIn("sklep0.pl", domeny)


class HubInvestigateSectionTests(TestCase):
    """Sekcja "Holistyczne podsumowanie AI" na ekranie głównym."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="hub@przyklad.pl",
            email="hub@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_section_is_rendered(self):
        response = self.client.get(reverse("auditor:hub"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Holistyczne podsumowanie AI")
        self.assertContains(response, "3 w 1")
        self.assertContains(
            response,
            "Połącz dane techniczne, analityczne i widoczność w AI dla swojej witryny.",
        )

    def test_form_points_at_the_domain_route(self):
        response = self.client.get(reverse("auditor:hub"))

        self.assertContains(response, 'action="/investigate/"')
        self.assertContains(response, 'name="custom"')

    def test_recent_domains_land_in_the_context(self):
        _przesun_w_czasie(Audit.objects.create(url="https://enova.pl/", owner=self.user), 60)
        _przesun_w_czasie(GeoStudy.objects.create(domain="inna.pl", owner=self.user), 1)

        response = self.client.get(reverse("auditor:hub"))

        self.assertEqual(
            [wpis["domain"] for wpis in response.context["recent_domains"]],
            ["inna.pl", "enova.pl"],
        )

    def test_dropdown_lists_domains_with_their_sources(self):
        Audit.objects.create(url="https://enova.pl/", owner=self.user)

        response = self.client.get(reverse("auditor:hub"))

        self.assertContains(response, 'name="domain"')
        self.assertContains(response, '<option value="enova.pl">enova.pl · audyt</option>', html=True)

    def test_without_history_there_is_no_dropdown(self):
        # Pusta lista byłaby polem, z którego nie da się nic wybrać.
        response = self.client.get(reverse("auditor:hub"))

        self.assertNotContains(response, 'name="domain"')
        self.assertContains(response, "required")
        self.assertContains(response, "Nie masz jeszcze żadnych badań")

    def test_tool_tiles_still_render(self):
        response = self.client.get(reverse("auditor:hub"))

        self.assertContains(response, "Audyt Techniczny")
        self.assertContains(response, "Analityka i Ruch")
        self.assertContains(response, "Widoczność w AI")

    def test_hub_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse("auditor:hub"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)


class InvestigateDomainViewTests(TestCase):
    """`/investigate/?domain=` - synteza bez pośrednictwa audytu."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="synteza@przyklad.pl",
            email="synteza@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(
            url="https://www.enova.pl/cennik", owner=cls.user, score=66,
            status=Audit.Status.COMPLETED,
        )
        AuditMetric.objects.create(
            audit=cls.audit, category="seo", key="meta_description",
            value={"note": "Brak meta description."}, status="error",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _open(self, **params):
        brak_analityki = {"authorized": False, "ga4": None, "gsc": None, "error": ""}
        with patch(
            "auditor.agents.specialists.get_traffic_trends", return_value=brak_analityki
        ), patch(
            "auditor.agents.specialists.ReporterAgent.run",
            side_effect=lambda state: _Reporter().run(state),
        ):
            return self.client.get(reverse("auditor:investigate_domain"), params)

    def test_selected_domain_is_investigated(self):
        response = self._open(domain="enova.pl")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/audit_investigation.html")
        self.assertEqual(response.context["state"].domain, "enova.pl")

    def test_data_of_an_existing_audit_is_reused(self):
        # Wejście z hubu ma trafiać w te same dane co wejście z raportu - inaczej
        # użytkownik dostawałby dwa różne podsumowania tej samej witryny.
        response = self._open(domain="enova.pl")

        self.assertTrue(response.context["state"].has_source("technical"))
        self.assertContains(response, "Brak meta description")

    def test_typed_address_is_reduced_to_a_domain(self):
        response = self._open(custom="https://www.enova.pl/cennik?x=1")

        self.assertEqual(response.context["state"].domain, "enova.pl")

    def test_typed_address_wins_over_the_dropdown(self):
        Audit.objects.create(url="https://inna.pl/", owner=self.user)

        response = self._open(domain="inna.pl", custom="enova.pl")

        self.assertEqual(response.context["state"].domain, "enova.pl")

    def test_back_link_goes_to_the_hub_without_an_audit(self):
        response = self._open(domain="enova.pl")

        self.assertIsNone(response.context["audit"])
        self.assertContains(response, "Powrót do ekranu głównego")
        self.assertNotContains(response, "Powrót do raportu technicznego")

    def test_empty_domain_returns_to_the_hub(self):
        response = self.client.get(reverse("auditor:investigate_domain"))

        self.assertRedirects(response, reverse("auditor:hub"))

    def test_address_without_a_dot_is_rejected(self):
        # "localhost" nie jest adresem witryny, a agent próbowałby go pobrać.
        response = self.client.get(reverse("auditor:investigate_domain"), {"custom": "localhost"})

        self.assertRedirects(response, reverse("auditor:hub"))

    def test_rejection_explains_itself(self):
        response = self.client.get(
            reverse("auditor:investigate_domain"), {"custom": "localhost"}, follow=True
        )

        self.assertContains(response, "Podaj adres witryny")

    def test_other_users_data_is_not_reachable_by_domain(self):
        # Domena jest publiczna, więc samo jej wpisanie nie może odsłonić cudzych
        # audytów - izolacja musi działać po właścicielu, nie po adresie.
        obcy = User.objects.create_user(
            username="obcy11@przyklad.pl",
            email="obcy11@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        cudzy = Audit.objects.create(
            url="https://cudza.pl/", owner=obcy, score=12, status=Audit.Status.COMPLETED
        )
        AuditMetric.objects.create(
            audit=cudzy, category="seo", key="title",
            value={"note": "Sekret z cudzego audytu."}, status="error",
        )

        with patch(
            "auditor.agents.tools._scan_technical_health",
            return_value={"source": "scan", "audit_id": None, "score": None,
                          "problems": [], "error": "pominięto skan w teście"},
        ):
            response = self._open(domain="cudza.pl")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Sekret z cudzego audytu")

    def test_view_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse("auditor:investigate_domain"), {"domain": "enova.pl"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)
