"""Tabele Search Console, przepinanie usług i wykres widoczności w panelu analityki.

Puste kolumny „Teraz" i „Rok temu" w tabeli fraz miały konkretną przyczynę: szablon
czytał `row.current`, a zapisane wiersze mają `clicks_current`. Django renderuje
brakujący klucz jako pusty ciąg, więc błąd nie dawał żadnego sygnału - stąd testy
na KONKRETNE liczby w komórkach, a nie na obecność tabeli.

Żadne wywołanie Google nie jest tu prawdziwe.
"""
from __future__ import annotations

import re

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.models import Audit, GoogleAccount
from auditor.presentation import build_gsc_rows, page_label

User = get_user_model()

WITRYNY_GSC = [
    {
        "site_url": "sc-domain:earlystage.pl",
        "label": "sc-domain:earlystage.pl",
        "domain": "earlystage.pl",
    },
    {
        "site_url": "https://materialy.earlystage.pl/",
        "label": "materialy.earlystage.pl",
        "domain": "materialy.earlystage.pl",
    },
]


def _body_only(html: str) -> str:
    """HTML bez arkusza stylów - nazwa klasy w CSS nie znaczy, że element istnieje."""
    return re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL)


class GscRowBuilderTests(SimpleTestCase):
    """`build_gsc_rows` - przekład zapisanych wierszy na kształt dla tabeli."""

    FRAZA = {
        "query": "present simple",
        "clicks_current": 1090,
        "clicks_previous": 9,
        "delta": 1081,
        "impressions_current": 5000,
        "ctr_current": 21.8,
        "position_current": 3.1,
    }

    def test_clicks_come_from_the_stored_keys(self):
        # Sedno błędu: dane są pod `clicks_current`, nie pod `current`.
        wiersz = build_gsc_rows([self.FRAZA])[0]

        self.assertEqual(wiersz["current"], 1090)
        self.assertEqual(wiersz["previous"], 9)

    def test_query_becomes_the_label(self):
        self.assertEqual(build_gsc_rows([self.FRAZA])[0]["label"], "present simple")

    def test_direction_follows_the_delta(self):
        wzrost = build_gsc_rows([{**self.FRAZA, "delta": 5}])[0]
        spadek = build_gsc_rows([{**self.FRAZA, "delta": -5}])[0]
        bez_zmian = build_gsc_rows([{**self.FRAZA, "delta": 0}])[0]

        self.assertEqual(wzrost["direction"], "up")
        self.assertEqual(spadek["direction"], "down")
        self.assertEqual(bez_zmian["direction"], "")

    def test_missing_clicks_become_zero_not_blank(self):
        # Pusta komórka wygląda jak błąd renderowania; zero jest pomiarem.
        wiersz = build_gsc_rows([{"query": "fraza"}])[0]

        self.assertEqual(wiersz["current"], 0)
        self.assertEqual(wiersz["previous"], 0)

    def test_rows_without_a_name_are_skipped(self):
        # Wiersza bez frazy ani adresu nie da się opisać w tabeli.
        wiersze = build_gsc_rows([{"clicks_current": 10}, self.FRAZA])

        self.assertEqual(len(wiersze), 1)

    def test_limit_is_respected(self):
        wiersze = build_gsc_rows([dict(self.FRAZA, query=f"f{n}") for n in range(25)])

        self.assertEqual(len(wiersze), 10)

    def test_empty_input_gives_no_rows(self):
        self.assertEqual(build_gsc_rows([]), [])
        self.assertEqual(build_gsc_rows(None), [])

    def test_page_rows_carry_the_full_address(self):
        wiersz = build_gsc_rows(
            [{"page": "https://earlystage.pl/blog/x", "clicks_current": 5,
              "clicks_previous": 1, "delta": 4}],
            "earlystage.pl",
        )[0]

        self.assertEqual(wiersz["url"], "https://earlystage.pl/blog/x")
        self.assertEqual(wiersz["label"], "/blog/x")


class PageLabelTests(SimpleTestCase):
    """Skracanie adresów podstron."""

    def test_same_domain_shows_only_the_path(self):
        # Host powtarzałby się w każdym wierszu tabeli o jednej witrynie.
        self.assertEqual(
            page_label("https://earlystage.pl/blog/present-simple", "earlystage.pl"),
            "/blog/present-simple",
        )

    def test_other_subdomain_keeps_its_host(self):
        # To nie szum: ruch na innej subdomenie to inna rzecz niż na domenie głównej.
        self.assertEqual(
            page_label("https://universe.earlystage.pl/freebies", "earlystage.pl"),
            "universe.earlystage.pl/freebies",
        )

    def test_www_is_not_a_different_subdomain(self):
        self.assertEqual(
            page_label("https://www.earlystage.pl/blog/x", "earlystage.pl"), "/blog/x"
        )

    def test_homepage_shows_the_host(self):
        # Sama "/" nie mówi nic - pokazujemy domenę.
        self.assertEqual(
            page_label("https://earlystage.pl/", "earlystage.pl"), "earlystage.pl"
        )

    def test_query_string_is_kept(self):
        self.assertEqual(
            page_label("https://earlystage.pl/szukaj?q=angielski", "earlystage.pl"),
            "/szukaj?q=angielski",
        )

    def test_long_address_is_shortened_in_the_middle(self):
        # Koniec adresu bywa rozpoznawalny (nazwa artykułu), więc ucinamy środek.
        dlugi = "https://earlystage.pl/" + "/".join(f"segment{n}" for n in range(12))

        etykieta = page_label(dlugi, "earlystage.pl")

        self.assertLessEqual(len(etykieta), 64)
        self.assertIn("…", etykieta)
        self.assertTrue(etykieta.endswith("segment11"))

    def test_empty_address_gives_an_empty_label(self):
        self.assertEqual(page_label("", "earlystage.pl"), "")
        self.assertEqual(page_label(None, "earlystage.pl"), "")


class AnalyticsPanelBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="tabele@przyklad.pl",
            email="tabele@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(
            url="https://earlystage.pl/", owner=cls.user, status=Audit.Status.COMPLETED,
            ga4_property_id="316375346", ga4_organic_sessions=38035,
            gsc_site_url="sc-domain:earlystage.pl", gsc_total_clicks_current=103953,
            senuto_top3=5, senuto_top10=40, senuto_top50=120,
            senuto_history={
                "dates": ["2026-09-01", "2026-10-01"],
                "top3": [4, 5], "top10": [38, 40], "top50": [110, 120],
            },
            gsc_top_gainers=[
                {"query": "present simple", "clicks_current": 1090,
                 "clicks_previous": 9, "delta": 1081},
            ],
            gsc_top_losers=[
                {"query": "dni tygodnia", "clicks_current": 54,
                 "clicks_previous": 458, "delta": -404},
            ],
            gsc_top_page_gainers=[
                {"page": "https://universe.earlystage.pl/universe/freebies",
                 "clicks_current": 2180, "clicks_previous": 0, "delta": 2180},
            ],
            gsc_top_page_losers=[
                {"page": "https://earlystage.pl/blog/jezyki-swiata/",
                 "clicks_current": 957, "clicks_previous": 3965, "delta": -3008},
            ],
        )

    def setUp(self):
        self.client.force_login(self.user)
        cache.clear()
        konto, _ = GoogleAccount.objects.get_or_create(user=self.user)
        konto.refresh_token_encrypted = "jawny-token-testowy"
        konto.email = "jan@gmail.com"
        konto.save()

    def _open(self):
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=[],
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=WITRYNY_GSC
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.get_available_events",
            return_value=["generate_lead"],
        ):
            return self.client.get(
                reverse("auditor:audit_analytics", args=[self.audit.pk])
            )


class QueryTableTests(AnalyticsPanelBase):
    """Tabela „Frazy kluczowe rok do roku”."""

    def test_click_counts_are_rendered(self):
        # Regresja, od której zaczęło się zadanie: te komórki były puste.
        html = _body_only(self._open().content.decode())

        self.assertIn(">1090<", html)
        self.assertIn(">9<", html)
        self.assertIn(">54<", html)
        self.assertIn(">458<", html)

    def test_difference_column_shows_a_signed_number(self):
        html = _body_only(self._open().content.decode())

        self.assertIn("+1081", html)
        self.assertIn("-404", html)

    def test_difference_is_coloured_by_direction(self):
        html = _body_only(self._open().content.decode())

        self.assertIn("gsc-delta-positive", html)
        self.assertIn("gsc-delta-negative", html)

    def test_rows_reach_the_context_in_the_display_shape(self):
        kontekst = self._open().context

        self.assertEqual(kontekst["gsc_query_gainers"][0]["current"], 1090)
        self.assertEqual(kontekst["gsc_query_losers"][0]["delta"], -404)

    def test_table_is_hidden_without_data(self):
        Audit.objects.filter(pk=self.audit.pk).update(
            gsc_top_gainers=[], gsc_top_losers=[]
        )

        html = _body_only(self._open().content.decode())

        self.assertNotIn("Frazy kluczowe rok do roku", html)


class PageTableTests(AnalyticsPanelBase):
    """Nowa tabela „Podstrony rok do roku”."""

    def test_section_is_rendered(self):
        response = self._open()

        self.assertContains(response, "Podstrony rok do roku")

    def test_addresses_and_numbers_are_shown(self):
        html = _body_only(self._open().content.decode())

        self.assertIn(">2180<", html)
        self.assertIn(">3965<", html)
        self.assertIn("-3008", html)

    def test_main_domain_pages_show_only_the_path(self):
        html = _body_only(self._open().content.decode())

        self.assertIn(">/blog/jezyki-swiata/<", html)

    def test_other_subdomain_is_visible_in_the_label(self):
        # Inaczej 2180 kliknięć wyglądałoby na ruch domeny głównej.
        html = _body_only(self._open().content.decode())

        self.assertIn("universe.earlystage.pl/universe/freebies", html)

    def test_full_address_stays_in_the_title(self):
        html = _body_only(self._open().content.decode())

        self.assertIn('title="https://earlystage.pl/blog/jezyki-swiata/"', html)

    def test_section_is_hidden_without_data(self):
        Audit.objects.filter(pk=self.audit.pk).update(
            gsc_top_page_gainers=[], gsc_top_page_losers=[]
        )

        html = _body_only(self._open().content.decode())

        self.assertNotIn("Podstrony rok do roku", html)


class ServiceSwitchTests(AnalyticsPanelBase):
    """Przepinanie usługi GA4 / GSC i wymuszone odświeżenie danych."""

    def test_header_offers_both_actions(self):
        html = _body_only(self._open().content.decode())

        self.assertIn("Zmień usługę", html)
        self.assertIn("Odśwież dane", html)
        self.assertIn(f'action="/audits/{self.audit.pk}/refresh/"', html)

    def test_gsc_site_can_be_chosen_explicitly(self):
        html = _body_only(self._open().content.decode())

        self.assertIn('name="gsc_site_url"', html)
        self.assertIn("materialy.earlystage.pl", html)
        self.assertIn("dopasuj automatycznie", html)

    def test_current_site_is_preselected(self):
        html = _body_only(self._open().content.decode())

        self.assertRegex(html, r'value="sc-domain:earlystage\.pl"\s*\n?\s*selected')

    def test_explicit_site_overrides_the_automatic_match(self):
        # Automat nie odgadnie witryny zarejestrowanej pod inną nazwą ani wyboru
        # między subdomeną a domeną główną - i to jest powód tego pola.
        # Patchujemy nazwę w `views`, nie w serwisie: import na poziomie modułu
        # skopiował referencję, więc podmiana w serwisie nie dotarłaby do widoku.
        with patch("auditor.views._has_access_to_property", return_value=True), patch(
            "auditor.views.apply_google_services"
        ) as zapis:
            zapis.return_value = MagicMock(changed=True, fetched=True)
            self.client.post(
                reverse("auditor:assign_google_services", args=[self.audit.pk]),
                {
                    "ga4_property_id": "316375346",
                    "gsc_site_url": "https://materialy.earlystage.pl/",
                    "next": "analytics",
                },
            )

        self.assertEqual(
            zapis.call_args.kwargs["site_url"], "https://materialy.earlystage.pl/"
        )

    def test_empty_choice_means_automatic_matching(self):
        # Patchujemy nazwę w `views`, nie w serwisie: import na poziomie modułu
        # skopiował referencję, więc podmiana w serwisie nie dotarłaby do widoku.
        with patch("auditor.views._has_access_to_property", return_value=True), patch(
            "auditor.views.apply_google_services"
        ) as zapis:
            zapis.return_value = MagicMock(changed=False, fetched=False)
            self.client.post(
                reverse("auditor:assign_google_services", args=[self.audit.pk]),
                {"ga4_property_id": "316375346", "gsc_site_url": "", "next": "analytics"},
            )

        # None, nie pusty napis: `apply_google_services` rozróżnia "dopasuj sam"
        # od "zapisz pustą wartość".
        self.assertIsNone(zapis.call_args.kwargs["site_url"])

    def test_refresh_refetches_the_assigned_services(self):
        with patch("auditor.views._refresh_google_data", return_value=True) as pobranie:
            response = self.client.post(
                reverse("auditor:refresh_google_data", args=[self.audit.pk]),
                {"next": "analytics"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(pobranie.call_args.args[1], "316375346")
        self.assertEqual(pobranie.call_args.args[2], "sc-domain:earlystage.pl")

    def test_refresh_clears_the_cached_event_list(self):
        # Po zmianie usługi formularz celu musi pokazać zdarzenia NOWEJ usługi.
        cache.set("ga4_events:316375346", ["stare_zdarzenie"], 600)

        with patch("auditor.views._refresh_google_data", return_value=True):
            self.client.post(
                reverse("auditor:refresh_google_data", args=[self.audit.pk]),
                {"next": "analytics"},
            )

        self.assertIsNone(cache.get("ga4_events:316375346"))

    def test_refresh_without_a_service_is_refused(self):
        bez_uslugi = Audit.objects.create(url="https://inna.pl/", owner=self.user)

        with patch(
            "auditor.views._refresh_google_data",
            side_effect=AssertionError("nie powinno być wywołane bez usługi"),
        ):
            response = self.client.post(
                reverse("auditor:refresh_google_data", args=[bez_uslugi.pk])
            )

        self.assertEqual(response.status_code, 302)

    def test_failed_refresh_is_reported_not_crashed(self):
        with patch("auditor.views._refresh_google_data", return_value=False):
            self.client.post(
                reverse("auditor:refresh_google_data", args=[self.audit.pk]),
                {"next": "analytics"},
            )

        komunikaty = [
            str(m) for m in self.client.get(reverse("auditor:hub")).context["messages"]
        ]
        self.assertTrue(any("Nie udało się pobrać danych" in m for m in komunikaty))

    def test_refresh_requires_post(self):
        response = self.client.get(
            reverse("auditor:refresh_google_data", args=[self.audit.pk])
        )

        self.assertEqual(response.status_code, 405)

    def test_refresh_is_owner_scoped(self):
        obcy = User.objects.create_user(
            username="obcy51@przyklad.pl",
            email="obcy51@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        cudzy = Audit.objects.create(
            url="https://cudza.pl/", owner=obcy, ga4_property_id="999"
        )

        with patch(
            "auditor.views._refresh_google_data",
            side_effect=AssertionError("nie powinno dotknąć cudzego audytu"),
        ):
            response = self.client.post(
                reverse("auditor:refresh_google_data", args=[cudzy.pk])
            )

        self.assertEqual(response.status_code, 404)


class SenutoChartTests(AnalyticsPanelBase):
    """Wykres widoczności przeniesiony z raportu technicznego."""

    def test_chart_is_rendered_in_the_analytics_panel(self):
        response = self._open()

        self.assertContains(response, "Widoczność w wyszukiwarce (Senuto)")
        self.assertContains(response, 'id="senuto-visibility-chart"')

    def test_all_three_series_are_passed(self):
        html = _body_only(self._open().content.decode())

        self.assertIn('data-top3="4,5"', html)
        self.assertIn('data-top10="38,40"', html)
        self.assertIn('data-top50="110,120"', html)
        self.assertIn('data-dates="2026-09-01,2026-10-01"', html)

    def test_metric_switcher_covers_the_three_levels(self):
        html = _body_only(self._open().content.decode())

        for metryka in ("top3", "top10", "top50"):
            with self.subTest(metryka=metryka):
                self.assertIn(f'data-metric="{metryka}"', html)

    def test_switcher_shows_current_counts(self):
        html = _body_only(self._open().content.decode())

        self.assertIn("TOP 10: 40", html)

    def test_chart_library_is_loaded(self):
        html = _body_only(self._open().content.decode())

        self.assertIn("chart.umd.min.js", html)

    def test_panel_is_hidden_without_history(self):
        Audit.objects.filter(pk=self.audit.pk).update(senuto_history={})

        html = _body_only(self._open().content.decode())

        self.assertNotIn('id="senuto-visibility-chart"', html)

    def test_technical_report_no_longer_shows_the_chart(self):
        html = _body_only(
            self.client.get(
                reverse("auditor:detail", args=[self.audit.pk])
            ).content.decode()
        )

        self.assertNotIn("senuto-visibility-chart", html)


class MisleadingNoteTests(AnalyticsPanelBase):
    """Notatka odsyłająca po wykresy do raportu audytu."""

    def test_note_is_gone(self):
        # Raport techniczny nie zawiera już sekcji analitycznej, więc notatka
        # kierowała w puste miejsce.
        html = _body_only(self._open().content.decode())

        self.assertNotIn("znajdują się w", html)
        self.assertNotIn("#analityka", html)

    def test_charts_are_here_instead(self):
        html = _body_only(self._open().content.decode())

        self.assertIn('id="senuto-visibility-chart"', html)
        self.assertIn('id="analytics-range-picker"', html)
