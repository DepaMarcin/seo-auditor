"""Selektor domen w nagłówku analityki - podział po dostępie konta Google.

Płaska lista wszystkich audytów wprowadzała w błąd: przy koncie widzącym tylko
motivationdirect.pl użytkownik miał w selektorze także enova.pl i daszer.pl,
a o braku uprawnień dowiadywał się dopiero po przełączeniu, z pustego panelu.

Podział liczymy z list JUŻ pobranych do kontekstu konta (usługi GA4 i witryny
Search Console), więc nie dokłada on żadnego zapytania do Google.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.models import Audit
from auditor.presentation import account_domains, split_audits_by_account_access


def _audyt(url: str, pk: int = 1):
    """Lekka atrapa audytu - podział potrzebuje wyłącznie `url` i `pk`."""
    return Audit(pk=pk, url=url)


def _konto(ga4: list[str] = (), gsc: list[str] = (), connected: bool = True) -> dict:
    return {
        "connected": connected,
        "email": "jan@gmail.com",
        "ga4_properties": [{"display_name": nazwa} for nazwa in ga4],
        "gsc_sites": [{"domain": domena} for domena in gsc],
    }


class AccountDomainsTests(SimpleTestCase):
    """Domeny konta składane z obu źródeł uprawnień."""

    def test_both_sources_are_merged(self):
        # Dostęp do GA4 i do Search Console nadaje się niezależnie - bywa jeden
        # bez drugiego, więc pominięcie któregokolwiek źródła gubiłoby domeny.
        domeny = account_domains(_konto(ga4=["przyklad.pl"], gsc=["inna.pl"]))

        self.assertEqual(domeny, ["inna.pl", "przyklad.pl"])

    def test_duplicates_collapse(self):
        domeny = account_domains(_konto(ga4=["przyklad.pl"], gsc=["przyklad.pl"]))

        self.assertEqual(domeny, ["przyklad.pl"])

    def test_entries_without_a_readable_domain_are_skipped(self):
        # Nazwa usługi GA4 bywa nazwą firmy ("Klient Testowy"), bez domeny.
        domeny = account_domains(_konto(ga4=["Klient Testowy", "przyklad.pl"], gsc=[""]))

        self.assertEqual(domeny, ["przyklad.pl"])


class SplitTests(SimpleTestCase):
    """Podział listy audytów na obsługiwane i pozostałe."""

    def test_audits_are_split_by_account_access(self):
        audyty = [
            _audyt("https://motivationdirect.pl/", pk=1),
            _audyt("https://enova.pl/", pk=2),
            _audyt("https://daszer.pl/", pk=3),
        ]

        wynik = split_audits_by_account_access(audyty, _konto(gsc=["motivationdirect.pl"]))

        self.assertTrue(wynik["grouped"])
        self.assertEqual([a.pk for a in wynik["matching"]], [1])
        self.assertEqual([a.pk for a in wynik["other"]], [2, 3])

    def test_subdomains_count_as_access(self):
        # Jedno konto Google zwykle ma dostęp do całej rodziny adresów firmy.
        audyty = [_audyt("https://sklep.przyklad.pl/", pk=1)]

        wynik = split_audits_by_account_access(audyty, _konto(gsc=["przyklad.pl"]))

        self.assertEqual([a.pk for a in wynik["matching"]], [1])

    def test_a_foreign_domain_never_matches(self):
        audyty = [_audyt("https://obca.pl/", pk=1)]

        wynik = split_audits_by_account_access(audyty, _konto(gsc=["przyklad.pl"]))

        self.assertEqual(wynik["matching"], [])
        self.assertEqual([a.pk for a in wynik["other"]], [1])

    def test_without_a_connected_account_nothing_is_grouped(self):
        # "Nie wiemy", a nie "brak dostępu": wrzucenie wszystkiego do drugiej grupy
        # byłoby twierdzeniem bez pokrycia.
        audyty = [_audyt("https://przyklad.pl/", pk=1)]

        wynik = split_audits_by_account_access(audyty, _konto(connected=False))

        self.assertFalse(wynik["grouped"])
        self.assertIsNone(wynik["suggested"])

    def test_an_api_failure_is_not_reported_as_missing_access(self):
        # Puste listy przy połączonym koncie to typowy objaw awarii API Google.
        audyty = [_audyt("https://przyklad.pl/", pk=1)]

        wynik = split_audits_by_account_access(audyty, _konto())

        self.assertFalse(wynik["grouped"])

    def test_a_single_supported_audit_is_suggested(self):
        audyty = [_audyt("https://obca.pl/", pk=1), _audyt("https://przyklad.pl/", pk=2)]

        wynik = split_audits_by_account_access(audyty, _konto(gsc=["przyklad.pl"]))

        self.assertEqual(wynik["suggested"].pk, 2)

    def test_nothing_is_suggested_when_the_choice_is_ambiguous(self):
        # Przy kilku obsługiwanych audytach wybór należy do użytkownika.
        audyty = [_audyt("https://a.pl/", pk=1), _audyt("https://b.pl/", pk=2)]

        wynik = split_audits_by_account_access(audyty, _konto(gsc=["a.pl", "b.pl"]))

        self.assertIsNone(wynik["suggested"])

    def test_nothing_is_suggested_when_no_audit_is_supported(self):
        audyty = [_audyt("https://a.pl/", pk=1)]

        wynik = split_audits_by_account_access(audyty, _konto(gsc=["zupelnie-inna.pl"]))

        self.assertIsNone(wynik["suggested"])


class RenderingTests(TestCase):
    """Co widzi użytkownik w selektorze."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="grupy@przyklad.pl", email="grupy@przyklad.pl", password="haslo-kontrolne-1"
        )
        cls.wlasna = Audit.objects.create(
            url="https://motivationdirect.pl/", owner=cls.user,
            ga4_property_id="111", ga4_refresh_token_encrypted="token",
        )
        cls.obca = Audit.objects.create(url="https://enova.pl/", owner=cls.user)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    def _html(self, pk: int, gsc: list[str]) -> str:
        google = {
            "connected": True,
            "email": "jan@gmail.com",
            "token_encrypted": False,
            "ga4_properties": [],
            "gsc_sites": [{"domain": d, "site_url": f"sc-domain:{d}", "label": d} for d in gsc],
            "connect_audit": None,
            "error": "",
        }
        with patch("auditor.views._google_account_context", return_value=google), patch(
            "auditor.views._fetch_ga4_available_events", return_value=[]
        ):
            response = self.client.get(reverse("auditor:analytics"), {"audit": pk})
        return response.content.decode()

    def test_both_groups_are_labelled(self):
        html = self._html(self.wlasna.pk, ["motivationdirect.pl"])

        self.assertIn('<optgroup label="Dostępne na tym koncie Google">', html)
        self.assertIn('<optgroup label="Pozostałe projekty (wymaga innego konta)">', html)

    def test_the_inaccessible_domain_says_so_in_the_label(self):
        html = self._html(self.wlasna.pk, ["motivationdirect.pl"])

        self.assertIn("https://enova.pl/ (brak dostępu na tym koncie)", html)

    def test_the_accessible_domain_carries_no_warning(self):
        html = self._html(self.wlasna.pk, ["motivationdirect.pl"])

        self.assertNotIn("https://motivationdirect.pl/ (brak dostępu", html)

    def test_a_flat_list_is_kept_when_access_is_unknown(self):
        # Awaria API Google nie może zamienić selektora w listę samych ostrzeżeń.
        html = self._html(self.wlasna.pk, [])

        self.assertNotIn("<optgroup", html)
        self.assertNotIn("(brak dostępu na tym koncie)", html)

    def test_the_switch_is_suggested_for_the_only_supported_domain(self):
        # Wejście na domenę, której konto nie obsługuje.
        html = self._html(self.obca.pk, ["motivationdirect.pl"])

        self.assertIn('class="panel analytics-suggestion"', html)
        self.assertIn(f"?audit={self.wlasna.pk}", html)

    def test_no_suggestion_when_the_current_domain_is_supported(self):
        html = self._html(self.wlasna.pk, ["motivationdirect.pl"])

        # Nazwa klasy jest też w arkuszu stylów - celujemy w sam znacznik.
        self.assertNotIn('class="panel analytics-suggestion"', html)
