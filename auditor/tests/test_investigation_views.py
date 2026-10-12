"""Widoki badania multi-agentowego: status w JSON i raport odtworzony z bazy.

Agenci nie są tu uruchamiani. Rekordy `InvestigationRun` zakładamy wprost, bo
przedmiotem testów jest to, co widok robi z JUŻ zapisanym stanem - przepływ Celery
sprawdza `test_investigation_tasks`.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone as dtz

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auditor.agents.state import SEOInvestigatorState
from auditor.models import InvestigationRun

User = get_user_model()


def _stan(**nadpisania) -> dict:
    """Zapisany stan zakończonego badania - dwa źródła z trzech."""
    state = SEOInvestigatorState(domain="enova.pl")
    state.technical_issues.append("Błąd: brak meta description.")
    state.analytics_insights.append("Sesje organiczne w ostatnich 30 dniach: 14284.")
    state.geo_visibility_notes.append("Nie mierzono widoczności w AI.")
    state.mark_source("technical", True)
    state.mark_source("analytics", True)
    state.mark_source("geo", False)
    state.metrics.update({
        "technical_score": 66,
        "technical_errors": 1,
        "technical_warnings": 0,
        "organic_sessions": 14284,
        "organic_change_percent": -63,
    })
    state.data_timestamps["technical"] = date(2026, 9, 10)
    state.data_timestamps["analytics"] = datetime(2026, 9, 12, 8, 30, tzinfo=dtz.utc)
    state.final_synthesis_report = (
        "## Główne Wnioski (Executive Summary)\n"
        "Ruch spadł o 63%, głównie na frazie enova cennik.\n\n"
        "## Co wymaga uwagi\n- Brak meta description.\n"
    )

    dane = state.as_dict()
    dane.update(nadpisania)
    return dane


class InvestigationStatusViewTests(TestCase):
    """Endpoint `/investigate/<pk>/status/`."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="status@przyklad.pl",
            email="status@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _run(self, **pola):
        pola.setdefault("domain", "enova.pl")
        pola.setdefault("owner", self.user)
        return InvestigationRun.objects.create(**pola)

    def _json(self, run):
        response = self.client.get(
            reverse("auditor:investigation_status", args=[run.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        return json.loads(response.content)

    def test_pending_run_reports_pending(self):
        dane = self._json(self._run(status=InvestigationRun.Status.PENDING))

        self.assertEqual(dane["status"], "PENDING")
        self.assertFalse(dane["finished"])

    def test_running_study_reports_processing(self):
        dane = self._json(self._run(status=InvestigationRun.Status.PROCESSING))

        self.assertEqual(dane["status"], "PROCESSING")
        self.assertFalse(dane["finished"])

    def test_complete_result_is_success(self):
        run = self._run(
            status=InvestigationRun.Status.COMPLETED,
            state=_stan(
                sources={"technical": True, "analytics": True, "geo": True}, errors=[]
            ),
        )

        self.assertEqual(self._json(run)["status"], "SUCCESS")

    def test_missing_source_makes_the_result_degraded(self):
        # Badanie udało się technicznie, ale raport opiera się na węższych danych -
        # użytkownik musi to wiedzieć, więc to nie jest zwykły sukces.
        run = self._run(status=InvestigationRun.Status.COMPLETED, state=_stan())

        self.assertEqual(self._json(run)["status"], "DEGRADED")

    def test_recorded_error_makes_the_result_degraded(self):
        run = self._run(
            status=InvestigationRun.Status.COMPLETED,
            state=_stan(
                sources={"technical": True, "analytics": True, "geo": True},
                errors=["AnalyticsAgent: brak autoryzacji"],
            ),
        )

        self.assertEqual(self._json(run)["status"], "DEGRADED")

    def test_failed_run_reports_failed(self):
        run = self._run(status=InvestigationRun.Status.FAILED)

        dane = self._json(run)
        self.assertEqual(dane["status"], "FAILED")
        self.assertTrue(dane["finished"])

    def test_partial_progress_is_reported_while_running(self):
        # Badanie trwa tyle, ile najwolniejsze źródło - sam pasek postępu bez tej
        # listy nie mówiłby, na co się czeka.
        run = self._run(
            status=InvestigationRun.Status.PROCESSING,
            progress={"technical": True, "geo": False},
        )

        dane = self._json(run)
        self.assertEqual(dane["sources"], {"technical": True, "geo": False})
        self.assertEqual(dane["source_count"], 1)
        self.assertEqual(dane["source_total"], 3)

    def test_finished_run_reports_final_sources_not_progress(self):
        run = self._run(
            status=InvestigationRun.Status.COMPLETED,
            progress={"technical": True},
            state=_stan(),
        )

        dane = self._json(run)
        self.assertEqual(
            dane["sources"], {"technical": True, "analytics": True, "geo": False}
        )
        self.assertEqual(dane["source_count"], 2)

    def test_finished_response_points_at_the_report(self):
        run = self._run(status=InvestigationRun.Status.COMPLETED, state=_stan())

        dane = self._json(run)
        self.assertEqual(
            dane["report_url"],
            reverse("auditor:investigation_detail", args=[run.pk]),
        )

    def test_unfinished_response_has_no_report_link(self):
        dane = self._json(self._run(status=InvestigationRun.Status.PROCESSING))

        self.assertNotIn("report_url", dane)

    def test_errors_are_listed_once_the_study_ends(self):
        run = self._run(
            status=InvestigationRun.Status.COMPLETED,
            state=_stan(errors=["AnalyticsAgent: brak autoryzacji"]),
        )

        self.assertEqual(
            self._json(run)["errors"], ["AnalyticsAgent: brak autoryzacji"]
        )

    def test_payload_identifies_the_run(self):
        run = self._run(status=InvestigationRun.Status.PROCESSING)

        dane = self._json(run)
        self.assertEqual(dane["run_id"], run.pk)
        self.assertEqual(dane["domain"], "enova.pl")

    def test_unknown_run_is_404(self):
        response = self.client.get(
            reverse("auditor:investigation_status", args=[10_000])
        )

        self.assertEqual(response.status_code, 404)

    def test_another_users_run_is_404_not_403(self):
        # Tak samo jak przy audytach: po kodzie odpowiedzi nie może dać się ustalić,
        # które identyfikatory istnieją.
        obcy = User.objects.create_user(
            username="obcy31@przyklad.pl",
            email="obcy31@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        cudzy = InvestigationRun.objects.create(domain="cudza.pl", owner=obcy)

        response = self.client.get(
            reverse("auditor:investigation_status", args=[cudzy.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_endpoint_requires_login(self):
        run = self._run()
        self.client.logout()

        response = self.client.get(
            reverse("auditor:investigation_status", args=[run.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)


class InvestigationDetailViewTests(TestCase):
    """Widok `/investigate/<pk>/` - raport odtworzony z zapisanego stanu."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="raport@przyklad.pl",
            email="raport@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _run(self, **pola):
        pola.setdefault("domain", "enova.pl")
        pola.setdefault("owner", self.user)
        pola.setdefault("status", InvestigationRun.Status.COMPLETED)
        pola.setdefault("state", _stan())
        pola.setdefault("report", pola["state"].get("final_synthesis_report", ""))
        return InvestigationRun.objects.create(**pola)

    def _open(self, run):
        return self.client.get(
            reverse("auditor:investigation_detail", args=[run.pk])
        )

    def test_finished_run_renders_the_report(self):
        response = self._open(self._run())

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/audit_investigation.html")
        self.assertContains(response, "Ruch spadł o 63%")

    def test_agents_are_not_run_again(self):
        # Sedno zapisywania wyniku: odświeżenie nie kosztuje trzech odczytów
        # ani płatnego wywołania modelu.
        from unittest.mock import patch

        with patch("auditor.agents.orchestrator.run_seo_investigation") as badanie:
            self._open(self._run())

        self.assertFalse(badanie.called)

    def test_context_exposes_the_named_inputs(self):
        response = self._open(self._run())

        self.assertIn("Główne Wnioski", response.context["report_markdown"])
        self.assertEqual(response.context["metrics"]["technical_score"], 66)
        self.assertEqual(
            response.context["sources"],
            {"technical": True, "analytics": True, "geo": False},
        )
        self.assertIsInstance(response.context["warnings"], list)

    def test_state_is_rebuilt_as_an_object_not_a_dict(self):
        response = self._open(self._run())

        state = response.context["state"]
        self.assertIsInstance(state, SEOInvestigatorState)
        self.assertEqual(state.domain, "enova.pl")
        self.assertEqual(state.source_count, 2)

    def test_dates_come_back_as_date_objects(self):
        # W JSON-ie leżą jako napisy; warstwa prezentacji liczy na nich wiek danych,
        # więc napis nie odjąłby się od dzisiejszej daty.
        response = self._open(self._run())

        daty = response.context["state"].data_timestamps
        self.assertEqual(daty["technical"], date(2026, 9, 10))
        self.assertEqual(
            daty["analytics"], datetime(2026, 9, 12, 8, 30, tzinfo=dtz.utc)
        )

    def test_rebuilt_dates_drive_the_staleness_warning(self):
        stary = _stan()
        stary["data_timestamps"] = {
            "technical": (timezone.localdate() - timedelta(days=40)).isoformat()
        }

        response = self._open(self._run(state=stary))

        self.assertTrue(response.context["has_stale_data"])
        self.assertContains(response, "Synteza opiera się na nieaktualnych danych")

    def test_fresh_dates_show_no_warning(self):
        swiezy = _stan()
        swiezy["data_timestamps"] = {"technical": timezone.localdate().isoformat()}

        response = self._open(self._run(state=swiezy))

        self.assertFalse(response.context["has_stale_data"])

    def test_kpi_cards_are_built_from_the_stored_metrics(self):
        response = self._open(self._run())

        karty = response.context["kpis"]
        self.assertEqual(len(karty), 3)
        self.assertEqual(karty[0]["value"], "66")
        self.assertEqual(karty[1]["value"], "14,284")
        self.assertContains(response, "Krytyczne błędy: 1")

    def test_source_badges_match_the_stored_sources(self):
        response = self._open(self._run())
        html = response.content.decode()

        self.assertEqual(html.count('"source-badge source-badge-on"'), 2)
        self.assertEqual(html.count('"source-badge source-badge-off"'), 1)

    def test_report_sections_keep_their_tones(self):
        response = self._open(self._run())

        tony = [sekcja["tone"] for sekcja in response.context["report_sections"]]
        self.assertEqual(tony, ["executive", "attention"])

    def test_unfinished_run_shows_the_waiting_page(self):
        run = self._run(status=InvestigationRun.Status.PROCESSING, state={}, report="")

        response = self._open(run)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/investigation_pending.html")
        self.assertContains(response, "Badanie w toku")

    def test_waiting_page_points_at_the_status_endpoint(self):
        run = self._run(status=InvestigationRun.Status.PROCESSING, state={}, report="")

        response = self._open(run)

        self.assertContains(
            response,
            reverse("auditor:investigation_status", args=[run.pk]),
        )

    def test_waiting_page_lists_the_three_sources(self):
        run = self._run(status=InvestigationRun.Status.PROCESSING, state={}, report="")

        response = self._open(run)

        for etykieta in ("Audyt Techniczny", "Analityka GA4/GSC", "Widoczność w AI (GEO)"):
            with self.subTest(etykieta=etykieta):
                self.assertContains(response, etykieta)

    def test_failed_run_still_opens(self):
        # Badanie zakończone błędem ma pokazać, co wiadomo - nie pustkę.
        run = self._run(status=InvestigationRun.Status.FAILED, state={}, report="")

        response = self._open(run)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/audit_investigation.html")

    def test_unknown_run_is_404(self):
        response = self.client.get(
            reverse("auditor:investigation_detail", args=[10_000])
        )

        self.assertEqual(response.status_code, 404)

    def test_another_users_run_is_404(self):
        obcy = User.objects.create_user(
            username="obcy32@przyklad.pl",
            email="obcy32@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        cudzy = InvestigationRun.objects.create(
            domain="cudza.pl", owner=obcy,
            status=InvestigationRun.Status.COMPLETED,
            state=_stan(), report="Sekret z cudzego badania.",
        )

        response = self.client.get(
            reverse("auditor:investigation_detail", args=[cudzy.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_view_requires_login(self):
        run = self._run()
        self.client.logout()

        response = self._open(run)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_stored_report_is_escaped_like_any_model_output(self):
        zlosliwy = _stan()
        zlosliwy["final_synthesis_report"] = "## Uwaga\n<script>alert(1)</script>"

        response = self._open(self._run(state=zlosliwy))
        html = response.content.decode()

        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
