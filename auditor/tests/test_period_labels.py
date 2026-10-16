"""Zakres dat pod liczbami w panelu analityki.

„Sesji organicznych w ostatnim pobranym okresie" nie pozwalało odróżnić 8915 sesji
z tygodnia od 8915 sesji z kwartału. Okres GA4 odczytujemy z zapisanej historii
dziennych sesji, a okres Search Console utrwalamy przy pobraniu - policzenie
„ostatnich 3 pełnych miesięcy" dziś opisałoby dane pobrane tydzień temu innym oknem.

Żadne wywołanie Google nie jest tu prawdziwe.
"""
from __future__ import annotations

import re

from datetime import date
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.models import Audit, GoogleAccount
from auditor.presentation import build_period_labels, ga4_period

User = get_user_model()


def _body_only(html: str) -> str:
    """HTML bez arkusza stylów, ze zwiniętymi białymi znakami."""
    return re.sub(r"\s+", " ", re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL))


def _dni(od: date, do: date) -> list[str]:
    """Lista kolejnych dni w zapisie ISO - taka, jaką trzyma `ga4_history`."""
    return [
        (od.toordinal() + przesuniecie and date.fromordinal(od.toordinal() + przesuniecie)).isoformat()
        for przesuniecie in range((do - od).days + 1)
    ]


class Ga4PeriodTests(SimpleTestCase):
    """Okres GA4 odczytany z historii dziennych sesji."""

    def test_period_spans_the_first_and_last_day(self):
        audit = Audit(
            url="https://x.pl/",
            ga4_history={"dates": _dni(date(2026, 9, 17), date(2026, 10, 16))},
        )

        self.assertEqual(
            ga4_period(audit), (date(2026, 9, 17), date(2026, 10, 16))
        )

    def test_order_in_the_history_does_not_matter(self):
        # Historia przychodzi z API i nie mamy gwarancji kolejności.
        audit = Audit(
            url="https://x.pl/",
            ga4_history={"dates": ["2026-10-16", "2026-09-17", "2026-10-01"]},
        )

        self.assertEqual(ga4_period(audit), (date(2026, 9, 17), date(2026, 10, 16)))

    def test_missing_history_gives_no_period(self):
        self.assertEqual(ga4_period(Audit(url="https://x.pl/")), (None, None))
        self.assertEqual(
            ga4_period(Audit(url="https://x.pl/", ga4_history={})), (None, None)
        )

    def test_broken_date_gives_no_period(self):
        # Zepsuta wartość nie może wywrócić renderu strony ani dać zgadniętego okresu.
        audit = Audit(url="https://x.pl/", ga4_history={"dates": ["nie-data"]})

        self.assertEqual(ga4_period(audit), (None, None))


class PeriodLabelTests(SimpleTestCase):
    """`build_period_labels` - gotowe teksty dla obu kafelków."""

    def _audit(self, **pola):
        domyslne = {
            "url": "https://x.pl/",
            "ga4_history": {"dates": _dni(date(2026, 9, 17), date(2026, 10, 16))},
            "gsc_period_start": date(2026, 7, 1),
            "gsc_period_end": date(2026, 9, 30),
        }
        domyslne.update(pola)
        return Audit(**domyslne)

    def test_ga4_label_states_the_dates(self):
        etykiety = build_period_labels(self._audit())

        self.assertEqual(etykiety["ga4"], "17.09.2026 – 16.10.2026")
        self.assertEqual(etykiety["ga4_days"], 30)

    def test_ga4_label_does_not_repeat_the_day_count(self):
        # Szablon mówi "w ostatnich 30 dniach", więc zakres nie powtarza liczby.
        self.assertNotIn("dni", build_period_labels(self._audit())["ga4"])

    def test_gsc_label_states_both_compared_periods(self):
        etykiety = build_period_labels(self._audit())

        self.assertEqual(etykiety["gsc"], "01.07.2026 – 30.09.2026 (92 dni)")
        self.assertEqual(etykiety["gsc_previous"], "01.07.2025 – 30.09.2025 (92 dni)")

    def test_year_ago_period_is_shifted_by_exactly_one_year(self):
        etykiety = build_period_labels(
            self._audit(gsc_period_start=date(2026, 1, 1), gsc_period_end=date(2026, 3, 31))
        )

        self.assertEqual(etykiety["gsc_previous"], "01.01.2025 – 31.03.2025 (90 dni)")

    def test_single_day_is_grammatical(self):
        etykiety = build_period_labels(
            self._audit(ga4_history={"dates": ["2026-10-16"]})
        )

        self.assertEqual(etykiety["ga4_days"], 1)
        self.assertEqual(
            build_period_labels(
                self._audit(
                    gsc_period_start=date(2026, 10, 16), gsc_period_end=date(2026, 10, 16)
                )
            )["gsc"],
            "16.10.2026 – 16.10.2026 (1 dzień)",
        )

    def test_missing_data_gives_empty_labels_not_a_guess(self):
        etykiety = build_period_labels(
            Audit(url="https://x.pl/", ga4_history={}, gsc_period_start=None)
        )

        self.assertEqual(etykiety["ga4"], "")
        self.assertEqual(etykiety["gsc"], "")
        self.assertEqual(etykiety["gsc_previous"], "")
        self.assertEqual(etykiety["ga4_days"], 0)


class PeriodInPanelTests(TestCase):
    """Podpisy w wyrenderowanym panelu."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="okresy@przyklad.pl",
            email="okresy@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)
        konto, _ = GoogleAccount.objects.get_or_create(user=self.user)
        konto.refresh_token_encrypted = "jawny-token-testowy"
        konto.email = "jan@gmail.com"
        konto.save()

    def _audit(self, **pola):
        domyslne = {
            "url": "https://earlystage.pl/",
            "owner": self.user,
            "status": Audit.Status.COMPLETED,
            "ga4_property_id": "316375346",
            "ga4_organic_sessions": 8915,
            "ga4_history": {"dates": _dni(date(2026, 9, 17), date(2026, 10, 16))},
            "gsc_total_clicks_current": 8704,
            "gsc_total_clicks_previous": 10811,
            "gsc_period_start": date(2026, 7, 1),
            "gsc_period_end": date(2026, 9, 30),
        }
        domyslne.update(pola)
        return Audit.objects.create(**domyslne)

    def _open(self, audit):
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=[],
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=[]
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.get_available_events",
            return_value=[],
        ):
            return self.client.get(
                reverse("auditor:audit_analytics", args=[audit.pk])
            )

    def test_vague_label_is_gone(self):
        html = _body_only(self._open(self._audit()).content.decode())

        self.assertNotIn("ostatnim pobranym okresie", html)

    def test_ga4_tile_names_the_exact_range(self):
        html = _body_only(self._open(self._audit()).content.decode())

        self.assertIn("sesji organicznych w ostatnich 30 dniach", html)
        self.assertIn("17.09.2026 – 16.10.2026", html)

    def test_gsc_tile_names_both_compared_periods(self):
        html = _body_only(self._open(self._audit()).content.decode())

        self.assertIn(
            'class="analytics-period-dates">Okres: 01.07.2026 – 30.09.2026', html
        )
        self.assertIn(
            'class="analytics-period-dates">Rok temu: 01.07.2025 – 30.09.2025', html
        )

    def test_record_without_a_stored_gsc_range_states_the_method(self):
        # Rekordy sprzed wdrożenia pól nie mają zakresu - podajemy wtedy samą
        # metodę porównania, bo jest stała i prawdziwa dla wszystkich tych danych.
        audit = self._audit(gsc_period_start=None, gsc_period_end=None)

        html = _body_only(self._open(audit).content.decode())

        self.assertIn("3 pełne miesiące kalendarzowe vs", html)
        # Celujemy w znacznik, nie w sam tekst: "Okres: " występuje też w skrypcie
        # aktualizującym podpis po zmianie zakresu, a skrypty jadą w tej odpowiedzi.
        self.assertNotIn('class="analytics-period-dates">Okres:', html)

    def test_record_without_ga4_history_says_so(self):
        audit = self._audit(ga4_history={})

        html = _body_only(self._open(audit).content.decode())

        self.assertIn("nie znamy zakresu zapisanych danych", html)

    def test_labels_reach_the_context(self):
        kontekst = self._open(self._audit()).context

        self.assertEqual(kontekst["periods"]["ga4_days"], 30)
        self.assertEqual(kontekst["periods"]["ga4"], "17.09.2026 – 16.10.2026")

    def test_both_subtitles_can_be_updated_by_the_script(self):
        # Bez identyfikatorów nowa liczba zostałaby ze starą etykietą okresu.
        html = _body_only(self._open(self._audit()).content.decode())

        self.assertIn('id="analytics-sessions-period"', html)
        self.assertIn('id="analytics-clicks-period"', html)


class GscPeriodPersistenceTests(TestCase):
    """Zakres Search Console zapisywany razem z liczbami."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="zakres@przyklad.pl",
            email="zakres@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def test_sync_stores_the_window_it_actually_used(self):
        # Sedno: zakres musi pochodzić z TEGO pobrania, a nie być liczony później.
        from auditor.services.audit_service import AuditService
        from auditor.services.date_ranges import last_n_full_months_range
        from auditor.services.ga4_service import YOY_COMPARISON_MONTHS

        audit = Audit.objects.create(url="https://earlystage.pl/", owner=self.user)
        puste = {
            "total_clicks_current": 0, "total_clicks_previous": 0,
            "yoy_change_percent": 0.0, "top_gainers": [], "top_losers": [],
        }
        oczekiwany = last_n_full_months_range(YOY_COMPARISON_MONTHS)

        with patch(
            "auditor.services.gsc_service.GSCService.fetch_yoy_query_performance",
            return_value=puste,
        ) as frazy, patch(
            "auditor.services.gsc_service.GSCService.fetch_yoy_page_performance",
            return_value=puste,
        ):
            AuditService().sync_gsc_data(audit, MagicMock())

        audit.refresh_from_db()
        self.assertEqual((audit.gsc_period_start, audit.gsc_period_end), oczekiwany)
        # Zakres przekazany do API musi być tym samym, który zapisaliśmy.
        self.assertEqual(frazy.call_args.args[2], oczekiwany[0])
        self.assertEqual(frazy.call_args.args[3], oczekiwany[1])

    def test_both_dimensions_share_one_window(self):
        # Frazy i podstrony muszą opisywać ten sam okres - inaczej tabele obok
        # siebie porównywałyby różne rzeczy.
        from auditor.services.audit_service import AuditService

        audit = Audit.objects.create(url="https://earlystage.pl/", owner=self.user)
        puste = {
            "total_clicks_current": 0, "total_clicks_previous": 0,
            "yoy_change_percent": 0.0, "top_gainers": [], "top_losers": [],
        }

        with patch(
            "auditor.services.gsc_service.GSCService.fetch_yoy_query_performance",
            return_value=puste,
        ) as frazy, patch(
            "auditor.services.gsc_service.GSCService.fetch_yoy_page_performance",
            return_value=puste,
        ) as strony:
            AuditService().sync_gsc_data(audit, MagicMock())

        self.assertEqual(frazy.call_args.args[2:], strony.call_args.args[2:])
