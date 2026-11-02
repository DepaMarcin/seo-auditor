"""Testy dynamicznego wyboru zakresu dat dla sekcji GA4/GSC.

Obejmują: pomocniki zakresów dat, walidację endpointu `auditor:analytics_data`,
izolację cache między zakresami oraz przeliczanie wniosków dla wybranego okresu.
Wszystkie wywołania Google API są mockowane.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.models import Audit
from auditor.services.date_ranges import (
    DEFAULT_RANGE_DAYS,
    MAX_RANGE_DAYS,
    DateRangeError,
    default_range,
    expected_days,
    expected_months_between,
    format_range_label,
    parse_iso_date,
    previous_year_range,
    quick_range,
    use_monthly_granularity,
    validate_range,
)

TODAY = date(2026, 9, 6)


class DateRangeHelpersTests(SimpleTestCase):
    def test_validate_accepts_normal_range(self):
        start, end = validate_range(date(2026, 6, 1), date(2026, 6, 30), today=TODAY)

        self.assertEqual((start, end), (date(2026, 6, 1), date(2026, 6, 30)))

    def test_rejects_reversed_range(self):
        with self.assertRaises(DateRangeError):
            validate_range(date(2026, 6, 30), date(2026, 6, 1), today=TODAY)

    def test_rejects_range_wider_than_api_window(self):
        start = TODAY - timedelta(days=MAX_RANGE_DAYS + 10)

        with self.assertRaises(DateRangeError):
            validate_range(start, TODAY, today=TODAY)

    def test_future_end_date_is_clamped_to_today(self):
        """Data z przyszłości to nie błąd użytkownika - API i tak nie ma takich danych."""
        _, end = validate_range(date(2026, 9, 1), date(2027, 1, 1), today=TODAY)

        self.assertEqual(end, TODAY)

    def test_rejects_start_date_in_the_future(self):
        with self.assertRaises(DateRangeError):
            validate_range(date(2027, 1, 1), date(2027, 2, 1), today=TODAY)

    def test_parse_iso_date_rejects_bad_format(self):
        with self.assertRaises(DateRangeError):
            parse_iso_date("06-09-2026", "start_date")

    def test_quick_range_spans_expected_number_of_days(self):
        start, end = quick_range("7d", today=TODAY)

        self.assertEqual((end - start).days + 1, 7)
        # Zakres kończy się na ostatnim PEŁNYM dniu: trwający dzień ma dane tylko
        # do bieżącej godziny, więc zaniżałby sumy i psuł porównanie rok do roku.
        self.assertEqual(end, TODAY - timedelta(days=1))

    def test_the_default_window_is_four_full_weeks(self):
        # 28, nie 30: cztery pełne tygodnie, więc porównanie nie liczy dwukrotnie
        # żadnego dnia tygodnia (weekend różni się od środka tygodnia na tyle, że
        # okno 30-dniowe przekrzywiało wynik).
        self.assertEqual(DEFAULT_RANGE_DAYS, 28)
        self.assertEqual(DEFAULT_RANGE_DAYS % 7, 0)

    def test_the_default_range_covers_28_complete_days(self):
        start, end = default_range(today=TODAY)

        self.assertEqual(end, TODAY - timedelta(days=1))
        self.assertEqual(start, TODAY - timedelta(days=28))
        self.assertEqual((end - start).days + 1, 28)

    def test_the_quick_button_matches_the_default(self):
        # Przycisk "28 dni" jest zaznaczony przy wejściu, więc musi dawać dokładnie
        # ten sam zakres co widok bez parametrów - inaczej kliknięcie w podświetlony
        # przycisk zmieniałoby liczby.
        self.assertEqual(quick_range("28d", today=TODAY), default_range(today=TODAY))

    def test_the_old_thirty_day_key_is_gone(self):
        with self.assertRaises(DateRangeError):
            quick_range("30d", today=TODAY)

    def test_today_is_never_part_of_a_range(self):
        for klucz in ("7d", "28d", "90d", "12m"):
            with self.subTest(klucz=klucz):
                _, end = quick_range(klucz, today=TODAY)

                self.assertLess(end, TODAY)

    def test_previous_year_range_shifts_exactly_one_year(self):
        start, end = previous_year_range(date(2026, 6, 1), date(2026, 6, 30))

        self.assertEqual((start, end), (date(2025, 6, 1), date(2025, 6, 30)))

    def test_previous_year_range_handles_leap_day(self):
        start, end = previous_year_range(date(2024, 2, 29), date(2024, 2, 29))

        self.assertEqual((start, end), (date(2023, 2, 28), date(2023, 2, 28)))

    def test_granularity_switches_to_months_for_long_ranges(self):
        self.assertFalse(use_monthly_granularity(TODAY - timedelta(days=29), TODAY))
        self.assertTrue(use_monthly_granularity(TODAY - timedelta(days=364), TODAY))

    def test_expected_days_covers_whole_range(self):
        keys = expected_days(date(2026, 6, 28), date(2026, 7, 2))

        self.assertEqual(keys, ["20260628", "20260629", "20260630", "20260701", "20260702"])

    def test_expected_months_covers_whole_range(self):
        keys = expected_months_between(date(2025, 11, 15), date(2026, 2, 3))

        self.assertEqual(keys, ["202511", "202512", "202601", "202602"])

    def test_range_label_mentions_span(self):
        self.assertIn("(30 dni)", format_range_label(date(2026, 6, 1), date(2026, 6, 30)))


class AnalyticsDataEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="analityk", password="haslo12345")
        self.client.force_login(self.user)
        self.audit = Audit.objects.create(
            url="https://example.com",
            status=Audit.Status.COMPLETED,
            owner=self.user,
            ga4_property_id="123456",
        )
        self.url = reverse("auditor:analytics_data", kwargs={"pk": self.audit.pk})

    def test_requires_login(self):
        self.client.logout()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_foreign_audit_returns_404(self):
        other = User.objects.create_user(username="obcy2", password="haslo12345")
        foreign = Audit.objects.create(url="https://obca.pl", owner=other)

        response = self.client.get(reverse("auditor:analytics_data", kwargs={"pk": foreign.pk}))

        self.assertEqual(response.status_code, 404)

    def test_rejects_reversed_range_with_400(self):
        response = self.client.get(self.url, {"start_date": "2026-06-30", "end_date": "2026-06-01"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("późniejsza", response.json()["error"])

    def test_rejects_invalid_date_format_with_400(self):
        response = self.client.get(self.url, {"start_date": "30-06-2026", "end_date": "2026-07-01"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("YYYY-MM-DD", response.json()["error"])

    def test_rejects_too_wide_range_with_400(self):
        start = (date.today() - timedelta(days=MAX_RANGE_DAYS + 30)).isoformat()

        response = self.client.get(self.url, {"start_date": start, "end_date": date.today().isoformat()})

        self.assertEqual(response.status_code, 400)
        self.assertIn("zbyt szeroki", response.json()["error"])

    def test_rejects_incomplete_range_with_400(self):
        response = self.client.get(self.url, {"start_date": "2026-06-01"})

        self.assertEqual(response.status_code, 400)

    def test_rejects_unknown_source_with_400(self):
        response = self.client.get(self.url, {"range": "28d", "source": "facebook"})

        self.assertEqual(response.status_code, 400)

    def test_quick_range_returns_resolved_dates(self):
        response = self.client.get(self.url, {"range": "7d", "source": "ga4"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()["range"]
        span = date.fromisoformat(payload["end_date"]) - date.fromisoformat(payload["start_date"])
        self.assertEqual(span.days + 1, 7)

    def test_without_google_connection_reports_unavailable(self):
        """Brak połączenia z Google to nie błąd 500 - sekcja po prostu nie ma danych."""
        response = self.client.get(self.url, {"range": "28d"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ga4"]["available"])
        self.assertFalse(response.json()["gsc"]["available"])

    def test_source_parameter_limits_computed_sections(self):
        response = self.client.get(self.url, {"range": "28d", "source": "gsc"})

        payload = response.json()
        self.assertIn("gsc", payload)
        self.assertNotIn("ga4", payload)


class AnalyticsPayloadTests(TestCase):
    """Pełna ścieżka endpointu z zamockowanymi serwisami Google."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="analityk2", password="haslo12345")
        self.client.force_login(self.user)
        with self.settings(TOKEN_ENCRYPTION_KEY=""):
            self.audit = Audit.objects.create(
                url="https://example.com",
                status=Audit.Status.COMPLETED,
                owner=self.user,
                ga4_property_id="123456",
                ga4_refresh_token_encrypted="token-testowy",
                ga4_selected_lead_event="generate_lead",
            )
        self.url = reverse("auditor:analytics_data", kwargs={"pk": self.audit.pk})

    def _patched_services(self):
        ga4 = MagicMock()
        ga4.fetch_organic_traffic.return_value = {
            "total_sessions": 401,
            "granularity": "day",
            "history": {"dates": ["2026-06-01", "2026-06-02"], "sessions": [200, 201]},
        }
        ga4.fetch_channel_history.return_value = {
            "months": ["2026-06-01", "2026-06-02"],
            "channels": {"Organic Search": [200, 201], "Paid Search": [10, 12]},
        }
        ga4.fetch_event_conversions.return_value = {
            "total_events": 8,
            "history": {"months": ["2026-06-01", "2026-06-02"], "events": [3, 5]},
        }
        ga4.fetch_yoy_summary.return_value = {
            "channels": {
                "current": {"Organic Search": 401, "Paid Search": 22, "Direct": 90},
                "previous": {"Organic Search": 300, "Paid Search": 20, "Direct": 100},
            },
            "leads": {"current": 8, "previous": 4},
        }

        gsc = MagicMock()
        gsc.fetch_yoy_query_performance.return_value = {
            "total_clicks_current": 1500,
            "total_clicks_previous": 1200,
            "yoy_change_percent": 25.0,
            "top_gainers": [{"query": "audyt seo", "clicks_current": 100, "clicks_previous": 40, "delta": 60}],
            "top_losers": [{"query": "seo cennik", "clicks_current": 10, "clicks_previous": 50, "delta": -40}],
        }
        gsc.fetch_yoy_page_performance.return_value = {
            "total_clicks_current": 1500,
            "total_clicks_previous": 1200,
            "yoy_change_percent": 25.0,
            "top_gainers": [{"page": "/blog/audyt", "clicks_current": 80, "clicks_previous": 20, "delta": 60}],
            "top_losers": [],
        }
        return ga4, gsc

    def test_payload_contains_kpi_series_and_insights(self):
        ga4, gsc = self._patched_services()

        with patch("auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()), \
                patch("auditor.views.GA4OAuthService", return_value=ga4), \
                patch("auditor.views.GSCService", return_value=gsc):
            response = self.client.get(self.url, {"start_date": "2026-06-01", "end_date": "2026-06-02"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["ga4"]["organic_sessions"], 401)
        self.assertEqual(payload["ga4"]["history"]["sessions"], [200, 201])
        self.assertIn("Organic Search", payload["ga4"]["channels"]["channels"])
        self.assertEqual(payload["gsc"]["total_clicks_current"], 1500)
        self.assertEqual(payload["gsc"]["top_gainers"][0]["query"], "audyt seo")

    def test_insights_are_recalculated_for_selected_period(self):
        """Wnioski mają opisywać wybrany okres, a nie zaszyte "ostatnie 3 miesiące"."""
        ga4, gsc = self._patched_services()

        with patch("auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()), \
                patch("auditor.views.GA4OAuthService", return_value=ga4), \
                patch("auditor.views.GSCService", return_value=gsc):
            response = self.client.get(self.url, {"start_date": "2026-06-01", "end_date": "2026-06-02"})

        payload = response.json()
        joined = " ".join(payload["ga4"]["insights"]["summary_points"])
        self.assertIn("01.06.2026 - 02.06.2026", joined)
        self.assertIn("01.06.2026 - 02.06.2026", payload["gsc"]["query_commentary"])
        # Ruch organiczny 300 -> 401 to wzrost o 33.7%.
        self.assertIn("33.7%", joined)

    def test_services_receive_the_requested_range(self):
        ga4, gsc = self._patched_services()

        with patch("auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()), \
                patch("auditor.views.GA4OAuthService", return_value=ga4), \
                patch("auditor.views.GSCService", return_value=gsc):
            self.client.get(self.url, {"start_date": "2026-06-01", "end_date": "2026-06-30"})

        args = ga4.fetch_organic_traffic.call_args.args
        self.assertEqual(args[2], date(2026, 6, 1))
        self.assertEqual(args[3], date(2026, 6, 30))
        gsc_args = gsc.fetch_yoy_query_performance.call_args.args
        self.assertEqual(gsc_args[2], date(2026, 6, 1))
        self.assertEqual(gsc_args[3], date(2026, 6, 30))


class RangeScopedCacheTests(SimpleTestCase):
    """Klucz cache musi zawierać zakres dat - inaczej zmiana zakresu zwróciłaby
    zbuforowane dane poprzedniego okresu."""

    def setUp(self):
        cache.clear()

    def test_ga4_cache_keys_differ_between_ranges(self):
        from auditor.services.ga4_service import GA4OAuthService

        service = GA4OAuthService()
        key_june = service._cache_key("traffic", "123", date(2026, 6, 1), date(2026, 6, 30))
        key_july = service._cache_key("traffic", "123", date(2026, 7, 1), date(2026, 7, 31))

        self.assertNotEqual(key_june, key_july)
        self.assertIn("2026-06-01", key_june)
        self.assertIn("2026-07-31", key_july)

    def test_ga4_cache_key_separates_lead_events(self):
        from auditor.services.ga4_service import GA4OAuthService

        service = GA4OAuthService()
        a = service._cache_key("events", "123", date(2026, 6, 1), date(2026, 6, 30), extra="generate_lead")
        b = service._cache_key("events", "123", date(2026, 6, 1), date(2026, 6, 30), extra="purchase")

        self.assertNotEqual(a, b)

    def test_second_call_with_same_range_is_served_from_cache(self):
        from auditor.services.ga4_service import GA4OAuthService

        service = GA4OAuthService()
        with patch("auditor.services.ga4_service.BetaAnalyticsDataClient") as mock_client:
            mock_client.return_value.run_report.return_value = MagicMock(rows=[])
            service.fetch_organic_traffic(MagicMock(), "123", date(2026, 6, 1), date(2026, 6, 30))
            service.fetch_organic_traffic(MagicMock(), "123", date(2026, 6, 1), date(2026, 6, 30))

            self.assertEqual(mock_client.return_value.run_report.call_count, 1)

    def test_different_range_bypasses_cache(self):
        from auditor.services.ga4_service import GA4OAuthService

        service = GA4OAuthService()
        with patch("auditor.services.ga4_service.BetaAnalyticsDataClient") as mock_client:
            mock_client.return_value.run_report.return_value = MagicMock(rows=[])
            service.fetch_organic_traffic(MagicMock(), "123", date(2026, 6, 1), date(2026, 6, 30))
            service.fetch_organic_traffic(MagicMock(), "123", date(2026, 7, 1), date(2026, 7, 31))

            self.assertEqual(mock_client.return_value.run_report.call_count, 2)


class GscDefaultWindowTests(SimpleTestCase):
    """Okno Search Console bez jawnego zakresu musi być TO SAMO co w GA4.

    Wcześniej GA4 liczyło 30 dni, a Search Console 3 pełne miesiące kalendarzowe
    (~92 dni), więc kafelki obok siebie opisywały różne okresy i nie dawały się
    porównać - 442 sesje organiczne przy 1248 kliknięciach to zestawienie bez
    sensu, jeśli liczby pochodzą z innych przedziałów czasu.
    """

    def setUp(self):
        super().setUp()
        cache.clear()

    def _zapytane_okresy(self) -> list[tuple[str, str]]:
        """Uruchamia pobranie bez zakresu i zwraca odpytane pary (start, koniec)."""
        okresy: list[tuple[str, str]] = []

        class _Zapytanie:
            def __init__(self, body):
                okresy.append((body["startDate"], body["endDate"]))

            def execute(self):
                return {"rows": []}

        class _Analityka:
            def query(self, siteUrl, body):  # noqa: N803 - nazwa z API Google
                return _Zapytanie(body)

        class _Serwis:
            def searchanalytics(self):
                return _Analityka()

        from auditor.services.gsc_service import GSCService

        with patch.object(GSCService, "_build_service", return_value=_Serwis()), patch(
            "auditor.services.gsc_service.find_best_gsc_site",
            return_value="sc-domain:przyklad.pl",
        ):
            GSCService().fetch_yoy_query_performance(MagicMock(), "https://przyklad.pl/")
        return okresy

    def test_the_current_period_is_the_default_28_day_window(self):
        start, koniec = default_range()
        okresy = self._zapytane_okresy()

        self.assertIn((start.isoformat(), koniec.isoformat()), okresy)

    def test_the_comparison_period_is_the_same_28_days_a_year_earlier(self):
        rok_start, rok_koniec = previous_year_range(*default_range())
        okresy = self._zapytane_okresy()

        self.assertIn((rok_start.isoformat(), rok_koniec.isoformat()), okresy)

    def test_every_queried_period_spans_exactly_28_days(self):
        for start, koniec in self._zapytane_okresy():
            with self.subTest(okres=(start, koniec)):
                dni = (date.fromisoformat(koniec) - date.fromisoformat(start)).days + 1

                self.assertEqual(dni, DEFAULT_RANGE_DAYS)

    def test_no_queried_period_reaches_today(self):
        # Trwający dzień zaniżałby sumy w kafelku.
        for _, koniec in self._zapytane_okresy():
            with self.subTest(koniec=koniec):
                self.assertLess(date.fromisoformat(koniec), date.today())
