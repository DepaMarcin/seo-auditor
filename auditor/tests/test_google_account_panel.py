"""Pasek konta Google i przypisywanie usług GA4 / GSC do audytów.

Użytkownik obsługujący kilka firm ma zwykle kilka kont Google i kilkanaście usług
na każdym. Bez pokazania, czyje konto jest podłączone, i bez możliwości wskazania
usługi wprost, dane w raporcie mogą po cichu pochodzić z cudzej witryny.

Żadne wywołanie Google nie jest tu prawdziwe.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.models import Audit
from auditor.services.google_api import (
    audit_domain,
    fetch_account_email,
    gsc_site_domain,
    list_gsc_sites,
    mark_suggestions,
)

User = get_user_model()

WLASCIWOSCI_GA4 = [
    {"property_id": "111111", "display_name": "przyklad.pl - GA4", "account_name": "Agencja"},
    {"property_id": "222222", "display_name": "inna-firma.pl", "account_name": "Agencja"},
]

WITRYNY_GSC = [
    {"siteUrl": "sc-domain:przyklad.pl", "permissionLevel": "siteOwner"},
    {"siteUrl": "https://inna-firma.pl/", "permissionLevel": "siteFullUser"},
    {"siteUrl": "https://niezweryfikowana.pl/", "permissionLevel": "siteUnverifiedUser"},
]


class GoogleApiHelpersTests(SimpleTestCase):
    """Pomocnicy odpytujący API Google."""

    def test_account_email_is_read_from_userinfo(self):
        service = MagicMock()
        service.userinfo.return_value.get.return_value.execute.return_value = {
            "email": "jan.kowalski@gmail.com"
        }

        with patch("googleapiclient.discovery.build", return_value=service):
            self.assertEqual(fetch_account_email(MagicMock()), "jan.kowalski@gmail.com")

    def test_missing_scope_does_not_raise(self):
        # Tokeny wydane przed dodaniem zakresu `userinfo.email` go nie mają.
        with patch("googleapiclient.discovery.build", side_effect=Exception("insufficient scope")):
            self.assertEqual(fetch_account_email(MagicMock()), "")

    def test_gsc_sites_are_listed_without_unverified_ones(self):
        service = MagicMock()
        service.sites.return_value.list.return_value.execute.return_value = {
            "siteEntry": WITRYNY_GSC
        }

        with patch("googleapiclient.discovery.build", return_value=service):
            witryny = list_gsc_sites(MagicMock())

        adresy = [w["site_url"] for w in witryny]
        self.assertIn("sc-domain:przyklad.pl", adresy)
        self.assertIn("https://inna-firma.pl/", adresy)
        # Niezweryfikowana usługa jest widoczna na liście, ale zapytanie o jej dane
        # kończy się błędem - w selektorze byłaby pułapką.
        self.assertNotIn("https://niezweryfikowana.pl/", adresy)

    def test_gsc_api_failure_returns_an_empty_list(self):
        with patch("googleapiclient.discovery.build", side_effect=Exception("503")):
            self.assertEqual(list_gsc_sites(MagicMock()), [])

    def test_domain_property_label_is_explained(self):
        service = MagicMock()
        service.sites.return_value.list.return_value.execute.return_value = {
            "siteEntry": [{"siteUrl": "sc-domain:przyklad.pl", "permissionLevel": "siteOwner"}]
        }

        with patch("googleapiclient.discovery.build", return_value=service):
            witryna = list_gsc_sites(MagicMock())[0]

        self.assertIn("usługa domenowa", witryna["label"])
        self.assertEqual(witryna["domain"], "przyklad.pl")

    def test_domains_are_normalised(self):
        self.assertEqual(gsc_site_domain("sc-domain:Przyklad.PL"), "przyklad.pl")
        self.assertEqual(gsc_site_domain("https://www.przyklad.pl/sklep"), "przyklad.pl")
        self.assertEqual(audit_domain("https://www.przyklad.pl/oferta"), "przyklad.pl")


class SuggestionTests(SimpleTestCase):
    """Dopasowanie usługi do domeny audytu."""

    def _opcje(self):
        return [
            {"value": "2", "label": "inna-firma.pl", "match": "inna-firma.pl"},
            {"value": "1", "label": "przyklad.pl - GA4", "match": "przyklad.pl"},
        ]

    def test_matching_option_is_marked_and_moved_first(self):
        wynik = mark_suggestions(self._opcje(), "przyklad.pl", "match")

        self.assertTrue(wynik[0]["suggested"])
        self.assertEqual(wynik[0]["value"], "1")
        self.assertFalse(wynik[1]["suggested"])

    def test_subdomain_counts_as_a_match(self):
        opcje = [{"value": "1", "label": "sklep", "match": "sklep.przyklad.pl"}]

        wynik = mark_suggestions(opcje, "przyklad.pl", "match")

        self.assertTrue(wynik[0]["suggested"])

    def test_unrelated_domain_is_not_marked(self):
        opcje = [{"value": "1", "label": "obca", "match": "zupelnie-inna.pl"}]

        wynik = mark_suggestions(opcje, "przyklad.pl", "match")

        self.assertFalse(wynik[0]["suggested"])

    def test_no_domain_leaves_the_order_untouched(self):
        opcje = self._opcje()

        self.assertEqual(mark_suggestions(opcje, "", "match"), opcje)


class AssignServicesTests(TestCase):
    """Zapis wybranej usługi GA4 i witryny GSC."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="zapis@przyklad.pl",
            email="zapis@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.obcy = User.objects.create_user(
            username="obcy@przyklad.pl",
            email="obcy@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        cls.audit = Audit.objects.create(url="https://przyklad.pl/", owner=cls.user)

    def setUp(self):
        self.client.force_login(self.user)

    def test_selection_is_saved_on_the_audit(self):
        self.client.post(
            reverse("auditor:assign_google_services", args=[self.audit.pk]),
            {"ga4_property_id": "111111"},
        )

        self.audit.refresh_from_db()
        self.assertEqual(self.audit.ga4_property_id, "111111")
        # Witrynę Search Console dobiera automat; bez tokenu nie ma czym odpytać
        # Google, więc pole zostaje puste.
        self.assertEqual(self.audit.gsc_site_url, "")

    def test_empty_values_clear_the_assignment(self):
        # Puste pole to świadomy wybór "dopasuj automatycznie", nie brak danych.
        self.audit.ga4_property_id = "111111"
        self.audit.gsc_site_url = "sc-domain:przyklad.pl"
        self.audit.save()

        self.client.post(
            reverse("auditor:assign_google_services", args=[self.audit.pk]),
            {"ga4_property_id": "", "gsc_site_url": ""},
        )

        self.audit.refresh_from_db()
        self.assertIsNone(self.audit.ga4_property_id)
        self.assertEqual(self.audit.gsc_site_url, "")

    def test_get_is_rejected(self):
        response = self.client.get(
            reverse("auditor:assign_google_services", args=[self.audit.pk])
        )

        self.assertEqual(response.status_code, 405)

    def test_foreign_audit_cannot_be_reassigned(self):
        cudzy = Audit.objects.create(url="https://cudzy.pl/", owner=self.obcy)

        response = self.client.post(
            reverse("auditor:assign_google_services", args=[cudzy.pk]),
            {"ga4_property_id": "999999", "gsc_site_url": ""},
        )

        self.assertEqual(response.status_code, 404)
        cudzy.refresh_from_db()
        self.assertIsNone(cudzy.ga4_property_id)


class DisconnectTests(TestCase):
    """Odłączanie konta Google."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="odlacz@przyklad.pl",
            email="odlacz@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.obcy = User.objects.create_user(
            username="obcy2@przyklad.pl",
            email="obcy2@przyklad.pl",
            password="haslo-kontrolne-2",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_token_is_cleared_from_every_audit(self):
        # Token jest jeden na konto Google, ale zapisany przy każdym audycie -
        # wyczyszczenie jednego zostawiałoby poświadczenie w pozostałych.
        for adres in ("https://a.przyklad.pl/", "https://b.przyklad.pl/"):
            audit = Audit.objects.create(url=adres, owner=self.user)
            Audit.objects.filter(pk=audit.pk).update(
                ga4_refresh_token_encrypted="enc:v1:cokolwiek",
                ga4_account_email="jan@gmail.com",
            )

        self.client.post(reverse("auditor:google_disconnect"))

        for audit in Audit.objects.filter(owner=self.user):
            self.assertFalse(audit.ga4_refresh_token_encrypted)
            self.assertEqual(audit.ga4_account_email, "")

    def test_other_users_tokens_are_untouched(self):
        cudzy = Audit.objects.create(url="https://cudzy.pl/", owner=self.obcy)
        Audit.objects.filter(pk=cudzy.pk).update(
            ga4_refresh_token_encrypted="enc:v1:cudzy-token"
        )

        self.client.post(reverse("auditor:google_disconnect"))

        cudzy.refresh_from_db()
        self.assertEqual(cudzy.ga4_refresh_token_encrypted, "enc:v1:cudzy-token")

    def test_get_is_rejected(self):
        response = self.client.get(reverse("auditor:google_disconnect"))

        self.assertEqual(response.status_code, 405)


class AccountSwitchTests(TestCase):
    """Przełączanie konta wymusza ekran wyboru u Google."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="switch@przyklad.pl",
            email="switch@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(url="https://przyklad.pl/", owner=cls.user)

    def setUp(self):
        self.client.force_login(self.user)

    def _przechwyc_prompt(self, adres: str) -> str:
        flow = MagicMock()
        flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth", "stan")
        flow.code_verifier = "weryfikator"

        with patch("auditor.views.Flow.from_client_secrets_file", return_value=flow):
            self.client.get(adres)

        return flow.authorization_url.call_args.kwargs["prompt"]

    def test_switch_forces_the_account_chooser(self):
        prompt = self._przechwyc_prompt(
            reverse("auditor:start_ga4_auth", args=[self.audit.pk]) + "?switch=1"
        )

        # Bez "select_account" Google loguje po cichu na konto aktywne w przeglądarce.
        self.assertIn("select_account", prompt)

    def test_normal_connect_does_not_force_it(self):
        prompt = self._przechwyc_prompt(
            reverse("auditor:start_ga4_auth", args=[self.audit.pk])
        )

        self.assertEqual(prompt, "consent")


class PropertySwitchDataTests(TestCase):
    """Zmiana usługi GA4 nie może zostawić liczb poprzedniej.

    Objaw zgłoszony przez użytkownika: po wskazaniu usługi konta "universe" raport
    pokazywał dane orlen.pl. Sam zapis wyboru działał poprawnie - w bazie zostawały
    natomiast `ga4_organic_sessions`, `ga4_history`, `ga4_insights` i nazwa zdarzenia
    konwersji pobrane z poprzedniej usługi, a to one są renderowane w raporcie.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="zmiana@przyklad.pl",
            email="zmiana@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _audit_with_old_data(self) -> Audit:
        return Audit.objects.create(
            url="https://universe.pl/",
            owner=self.user,
            ga4_property_id="999999",
            ga4_organic_sessions=123456,
            ga4_history={"2026-01": 123456},
            ga4_channels_history={"organic": [1, 2, 3]},
            ga4_insights={"wniosek": "dane poprzedniej usługi"},
            ga4_selected_lead_event="zakup_orlen",
            gsc_site_url="sc-domain:orlen.pl",
            gsc_total_clicks_current=9999,
            gsc_query_commentary="komentarz o orlen.pl",
        )

    def test_posted_property_id_is_saved_exactly(self):
        audit = self._audit_with_old_data()

        self.client.post(
            reverse("auditor:assign_google_services", args=[audit.pk]),
            {"ga4_property_id": "111111", "gsc_site_url": ""},
        )

        audit.refresh_from_db()
        self.assertEqual(audit.ga4_property_id, "111111")

    def test_ga4_numbers_from_the_previous_property_are_cleared(self):
        audit = self._audit_with_old_data()

        self.client.post(
            reverse("auditor:assign_google_services", args=[audit.pk]),
            {"ga4_property_id": "111111", "gsc_site_url": "sc-domain:orlen.pl"},
        )

        audit.refresh_from_db()
        self.assertEqual(audit.ga4_organic_sessions, 0)
        self.assertEqual(audit.ga4_history, {})
        self.assertEqual(audit.ga4_channels_history, {})
        self.assertEqual(audit.ga4_insights, {})

    def test_conversion_event_from_the_previous_property_is_cleared(self):
        # Nazwa zdarzenia pochodzi z konfiguracji poprzedniej usługi i w nowej
        # najczęściej nie istnieje.
        audit = self._audit_with_old_data()

        self.client.post(
            reverse("auditor:assign_google_services", args=[audit.pk]),
            {"ga4_property_id": "111111", "gsc_site_url": "sc-domain:orlen.pl"},
        )

        audit.refresh_from_db()
        self.assertIsNone(audit.ga4_selected_lead_event)

    def test_gsc_numbers_are_cleared_when_the_site_changes(self):
        audit = self._audit_with_old_data()

        self.client.post(
            reverse("auditor:assign_google_services", args=[audit.pk]),
            {"ga4_property_id": "999999", "gsc_site_url": "sc-domain:universe.pl"},
        )

        audit.refresh_from_db()
        self.assertEqual(audit.gsc_total_clicks_current, 0)
        self.assertEqual(audit.gsc_query_commentary, "")

    def test_unchanged_selection_keeps_the_data(self):
        # Ponowne zatwierdzenie tej samej usługi nie może kasować raportu. Witrynę
        # podstawiamy tę samą, którą zwróci automat - inaczej sama zmiana witryny
        # uznałaby zapis za zmianę.
        audit = self._audit_with_old_data()

        with patch(
            "auditor.services.google_services.resolve_gsc_site",
            return_value="sc-domain:orlen.pl",
        ):
            self.client.post(
                reverse("auditor:assign_google_services", args=[audit.pk]),
                {"ga4_property_id": "999999"},
            )

        audit.refresh_from_db()
        self.assertEqual(audit.ga4_organic_sessions, 123456)
        self.assertEqual(audit.gsc_total_clicks_current, 9999)

    def test_changing_only_gsc_keeps_ga4_data(self):
        audit = self._audit_with_old_data()

        self.client.post(
            reverse("auditor:assign_google_services", args=[audit.pk]),
            {"ga4_property_id": "999999", "gsc_site_url": "sc-domain:universe.pl"},
        )

        audit.refresh_from_db()
        self.assertEqual(audit.ga4_organic_sessions, 123456)

    def test_fresh_data_is_fetched_for_the_new_property(self):
        audit = self._audit_with_old_data()
        Audit.objects.filter(pk=audit.pk).update(
            ga4_refresh_token_encrypted="jawny-token-testowy"
        )

        with patch("auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()),              patch("auditor.services.audit_service.AuditService.sync_ga4_data") as sync_ga4,              patch("auditor.services.audit_service.AuditService.sync_gsc_data"):
            self.client.post(
                reverse("auditor:assign_google_services", args=[audit.pk]),
                {"ga4_property_id": "111111", "gsc_site_url": ""},
            )

        # Pobranie musi dotyczyć usługi WYBRANEJ, a nie zapisanej wcześniej.
        self.assertEqual(sync_ga4.call_args.args[2], "111111")

    def test_google_failure_leaves_cleared_data_not_stale_data(self):
        audit = self._audit_with_old_data()
        Audit.objects.filter(pk=audit.pk).update(
            ga4_refresh_token_encrypted="jawny-token-testowy"
        )

        with patch("auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()),              patch(
                 "auditor.services.audit_service.AuditService.sync_ga4_data",
                 side_effect=Exception("Google nie odpowiada"),
             ) as sync_ga4,              patch("auditor.services.audit_service.AuditService.sync_gsc_data",
                   side_effect=Exception("Google nie odpowiada")):
            response = self.client.post(
                reverse("auditor:assign_google_services", args=[audit.pk]),
                {"ga4_property_id": "111111", "gsc_site_url": ""},
                follow=True,
            )

        audit.refresh_from_db()
        # Bez tego test przechodziłby również wtedy, gdyby pobrania w ogóle nie było.
        sync_ga4.assert_called_once()
        # Puste liczby są uczciwe; cudze nie.
        self.assertEqual(audit.ga4_organic_sessions, 0)
        self.assertEqual(audit.ga4_property_id, "111111")
        self.assertContains(response, "nie udało się pobrać danych")

    def test_event_cache_of_the_old_property_is_dropped(self):
        from django.core.cache import cache

        audit = self._audit_with_old_data()
        cache.set("ga4_events:999999", ["zakup_orlen"], 300)

        self.client.post(
            reverse("auditor:assign_google_services", args=[audit.pk]),
            {"ga4_property_id": "111111", "gsc_site_url": ""},
        )

        self.assertIsNone(cache.get("ga4_events:999999"))
