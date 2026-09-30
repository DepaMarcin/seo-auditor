"""Powiązanie wyboru usług Google z właściwym audytem.

Objaw zgłoszony przez użytkownika: kliknięcie "Połącz GA4" przy audycie "enova"
kończyło się konfiguracją audytu "orlen". Przyczyną był jeden wspólny przycisk nad
listą - startował przepływ OAuth zawsze dla tego samego audytu, niezależnie od tego,
przy którym wierszu użytkownik kliknął.
"""
from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from auditor.models import Audit
from auditor.views import _audit_pk_from_state, _build_oauth_state

User = get_user_model()


class ThreeAuditsTestCase(TestCase):
    """Wspólny zestaw: trzy audyty, z których myli się zwykle pierwszy z brzegu."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="wiele@przyklad.pl",
            email="wiele@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.orlen = Audit.objects.create(url="https://orlen.pl/", owner=cls.user)
        cls.enova = Audit.objects.create(url="https://enova.pl/", owner=cls.user)
        cls.shell = Audit.objects.create(url="https://shell.pl/", owner=cls.user)

    def setUp(self):
        self.client.force_login(self.user)

    def _flow(self) -> MagicMock:
        flow = MagicMock()
        flow.authorization_url.return_value = ("https://accounts.google.com/auth", "stan")
        flow.code_verifier = "weryfikator"
        return flow


class AssignServicesBindingTests(ThreeAuditsTestCase):
    """Zapis przypisania dotyczy dokładnie tego audytu, który wskazał adres."""

    def test_selected_audit_gets_the_property(self):
        self.client.post(
            reverse("auditor:assign_google_services", args=[self.enova.pk]),
            {"ga4_property_id": "222222", "gsc_site_url": "sc-domain:enova.pl"},
        )

        self.enova.refresh_from_db()
        self.assertEqual(self.enova.ga4_property_id, "222222")
        self.assertEqual(self.enova.gsc_site_url, "sc-domain:enova.pl")

    def test_other_audits_are_untouched(self):
        self.client.post(
            reverse("auditor:assign_google_services", args=[self.enova.pk]),
            {"ga4_property_id": "222222", "gsc_site_url": "sc-domain:enova.pl"},
        )

        self.orlen.refresh_from_db()
        self.shell.refresh_from_db()
        self.assertIsNone(self.orlen.ga4_property_id)
        self.assertEqual(self.orlen.gsc_site_url, "")
        self.assertIsNone(self.shell.ga4_property_id)

    def test_redirect_leads_back_to_the_analytics_panel(self):
        response = self.client.post(
            reverse("auditor:assign_google_services", args=[self.enova.pk]),
            {"ga4_property_id": "222222", "gsc_site_url": ""},
        )

        self.assertRedirects(response, reverse("auditor:analytics"))

    def test_middle_audit_is_not_confused_with_the_first(self):
        # Najczęstszy objaw błędu: zapis trafiał do pierwszego rekordu w bazie.
        self.client.post(
            reverse("auditor:assign_google_services", args=[self.enova.pk]),
            {"ga4_property_id": "222222", "gsc_site_url": ""},
        )

        z_baza = {a.url: a.ga4_property_id for a in Audit.objects.all()}
        self.assertEqual(z_baza["https://enova.pl/"], "222222")
        self.assertIsNone(z_baza["https://orlen.pl/"])


class PanelMarkupTests(ThreeAuditsTestCase):
    """Każdy wiersz ma własne adresy - bez współdzielonych identyfikatorów."""

    def test_every_audit_has_its_own_connect_link(self):
        html = self.client.get(reverse("auditor:analytics")).content.decode()

        linki = set(re.findall(r'href="/audits/(\d+)/ga4/connect/"', html))

        self.assertEqual(
            linki,
            {str(self.orlen.pk), str(self.enova.pk), str(self.shell.pk)},
        )

    def test_connect_link_names_the_domain(self):
        html = self.client.get(reverse("auditor:analytics")).content.decode()

        self.assertIn("Połącz GA4 dla enova.pl", html)
        self.assertIn("Połącz GA4 dla orlen.pl", html)

    def test_form_actions_carry_distinct_audit_ids(self):
        Audit.objects.update(ga4_refresh_token_encrypted="jawny-token-testowy")

        with patch("auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()), \
             patch("auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"), \
             patch("auditor.services.ga4_service.GA4OAuthService.list_accessible_properties", return_value=[]), \
             patch("auditor.services.google_api.list_gsc_sites", return_value=[]):
            html = self.client.get(reverse("auditor:analytics")).content.decode()

        akcje = set(re.findall(r'action="/analytics/(\d+)/assign/"', html))

        self.assertEqual(
            akcje,
            {str(self.orlen.pk), str(self.enova.pk), str(self.shell.pk)},
        )

    def test_select_element_ids_are_unique(self):
        # Powtórzony identyfikator sprawia, że etykieta jednego wiersza steruje
        # polem innego.
        Audit.objects.update(ga4_refresh_token_encrypted="jawny-token-testowy")

        with patch("auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()), \
             patch("auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"), \
             patch("auditor.services.ga4_service.GA4OAuthService.list_accessible_properties", return_value=[]), \
             patch("auditor.services.google_api.list_gsc_sites", return_value=[]):
            html = self.client.get(reverse("auditor:analytics")).content.decode()

        identyfikatory = re.findall(r'id="(ga4-\d+|gsc-\d+)"', html)

        self.assertEqual(len(identyfikatory), len(set(identyfikatory)))
        self.assertEqual(len(identyfikatory), 6)

    def test_connected_audit_loses_its_connect_link(self):
        Audit.objects.filter(pk=self.enova.pk).update(
            ga4_refresh_token_encrypted="jawny-token-testowy"
        )

        with patch("auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()), \
             patch("auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"), \
             patch("auditor.services.ga4_service.GA4OAuthService.list_accessible_properties", return_value=[]), \
             patch("auditor.services.google_api.list_gsc_sites", return_value=[]):
            html = self.client.get(reverse("auditor:analytics")).content.decode()

        self.assertNotIn(f'href="/audits/{self.enova.pk}/ga4/connect/"', html)
        self.assertIn(f'href="/audits/{self.orlen.pk}/ga4/connect/"', html)


class OAuthStateTests(ThreeAuditsTestCase):
    """Numer audytu podróżuje w parametrze `state`, nie tylko w sesji."""

    def test_state_round_trip(self):
        state = _build_oauth_state(42, "losowy-ciag")

        self.assertTrue(state.startswith("42:"))
        self.assertEqual(_audit_pk_from_state(state), 42)

    def test_foreign_state_is_ignored(self):
        # Parametr wraca od klienta - wartość nie nasza nie może udawać numeru.
        self.assertIsNone(_audit_pk_from_state("bez-separatora"))
        self.assertIsNone(_audit_pk_from_state("abc:def"))
        self.assertIsNone(_audit_pk_from_state(""))

    def test_authorization_url_carries_the_audit_number(self):
        flow = self._flow()

        with patch("auditor.views.Flow.from_client_secrets_file", return_value=flow):
            self.client.get(reverse("auditor:start_ga4_auth", args=[self.enova.pk]))

        state = flow.authorization_url.call_args.kwargs["state"]

        self.assertEqual(_audit_pk_from_state(state), self.enova.pk)

    def test_each_start_uses_a_fresh_random_part(self):
        flow = self._flow()

        with patch("auditor.views.Flow.from_client_secrets_file", return_value=flow):
            self.client.get(reverse("auditor:start_ga4_auth", args=[self.enova.pk]))
            pierwszy = flow.authorization_url.call_args.kwargs["state"]
            self.client.get(reverse("auditor:start_ga4_auth", args=[self.enova.pk]))
            drugi = flow.authorization_url.call_args.kwargs["state"]

        self.assertNotEqual(pierwszy, drugi)


class CallbackBindingTests(ThreeAuditsTestCase):
    """Powrót z Google trafia do audytu, z którego wyszedł."""

    def _callback(self, state: str, session_audit_pk: int | None = None):
        """Symuluje powrót z Google z podanym `state`."""
        if session_audit_pk is not None:
            sesja = self.client.session
            sesja["pending_audit_id"] = session_audit_pk
            sesja.save()

        flow = MagicMock()
        flow.credentials = MagicMock(refresh_token="token-odswiezania")

        with patch("auditor.views.Flow.from_client_secrets_file", return_value=flow), \
             patch(
                 "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
                 return_value=[],
             ):
            return self.client.get("/ga4/callback/", {"state": state, "code": "kod"})

    def test_callback_uses_the_audit_from_state(self):
        state = _build_oauth_state(self.enova.pk, "losowy")

        response = self._callback(state)

        # Bez usług GA4 callback wraca do szczegółów audytu - ma to być enova.
        self.assertRedirects(
            response,
            reverse("auditor:detail", args=[self.enova.pk]),
            fetch_redirect_response=False,
        )

    def test_state_wins_over_a_stale_session_entry(self):
        # Sedno błędu: druga karta przeglądarki nadpisywała `pending_audit_id`.
        state = _build_oauth_state(self.enova.pk, "losowy")

        response = self._callback(state, session_audit_pk=self.orlen.pk)

        self.assertRedirects(
            response,
            reverse("auditor:detail", args=[self.enova.pk]),
            fetch_redirect_response=False,
        )

    def test_token_lands_on_the_audit_from_state(self):
        state = _build_oauth_state(self.enova.pk, "losowy")

        self._callback(state, session_audit_pk=self.orlen.pk)

        self.enova.refresh_from_db()
        self.orlen.refresh_from_db()
        self.assertTrue(self.enova.ga4_refresh_token_encrypted)
        self.assertFalse(self.orlen.ga4_refresh_token_encrypted)

    def test_session_still_works_for_flows_started_earlier(self):
        # Przepływy rozpoczęte przed tą zmianą mają `state` bez numeru audytu.
        response = self._callback("stary-state-bez-numeru", session_audit_pk=self.shell.pk)

        self.assertRedirects(
            response,
            reverse("auditor:detail", args=[self.shell.pk]),
            fetch_redirect_response=False,
        )

    def test_state_pointing_at_a_foreign_audit_is_refused(self):
        obcy = User.objects.create_user(
            username="obcy@przyklad.pl",
            email="obcy@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        cudzy = Audit.objects.create(url="https://cudzy.pl/", owner=obcy)

        response = self._callback(_build_oauth_state(cudzy.pk, "losowy"))

        self.assertEqual(response.status_code, 404)
        cudzy.refresh_from_db()
        self.assertFalse(cudzy.ga4_refresh_token_encrypted)


class RedirectTargetTests(TestCase):
    """Dokąd trafia użytkownik po zapisaniu usługi GA4.

    Objaw: po wyborze usługi pojawiał się komunikat o powodzeniu, ale przeglądarka
    lądowała na szczegółach pierwszego audytu w bazie zamiast na konfigurowanym.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="redirect@przyklad.pl",
            email="redirect@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.orlen = Audit.objects.create(url="https://orlen.pl/", owner=cls.user)
        cls.enova = Audit.objects.create(url="https://enova.pl/", owner=cls.user)

    def setUp(self):
        self.client.force_login(self.user)

    def test_assign_redirects_away_from_the_first_audit(self):
        response = self.client.post(
            reverse("auditor:assign_google_services", args=[self.enova.pk]),
            {"ga4_property_id": "222222", "gsc_site_url": ""},
        )

        self.assertEqual(response.status_code, 302)
        cel = response["Location"]
        self.assertIn(cel, ("/analytics/", f"/audits/{self.enova.pk}/"))
        self.assertNotEqual(cel, f"/audits/{self.orlen.pk}/")

    def test_page_after_the_redirect_shows_the_edited_domain(self):
        response = self.client.post(
            reverse("auditor:assign_google_services", args=[self.enova.pk]),
            {"ga4_property_id": "222222", "gsc_site_url": ""},
            follow=True,
        )

        self.assertContains(response, "enova.pl")

    def test_property_selection_returns_to_the_configured_audit(self):
        from django.core.cache import cache

        Audit.objects.filter(pk=self.enova.pk).update(
            ga4_refresh_token_encrypted="jawny-token-testowy"
        )
        cache.set(
            f"ga4_properties:{self.enova.pk}",
            [{"property_id": "222222", "display_name": "enova.pl"}],
            300,
        )

        # Ekran po autoryzacji zapisuje przez ten sam endpoint co panel analityki,
        # różni się wyłącznie celem powrotu.
        with patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch("auditor.services.audit_service.AuditService.sync_ga4_data"):
            response = self.client.post(
                reverse("auditor:assign_google_services", args=[self.enova.pk]),
                {"ga4_property_id": "222222", "gsc_site_url": "", "next": "detail"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/audits/{self.enova.pk}/")
        self.assertNotEqual(response["Location"], f"/audits/{self.orlen.pk}/")

    def test_selection_form_posts_to_its_own_audit(self):
        from django.core.cache import cache

        cache.set(
            f"ga4_properties:{self.enova.pk}",
            [{"property_id": "222222", "display_name": "enova.pl"}],
            300,
        )

        html = self.client.get(
            reverse("auditor:select_ga4_property", args=[self.enova.pk])
        ).content.decode()

        # Bez jawnego `action` POST szedłby na bieżący adres - a strona bywa otwarta
        # pod adresem innego audytu.
        self.assertIn(f'action="/analytics/{self.enova.pk}/assign/"', html)
        self.assertIn('name="next" value="detail"', html)

    def test_selection_page_names_the_audit_in_the_heading(self):
        from django.core.cache import cache

        cache.set(
            f"ga4_properties:{self.enova.pk}",
            [{"property_id": "222222", "display_name": "enova.pl"}],
            300,
        )

        html = self.client.get(
            reverse("auditor:select_ga4_property", args=[self.enova.pk])
        ).content.decode()

        naglowek = re.search(r"<h1>([^<]+)</h1>", html).group(1)

        self.assertIn("Konfiguracja Google Analytics dla", naglowek)
        self.assertIn("enova.pl", naglowek)
        self.assertNotIn("orlen.pl", naglowek)

    def test_no_view_redirects_to_a_hardcoded_audit(self):
        # Zabezpieczenie przed powrotem sztywnego `pk=1` albo `objects.first()`.
        from pathlib import Path

        zrodlo = Path("auditor/views.py").read_text(encoding="utf-8")

        self.assertNotIn("Audit.objects.first()", zrodlo)
        self.assertNotIn('redirect("auditor:detail", pk=1)', zrodlo)
