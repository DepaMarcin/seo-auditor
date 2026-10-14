"""Normalizacja adresów przy weryfikacji dostępu do usług Google.

Ta sama domena występuje w Google i w polu formularza w kilku zapisach:
`sc-domain:earlystage.pl`, `https://earlystage.pl/`, `www.earlystage.pl`, samo
`earlystage.pl`. Porównanie ciągów znak w znak odmawiało dostępu do domeny, do
której konto dostęp MA - a użytkownik czytał to jako wygaśnięcie tokena.

Żadne wywołanie Google nie jest tu prawdziwe.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.models import Audit, GoogleAccount
from auditor.services.google_api import (
    audit_domain,
    find_authorized_domain,
    gsc_site_domain,
    normalize_host,
    property_domain,
)

User = get_user_model()

# Zapisy TEJ SAMEJ domeny, jakie użytkownik może wpisać w formularzu.
ZAPISY_DOMENY = (
    "earlystage.pl",
    "https://earlystage.pl",
    "https://earlystage.pl/",
    "http://earlystage.pl",
    "http://earlystage.pl/",
    "www.earlystage.pl",
    "https://www.earlystage.pl/",
    "EARLYSTAGE.PL",
    "  https://earlystage.pl/  ",
)


class NormalizeHostTests(SimpleTestCase):
    """`normalize_host` - jedno miejsce na regułę porównywania adresów."""

    def test_every_spelling_gives_the_same_host(self):
        wyniki = {normalize_host(zapis) for zapis in ZAPISY_DOMENY}

        self.assertEqual(wyniki, {"earlystage.pl"})

    def test_domain_property_prefix_is_stripped(self):
        # "sc-domain:" musi zniknąć przed resztą - inaczej dwukropek wyglądałby
        # jak numer portu i zostałaby pusta nazwa.
        self.assertEqual(normalize_host("sc-domain:earlystage.pl"), "earlystage.pl")

    def test_path_does_not_change_the_host(self):
        self.assertEqual(normalize_host("earlystage.pl/blog/x"), "earlystage.pl")
        self.assertEqual(
            normalize_host("https://earlystage.pl/blog/x?q=1#top"), "earlystage.pl"
        )

    def test_port_is_dropped(self):
        self.assertEqual(normalize_host("earlystage.pl:8080"), "earlystage.pl")
        self.assertEqual(normalize_host("https://earlystage.pl:8080/"), "earlystage.pl")

    def test_subdomain_is_kept(self):
        # Subdomena to inna rzecz niż domena główna - nie wolno jej zgubić.
        self.assertEqual(
            normalize_host("https://materialy.earlystage.pl/"), "materialy.earlystage.pl"
        )

    def test_www_is_not_a_subdomain(self):
        self.assertEqual(normalize_host("www.earlystage.pl"), "earlystage.pl")

    def test_empty_input_gives_an_empty_host(self):
        self.assertEqual(normalize_host(""), "")
        self.assertEqual(normalize_host(None), "")
        self.assertEqual(normalize_host("   "), "")

    def test_existing_helpers_share_the_rule(self):
        # `audit_domain` i `gsc_site_domain` muszą dawać ten sam wynik dla tej samej
        # domeny - rozjechanie ich to właśnie opisywana odmowa dostępu.
        self.assertEqual(
            audit_domain("https://www.earlystage.pl/cennik"),
            gsc_site_domain("sc-domain:earlystage.pl"),
        )

    def test_property_name_with_a_full_address(self):
        self.assertEqual(
            property_domain("Sklep (https://www.earlystage.pl/)"), "earlystage.pl"
        )


class FindAuthorizedDomainTests(SimpleTestCase):
    """Dopasowanie wpisanej domeny do listy uprawnień z Google."""

    def _konto(self, zapis="sc-domain:earlystage.pl"):
        """Lista uprawnień z jedną domeną, zapisaną tak jak ją oddaje Google."""
        return [
            {
                "domain": gsc_site_domain(zapis),
                "ga4": [],
                "gsc": [{"site_url": zapis, "label": zapis, "domain": gsc_site_domain(zapis)}],
            }
        ]

    def test_every_spelling_matches_a_domain_property(self):
        # Sedno błędu: wpisanie "https://earlystage.pl/" nie trafiało w
        # "sc-domain:earlystage.pl", bo schemat zostawał w porównywanym ciągu.
        konto = self._konto()

        for zapis in ZAPISY_DOMENY:
            with self.subTest(zapis=zapis):
                wynik = find_authorized_domain(konto, zapis)

                self.assertIsNotNone(wynik, f"nie dopasowano zapisu {zapis!r}")
                self.assertEqual(wynik["domain"], "earlystage.pl")

    def test_entry_stored_as_a_url_also_matches(self):
        # Search Console oddaje usługi prefiksowe jako pełne adresy.
        for zapis in ("https://earlystage.pl/", "http://earlystage.pl", "https://www.earlystage.pl/"):
            with self.subTest(zapis=zapis):
                self.assertIsNotNone(
                    find_authorized_domain(self._konto(zapis), "https://earlystage.pl/")
                )

    def test_subdomain_matches_the_main_domain_both_ways(self):
        # Jedno konto zwykle ma dostęp do całej rodziny adresów jednej firmy.
        self.assertIsNotNone(
            find_authorized_domain(self._konto(), "materialy.earlystage.pl")
        )
        self.assertIsNotNone(
            find_authorized_domain(
                self._konto("https://materialy.earlystage.pl/"), "earlystage.pl"
            )
        )

    def test_unrelated_domain_is_still_refused(self):
        # Luźniejsze dopasowanie nie może oznaczać dostępu do czegokolwiek.
        for obca in ("orlen.pl", "https://orlen.pl/", "earlystage.com", "notearlystage.pl"):
            with self.subTest(obca=obca):
                self.assertIsNone(find_authorized_domain(self._konto(), obca))

    def test_empty_query_matches_nothing(self):
        self.assertIsNone(find_authorized_domain(self._konto(), ""))
        self.assertIsNone(find_authorized_domain(self._konto(), None))

    def test_broken_entry_is_skipped_not_matched(self):
        lista = [{"domain": "", "ga4": [], "gsc": []}] + self._konto()

        self.assertEqual(
            find_authorized_domain(lista, "earlystage.pl")["domain"], "earlystage.pl"
        )


class AnalyticsEntryAccessTests(TestCase):
    """Wejście na `/analytics/?domain=` dla domeny dostępnej na koncie Google."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="dostep@przyklad.pl",
            email="dostep@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)
        konto, _ = GoogleAccount.objects.get_or_create(user=self.user)
        konto.refresh_token_encrypted = "jawny-token-testowy"
        konto.email = "jan@gmail.com"
        konto.save()

    def _open(self, wpisana, wlasciwosci=None, witryny=None):
        """Wejście z atrapą uprawnień konta Google."""
        witryny = (
            witryny
            if witryny is not None
            else [
                {
                    "site_url": "sc-domain:earlystage.pl",
                    "label": "sc-domain:earlystage.pl",
                    "domain": "earlystage.pl",
                }
            ]
        )
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"
        ), patch(
            "auditor.services.google_api.list_ga4_properties",
            return_value=wlasciwosci if wlasciwosci is not None else [],
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=witryny
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=[],
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.get_available_events",
            return_value=[],
        ), patch(
            "auditor.services.gsc_service.GSCService.resolve_site_url",
            return_value="sc-domain:earlystage.pl",
        ), patch(
            "auditor.services.audit_service.AuditService.sync_ga4_data"
        ), patch("auditor.services.audit_service.AuditService.sync_gsc_data"):
            return self.client.get(
                reverse("auditor:analytics"), {"domain": wpisana}, follow=True
            )

    def _komunikaty(self, response):
        return [str(m) for m in response.context["messages"]]

    def test_every_spelling_opens_the_panel(self):
        for zapis in ZAPISY_DOMENY:
            with self.subTest(zapis=zapis):
                Audit.objects.all().delete()

                response = self._open(zapis)

                self.assertEqual(response.status_code, 200)
                self.assertFalse(
                    any("Brak dostępu" in m for m in self._komunikaty(response)),
                    f"odmowa dostępu dla zapisu {zapis!r}",
                )

    def test_panel_is_reached_not_the_entry_screen(self):
        response = self._open("https://earlystage.pl/")

        self.assertTemplateUsed(response, "auditor/analytics_dashboard.html")

    def test_record_is_created_with_the_normalised_domain(self):
        self._open("https://WWW.earlystage.pl/cennik")

        audit = Audit.objects.get()
        self.assertEqual(audit.url, "https://earlystage.pl/")
        self.assertTrue(audit.analytics_only)

    def test_single_gsc_site_is_bound_automatically(self):
        # Domena z dostępem tylko do Search Console też ma zostać powiązana -
        # inaczej użytkownik trafia na ekran podłączenia bez powodu.
        self._open("earlystage.pl")

        self.assertEqual(Audit.objects.get().gsc_site_url, "sc-domain:earlystage.pl")

    def test_single_ga4_property_is_bound_automatically(self):
        self._open(
            "earlystage.pl",
            wlasciwosci=[
                {"property_id": "316375346", "display_name": "earlystage.pl",
                 "account_name": "Konto"}
            ],
        )

        self.assertEqual(Audit.objects.get().ga4_property_id, "316375346")

    def test_several_properties_leave_the_choice_to_the_user(self):
        # Przy kilku pasujących usługach zgadywanie przypisałoby cudze liczby.
        self._open(
            "earlystage.pl",
            wlasciwosci=[
                {"property_id": "111", "display_name": "earlystage.pl - stara",
                 "account_name": "Konto"},
                {"property_id": "222", "display_name": "earlystage.pl - nowa",
                 "account_name": "Konto"},
            ],
        )

        self.assertFalse(Audit.objects.get().ga4_property_id)

    def test_existing_audit_also_gets_the_service_bound(self):
        # Wcześniej autoselekcja działała tylko dla nowego rekordu, więc domena
        # z audytem technicznym lądowała na ekranie podłączenia.
        istniejacy = Audit.objects.create(url="https://earlystage.pl/", owner=self.user)

        self._open(
            "https://earlystage.pl/",
            wlasciwosci=[
                {"property_id": "316375346", "display_name": "earlystage.pl",
                 "account_name": "Konto"}
            ],
        )

        istniejacy.refresh_from_db()
        self.assertEqual(istniejacy.ga4_property_id, "316375346")
        self.assertEqual(Audit.objects.count(), 1)

    def test_existing_assignment_is_not_overwritten(self):
        # Przypisanie wskazane ręcznie to decyzja użytkownika, także gdy wskazuje
        # subdomenę - autoselekcja nie może jej cofać przy każdym wejściu.
        istniejacy = Audit.objects.create(
            url="https://earlystage.pl/", owner=self.user,
            ga4_property_id="999", gsc_site_url="https://materialy.earlystage.pl/",
        )

        self._open(
            "https://earlystage.pl/",
            wlasciwosci=[
                {"property_id": "316375346", "display_name": "earlystage.pl",
                 "account_name": "Konto"}
            ],
        )

        istniejacy.refresh_from_db()
        self.assertEqual(istniejacy.ga4_property_id, "999")
        self.assertEqual(istniejacy.gsc_site_url, "https://materialy.earlystage.pl/")

    def test_domain_outside_the_account_is_still_refused(self):
        # Naprawa normalizacji nie może zamienić się w dostęp do czegokolwiek.
        response = self._open("orlen.pl")

        self.assertTrue(any("Brak dostępu" in m for m in self._komunikaty(response)))
        self.assertFalse(Audit.objects.exists())
