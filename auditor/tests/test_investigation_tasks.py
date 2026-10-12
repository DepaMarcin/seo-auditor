"""Zrównoleglone badanie multi-agentowe: scalanie stanów i przepływ Celery.

Testy nie dotykają brokera. `CELERY_TASK_ALWAYS_EAGER` wykonuje akord w miejscu,
więc sprawdzana jest ta sama droga co na produkcji - group trzech zadań zbierających
i zadanie syntezy w roli callbacku - tylko bez Redisa.

Ani sieć, ani OpenAI nie są tu prawdziwe: agent syntezy jest podmieniany, a skan
techniczny atrapowany tam, gdzie domena nie ma audytu w bazie.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dtz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone

from auditor.agents.specialists import _fallback_report
from auditor.agents.state import SEOInvestigatorState, merge_states
from auditor.models import Audit, AuditMetric, InvestigationRun

User = get_user_model()

# Skan techniczny sięga do sieci, gdy domena nie ma audytu w bazie. W testach
# zastępujemy go wynikiem bez problemów - ta ścieżka ma własne testy w test_multi_agents.
PUSTY_SKAN = {
    "source": "scan",
    "audit_id": None,
    "score": None,
    "problems": [],
    "as_of": None,
    "error": "",
}


def _reporter_bez_sieci(state):
    """Agent syntezy składający raport z samych ustaleń - bez wywołania modelu."""
    state.final_synthesis_report = _fallback_report(state)
    return state


class StateMergeTests(TestCase):
    """Reguła scalania stanów zebranych przez równoległych wykonawców."""

    def _czastka(self, **pola):
        state = SEOInvestigatorState(domain="enova.pl")
        for nazwa, wartosc in pola.items():
            atrybut = getattr(state, nazwa)
            if isinstance(atrybut, list):
                atrybut.extend(wartosc)
            elif isinstance(atrybut, dict):
                atrybut.update(wartosc)
            else:
                setattr(state, nazwa, wartosc)
        return state.as_dict()

    def test_findings_from_all_agents_land_in_one_state(self):
        scalony = merge_states([
            self._czastka(technical_issues=["brak H1"]),
            self._czastka(analytics_insights=["sesje: 100"]),
            self._czastka(geo_visibility_notes=["nie mierzono"]),
        ])

        self.assertEqual(scalony.technical_issues, ["brak H1"])
        self.assertEqual(scalony.analytics_insights, ["sesje: 100"])
        self.assertEqual(scalony.geo_visibility_notes, ["nie mierzono"])

    def test_metrics_and_sources_are_fused(self):
        scalony = merge_states([
            self._czastka(metrics={"technical_score": 66}, sources={"technical": True}),
            self._czastka(metrics={"organic_sessions": 100}, sources={"analytics": True}),
            self._czastka(sources={"geo": False}),
        ])

        self.assertEqual(
            scalony.metrics, {"technical_score": 66, "organic_sessions": 100}
        )
        self.assertEqual(scalony.source_count, 2)

    def test_errors_from_every_agent_survive(self):
        pierwszy = SEOInvestigatorState(domain="enova.pl")
        pierwszy.record_error("TechnicalAgent", "skaner padł")
        drugi = SEOInvestigatorState(domain="enova.pl")
        drugi.record_error("AnalyticsAgent", "brak autoryzacji")

        scalony = merge_states([pierwszy.as_dict(), drugi.as_dict()])

        self.assertEqual(len(scalony.errors), 2)

    def test_timestamps_come_back_as_dates_not_strings(self):
        # Na datach liczy się wiek danych - napis nie odjąłby się od dzisiejszej daty.
        scalony = merge_states([
            self._czastka(data_timestamps={"technical": date(2026, 9, 10)}),
            self._czastka(
                data_timestamps={"analytics": datetime(2026, 9, 20, 12, 0, tzinfo=dtz.utc)}
            ),
        ])

        self.assertEqual(scalony.data_timestamps["technical"], date(2026, 9, 10))
        self.assertEqual(
            scalony.data_timestamps["analytics"],
            datetime(2026, 9, 20, 12, 0, tzinfo=dtz.utc),
        )

    def test_pure_date_does_not_gain_a_time(self):
        # `datetime.fromisoformat("2026-09-10")` też by się udało, ale dokładałoby
        # północ - precyzję, której w danych nie było.
        scalony = merge_states([self._czastka(data_timestamps={"geo": date(2026, 9, 10)})])

        self.assertIsInstance(scalony.data_timestamps["geo"], date)
        self.assertNotIsInstance(scalony.data_timestamps["geo"], datetime)

    def test_broken_timestamp_becomes_none_instead_of_crashing(self):
        scalony = merge_states([{"domain": "enova.pl", "data_timestamps": {"geo": "nie-data"}}])

        self.assertIsNone(scalony.data_timestamps["geo"])

    def test_audit_id_is_taken_from_whichever_agent_found_it(self):
        scalony = merge_states([
            self._czastka(),
            self._czastka(audit_id=7),
        ])

        self.assertEqual(scalony.audit_id, 7)

    def test_domain_comes_from_the_parts(self):
        scalony = merge_states([self._czastka()])

        self.assertEqual(scalony.domain, "enova.pl")

    def test_empty_input_gives_an_empty_state(self):
        scalony = merge_states([])

        self.assertEqual(scalony.domain, "")
        self.assertFalse(scalony.has_findings)

    def test_missing_parts_are_skipped(self):
        # Zadanie, które z jakiegoś powodu nie oddało słownika, nie może wywrócić
        # scalania - raport ma powstać z tego, co jest.
        scalony = merge_states([None, self._czastka(technical_issues=["brak H1"]), {}])

        self.assertEqual(scalony.technical_issues, ["brak H1"])

    def test_report_from_a_part_is_not_lost(self):
        scalony = merge_states([
            self._czastka(),
            self._czastka(final_synthesis_report="# Raport"),
        ])

        self.assertEqual(scalony.final_synthesis_report, "# Raport")

    def test_absorb_is_the_single_merge_rule(self):
        # Ten sam `absorb` obsługuje wątki i workerów - inaczej dwie kopie reguły
        # rozjechałyby się przy pierwszym nowym polu stanu.
        docelowy = SEOInvestigatorState(domain="enova.pl")
        zrodlo = SEOInvestigatorState(domain="enova.pl")
        zrodlo.technical_issues.append("brak H1")
        zrodlo.metrics["technical_score"] = 66

        docelowy.absorb(zrodlo)

        self.assertEqual(docelowy.technical_issues, ["brak H1"])
        self.assertEqual(docelowy.metrics["technical_score"], 66)


class CollectorTaskTests(TestCase):
    """Zadania zbierające: czysty stan na wejściu, zserializowany na wyjściu."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="zadania@przyklad.pl",
            email="zadania@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(url="https://enova.pl/", owner=cls.user, score=66)
        AuditMetric.objects.create(
            audit=cls.audit, category="seo", key="meta_description",
            value={"note": "Brak meta description."}, status="error",
        )

    def test_technical_task_returns_a_serialised_state(self):
        from auditor.tasks import run_technical_task

        wynik = run_technical_task(domain="enova.pl", owner_id=self.user.pk)

        self.assertIsInstance(wynik, dict)
        self.assertEqual(wynik["domain"], "enova.pl")
        self.assertTrue(wynik["sources"]["technical"])
        self.assertTrue(any("meta description" in w for w in wynik["technical_issues"]))

    def test_task_sees_only_its_owner_data(self):
        obcy = User.objects.create_user(
            username="obcy21@przyklad.pl",
            email="obcy21@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        from auditor.tasks import run_technical_task

        with patch("auditor.agents.tools._scan_technical_health", return_value=PUSTY_SKAN):
            wynik = run_technical_task(domain="enova.pl", owner_id=obcy.pk)

        self.assertFalse(any("meta description" in w for w in wynik["technical_issues"]))

    def test_unknown_owner_id_yields_no_data_instead_of_an_error(self):
        from auditor.tasks import run_technical_task

        with patch("auditor.agents.tools._scan_technical_health", return_value=PUSTY_SKAN):
            wynik = run_technical_task(domain="enova.pl", owner_id=10_000)

        self.assertIsInstance(wynik, dict)
        self.assertEqual(wynik["errors"], [])

    def test_analytics_task_returns_a_state_even_without_authorisation(self):
        from auditor.tasks import run_analytics_task

        wynik = run_analytics_task(domain="enova.pl", owner_id=self.user.pk)

        self.assertFalse(wynik["sources"]["analytics"])
        self.assertTrue(wynik["analytics_insights"])

    def test_geo_task_returns_a_state_without_a_study(self):
        from auditor.tasks import run_geo_task

        wynik = run_geo_task(domain="enova.pl", owner_id=self.user.pk)

        self.assertFalse(wynik["sources"]["geo"])
        self.assertTrue(wynik["geo_visibility_notes"])

    def test_unexpected_failure_is_recorded_not_raised(self):
        # W akordzie porażka członka grupy pomija callback, czyli awaria jednego
        # źródła skasowałaby cały raport. Zadanie musi więc zawsze coś oddać.
        from auditor.tasks import run_technical_task

        with patch(
            "auditor.agents.specialists.TechnicalAgent.run",
            side_effect=RuntimeError("niespodzianka"),
        ):
            wynik = run_technical_task(domain="enova.pl", owner_id=self.user.pk)

        self.assertIsInstance(wynik, dict)
        self.assertTrue(any("niespodzianka" in blad for blad in wynik["errors"]))

    def test_database_lock_is_retried(self):
        # "database is locked" przy równoległych workerach i SQLite mija samo -
        # to jedyna klasa awarii, przy której ponowienie ma sens.
        from auditor.tasks import run_technical_task

        with patch(
            "auditor.agents.specialists.TechnicalAgent.run",
            side_effect=OperationalError("database is locked"),
        ), patch.object(run_technical_task, "retry", side_effect=RuntimeError("retry")) as ponow:
            with self.assertRaises(RuntimeError):
                run_technical_task(domain="enova.pl", owner_id=self.user.pk)

        self.assertTrue(ponow.called)

    def test_exhausted_retries_degrade_instead_of_failing(self):
        from celery.exceptions import MaxRetriesExceededError

        from auditor.tasks import run_technical_task

        with patch(
            "auditor.agents.specialists.TechnicalAgent.run",
            side_effect=OperationalError("database is locked"),
        ), patch.object(run_technical_task, "retry", side_effect=MaxRetriesExceededError()):
            wynik = run_technical_task(domain="enova.pl", owner_id=self.user.pk)

        self.assertIsInstance(wynik, dict)
        self.assertTrue(wynik["errors"])
        # Brak klucza znaczy to samo co False - źródło nie wniosło danych.
        self.assertFalse(wynik["sources"].get("technical"))


class SynthesisTaskTests(TestCase):
    """Callback akordu: scalenie, synteza, zapis."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="synteza2@przyklad.pl",
            email="synteza2@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def _run(self):
        return InvestigationRun.objects.create(
            domain="enova.pl", owner=self.user,
            status=InvestigationRun.Status.PROCESSING,
        )

    def _czastki(self):
        techniczna = SEOInvestigatorState(domain="enova.pl")
        techniczna.technical_issues.append("Błąd: brak meta description.")
        techniczna.mark_source("technical", True)
        techniczna.metrics["technical_score"] = 66

        analityczna = SEOInvestigatorState(domain="enova.pl")
        analityczna.analytics_insights.append("Sesje organiczne: 14284.")
        analityczna.mark_source("analytics", True)

        geo = SEOInvestigatorState(domain="enova.pl")
        geo.geo_visibility_notes.append("Nie mierzono.")
        geo.mark_source("geo", False)

        return [techniczna.as_dict(), analityczna.as_dict(), geo.as_dict()]

    def test_report_is_written_to_the_record(self):
        from auditor.tasks import synthesize_report_task

        run = self._run()
        with patch(
            "auditor.agents.specialists.ReporterAgent.run", side_effect=_reporter_bez_sieci
        ):
            synthesize_report_task(self._czastki(), run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, InvestigationRun.Status.COMPLETED)
        self.assertIn("meta description", run.report)
        self.assertIsNotNone(run.finished_at)

    def test_full_state_is_stored_beside_the_report(self):
        # Strona wyniku odtwarza z niego karty KPI, odznaki źródeł i daty danych -
        # z samego tekstu raportu nie dałoby się ich zrekonstruować.
        from auditor.tasks import synthesize_report_task

        run = self._run()
        with patch(
            "auditor.agents.specialists.ReporterAgent.run", side_effect=_reporter_bez_sieci
        ):
            synthesize_report_task(self._czastki(), run.pk)

        run.refresh_from_db()
        self.assertEqual(run.state["metrics"]["technical_score"], 66)
        self.assertEqual(
            run.state["sources"], {"technical": True, "analytics": True, "geo": False}
        )

    def test_synthesis_failure_marks_the_record_failed(self):
        from auditor.tasks import synthesize_report_task

        run = self._run()
        with patch(
            "auditor.agents.specialists.ReporterAgent.run",
            side_effect=RuntimeError("model nie odpowiada"),
        ):
            wynik = synthesize_report_task(self._czastki(), run.pk)

        run.refresh_from_db()
        self.assertIsNone(wynik)
        self.assertEqual(run.status, InvestigationRun.Status.FAILED)

    def test_missing_record_does_not_crash_the_task(self):
        from auditor.tasks import synthesize_report_task

        with patch(
            "auditor.agents.specialists.ReporterAgent.run", side_effect=_reporter_bez_sieci
        ):
            wynik = synthesize_report_task(self._czastki(), 10_000)

        self.assertIsNone(wynik)

    def test_report_survives_one_agent_failing(self):
        # Sedno odporności: badanie z dwóch źródeł jest użyteczne, brak raportu nie.
        from auditor.tasks import synthesize_report_task

        padniety = SEOInvestigatorState(domain="enova.pl")
        padniety.record_error("AnalyticsAgent", "OperationalError: database is locked")

        dzialajacy = SEOInvestigatorState(domain="enova.pl")
        dzialajacy.technical_issues.append("Błąd: brak meta description.")
        dzialajacy.mark_source("technical", True)

        run = self._run()
        with patch(
            "auditor.agents.specialists.ReporterAgent.run", side_effect=_reporter_bez_sieci
        ):
            synthesize_report_task([padniety.as_dict(), dzialajacy.as_dict()], run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, InvestigationRun.Status.COMPLETED)
        self.assertIn("meta description", run.report)
        self.assertTrue(run.state["errors"])


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class InvestigationFlowTests(TestCase):
    """Cały przepływ: start_investigation -> group -> chord -> zapis."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="przeplyw@przyklad.pl",
            email="przeplyw@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(
            url="https://enova.pl/", owner=cls.user, score=66,
            ga4_property_id="1", ga4_organic_sessions=14284,
            ga4_history={"dates": [timezone.localdate().isoformat()]},
        )
        AuditMetric.objects.create(
            audit=cls.audit, category="seo", key="meta_description",
            value={"note": "Brak meta description."}, status="error",
        )

    def _start(self, domena="enova.pl"):
        from auditor.agents.orchestrator import start_investigation

        with patch(
            "auditor.agents.specialists.ReporterAgent.run", side_effect=_reporter_bez_sieci
        ), patch("auditor.agents.tools._scan_technical_health", return_value=PUSTY_SKAN):
            return start_investigation(domena, owner_id=self.user.pk)

    def test_flow_produces_a_finished_record(self):
        run_id, sciezka = self._start()

        run = InvestigationRun.objects.get(pk=run_id)
        self.assertEqual(sciezka, "eager")
        self.assertEqual(run.status, InvestigationRun.Status.COMPLETED)
        self.assertTrue(run.finished)
        self.assertTrue(run.report)

    def test_all_three_agents_contribute(self):
        run_id, _ = self._start()

        stan = InvestigationRun.objects.get(pk=run_id).state
        self.assertEqual(set(stan["sources"]), {"technical", "analytics", "geo"})
        self.assertTrue(stan["sources"]["technical"])
        self.assertTrue(stan["sources"]["analytics"])

    def test_record_belongs_to_the_requesting_user(self):
        run_id, _ = self._start()

        self.assertEqual(InvestigationRun.objects.get(pk=run_id).owner, self.user)

    def test_url_is_reduced_to_a_domain(self):
        run_id, _ = self._start("https://www.enova.pl/cennik")

        self.assertEqual(InvestigationRun.objects.get(pk=run_id).domain, "enova.pl")

    def test_empty_domain_is_refused_before_queuing(self):
        from auditor.agents.orchestrator import start_investigation

        with self.assertRaises(ValueError):
            start_investigation("   ", owner_id=self.user.pk)

        self.assertFalse(InvestigationRun.objects.exists())

    def test_one_failing_agent_still_yields_a_report(self):
        from auditor.agents.orchestrator import start_investigation

        with patch(
            "auditor.agents.specialists.ReporterAgent.run", side_effect=_reporter_bez_sieci
        ), patch(
            "auditor.agents.specialists.AnalyticsAgent.run",
            side_effect=RuntimeError("GA4 nie odpowiada"),
        ), patch("auditor.agents.tools._scan_technical_health", return_value=PUSTY_SKAN):
            run_id, _ = start_investigation("enova.pl", owner_id=self.user.pk)

        run = InvestigationRun.objects.get(pk=run_id)
        self.assertEqual(run.status, InvestigationRun.Status.COMPLETED)
        self.assertTrue(run.report)
        self.assertTrue(any("GA4 nie odpowiada" in blad for blad in run.state["errors"]))

    def test_record_is_created_before_the_work_starts(self):
        # Przeglądarka musi mieć co odpytywać o postęp od pierwszej chwili.
        from auditor import tasks

        with patch.object(tasks, "chord") as akord:
            run_id, _ = tasks.enqueue_investigation("enova.pl", owner_id=self.user.pk)

        self.assertTrue(akord.called)
        self.assertEqual(
            InvestigationRun.objects.get(pk=run_id).status,
            InvestigationRun.Status.PROCESSING,
        )

    def test_stored_state_feeds_the_presentation_layer(self):
        # Zapisany stan musi dać się odtworzyć w obiekt, z którego liczą się karty
        # KPI i ostrzeżenie o starych danych.
        from auditor.presentation import build_investigation_kpis, build_stale_data_warnings

        run_id, _ = self._start()
        odtworzony = SEOInvestigatorState.from_dict(
            InvestigationRun.objects.get(pk=run_id).state
        )

        karty = build_investigation_kpis(odtworzony)
        self.assertEqual(len(karty), 3)
        self.assertEqual(build_stale_data_warnings(odtworzony), [])

    def test_stale_dates_survive_the_round_trip(self):
        from auditor.presentation import build_stale_data_warnings

        Audit.objects.filter(pk=self.audit.pk).update(
            created_at=timezone.now() - timedelta(days=40)
        )
        run_id, _ = self._start()

        odtworzony = SEOInvestigatorState.from_dict(
            InvestigationRun.objects.get(pk=run_id).state
        )

        self.assertTrue(build_stale_data_warnings(odtworzony))


class FallbackPathTests(TestCase):
    """Ścieżki awaryjne: brak backendu wyników, brak brokera."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="awaria@przyklad.pl",
            email="awaria@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False, CELERY_RESULT_BACKEND="")
    def test_without_a_result_backend_the_work_goes_sequential(self):
        # `chord` bez backendu wyników nie ma gdzie odłożyć wyników grupy, więc
        # sprawdzamy to zanim obiecamy użytkownikowi równoległe badanie.
        from auditor import tasks

        with patch.object(tasks, "_run_in_background") as w_tle, patch.object(
            tasks, "chord"
        ) as akord:
            run_id, sciezka = tasks.enqueue_investigation("enova.pl", owner_id=self.user.pk)

        self.assertEqual(sciezka, "thread")
        self.assertFalse(akord.called)
        self.assertTrue(w_tle.called)
        self.assertTrue(InvestigationRun.objects.filter(pk=run_id).exists())

    @override_settings(
        CELERY_TASK_ALWAYS_EAGER=False, CELERY_RESULT_BACKEND="redis://127.0.0.1:6379/1"
    )
    def test_dead_broker_falls_back_to_a_thread(self):
        from auditor import tasks

        with patch.object(
            tasks, "chord", side_effect=OSError("broker nie odpowiada")
        ), patch.object(tasks, "_run_in_background") as w_tle:
            run_id, sciezka = tasks.enqueue_investigation("enova.pl", owner_id=self.user.pk)

        self.assertEqual(sciezka, "thread")
        self.assertTrue(w_tle.called)
        self.assertTrue(InvestigationRun.objects.filter(pk=run_id).exists())

    def test_sequential_fallback_produces_the_same_shape_of_result(self):
        from auditor import tasks

        run = InvestigationRun.objects.create(
            domain="enova.pl", owner=self.user,
            status=InvestigationRun.Status.PROCESSING,
        )
        with patch(
            "auditor.agents.specialists.ReporterAgent.run", side_effect=_reporter_bez_sieci
        ), patch("auditor.agents.tools._scan_technical_health", return_value=PUSTY_SKAN):
            tasks._run_investigation_now("enova.pl", self.user.pk, run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, InvestigationRun.Status.COMPLETED)
        self.assertEqual(set(run.state["sources"]), {"technical", "analytics", "geo"})

    def test_sequential_fallback_marks_failure(self):
        from auditor import tasks

        run = InvestigationRun.objects.create(
            domain="enova.pl", status=InvestigationRun.Status.PROCESSING
        )
        with patch(
            "auditor.agents.orchestrator.run_seo_investigation",
            side_effect=RuntimeError("padło"),
        ):
            tasks._run_investigation_now("enova.pl", None, run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, InvestigationRun.Status.FAILED)
