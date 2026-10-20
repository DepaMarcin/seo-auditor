"""Ślepy zaułek po przełączeniu konta Google i wygląd zablokowanego przycisku.

Użytkownik na panelu `shell.pl` przełącza konto Google właśnie dlatego, że bieżące
nie ma do tej domeny dostępu. Powrót z OAuth odsyłał go na TEN SAM panel - znowu
z ostrzeżeniami i zablokowanym formularzem, czyli dokładnie tam, skąd uciekał.

Żadne wywołanie Google nie jest tu prawdziwe.
"""
from __future__ import annotations

import re

from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.models import Audit, GoogleAccount

User = get_user_model()

WLASNA = {"property_id": "222", "display_name": "shell.pl", "account_name": "Konto"}
OBCA = {"property_id": "999", "display_name": "earlystage.pl", "account_name": "Konto"}
WITRYNA_WLASNA = {
    "site_url": "sc-domain:shell.pl",
    "label": "sc-domain:shell.pl",
    "domain": "shell.pl",
}


def _body_only(html: str) -> str:
    """HTML bez arkusza stylów, ze zwiniętymi białymi znakami."""
    return re.sub(r"\s+", " ", re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL))


class CleanEntryTests(TestCase):
    """Wejście na ogólny adres `/analytics/`."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="wejscie@przyklad.pl",
            email="wejscie@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.shell = Audit.objects.create(
            url="https://shell.pl/", owner=cls.user, status=Audit.Status.COMPLETED
        )

    def setUp(self):
        self.client.force_login(self.user)
        konto, _ = GoogleAccount.objects.get_or_create(user=self.user)
        konto.refresh_token_encrypted = "jawny-token-testowy"
        konto.email = "krzysztof@gmail.com"
        konto.save()

    def _open(self, adres="/analytics/"):
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_api.fetch_account_email",
            return_value="krzysztof@gmail.com",
        ), patch(
            "auditor.services.google_api.list_authorized_domains",
            return_value=[
                {"domain": "earlystage.pl", "ga4": [OBCA], "gsc": []},
            ],
        ):
            return self.client.get(adres)

    def test_general_address_shows_the_entry_screen(self):
        # Nie ma tu wymuszania ostatniego audytu: bez `?audit=` i bez `?domain=`
        # widok renderuje czysty ekran startowy.
        response = self._open()

        self.assertTemplateUsed(response, "auditor/analytics_entry.html")

    def test_last_audit_is_not_loaded_by_itself(self):
        html = _body_only(self._open().content.decode())

        self.assertNotIn("shell.pl", html)

    def test_no_audit_identifier_is_kept_in_the_session(self):
        # Stan sesji nie uczestniczy w wyborze audytu dla analityki - sprawdzamy
        # to wprost, bo "pamiętanie ostatniego" byłoby niewidoczne w kodzie widoku.
        self._open()

        self.assertNotIn("active_audit_id", self.client.session)

    def test_explicit_parameter_still_opens_that_audit(self):
        # Zachowanie wskazania wprost nie może ucierpieć.
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_api.fetch_account_email",
            return_value="krzysztof@gmail.com",
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=[],
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=[]
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.get_available_events",
            return_value=[],
        ):
            response = self.client.get("/analytics/", {"audit": self.shell.pk})

        self.assertTemplateUsed(response, "auditor/analytics_dashboard.html")


class OauthReturnTests(TestCase):
    """Dokąd prowadzi powrót z przełączenia konta Google."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="powrot@przyklad.pl",
            email="powrot@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.shell = Audit.objects.create(
            url="https://shell.pl/", owner=cls.user, status=Audit.Status.COMPLETED
        )

    def setUp(self):
        self.client.force_login(self.user)
        konto, _ = GoogleAccount.objects.get_or_create(user=self.user)
        konto.refresh_token_encrypted = "jawny-token-testowy"
        konto.email = "krzysztof@gmail.com"
        konto.save()

    def _callback(self, wlasciwosci, witryny):
        """Powrót z Google po przełączeniu konta, z `return_to=analytics`."""
        from auditor.views import _build_oauth_state

        state = _build_oauth_state(self.shell.pk, "losowy-stan", "analytics")
        poswiadczenia = MagicMock()
        poswiadczenia.refresh_token = "nowy-token"

        przeplyw = MagicMock()
        przeplyw.credentials = poswiadczenia

        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "google_auth_oauthlib.flow.Flow.from_client_secrets_file", return_value=przeplyw
        ), patch(
            "auditor.services.google_api.fetch_account_email",
            return_value="krzysztof@gmail.com",
        ), patch(
            "auditor.services.google_api.list_ga4_properties",
            return_value=list(wlasciwosci),
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=list(witryny)
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=list(wlasciwosci),
        ):
            return self.client.get(
                reverse("auditor:ga4_callback"), {"code": "kod", "state": state}
            )

    def test_account_without_access_lands_on_the_entry_screen(self):
        # Sedno: użytkownik przełączał konto, bo poprzednie nie miało dostępu.
        # Odesłanie go na ten sam panel to powrót do punktu wyjścia.
        response = self._callback([OBCA], [])

        self.assertRedirects(
            response, reverse("auditor:analytics"), fetch_redirect_response=False
        )

    def test_the_reason_is_explained(self):
        self._callback([OBCA], [])

        komunikaty = [
            str(m) for m in self.client.get(reverse("auditor:hub")).context["messages"]
        ]
        self.assertTrue(any("nie ma ono dostępu do domeny shell.pl" in m for m in komunikaty))

    def test_account_with_access_returns_to_the_panel(self):
        # Gdy nowe konto obsługuje tę domenę, powrót na jej panel jest właściwy.
        response = self._callback([WLASNA], [])

        self.assertRedirects(
            response,
            reverse("auditor:audit_analytics", args=[self.shell.pk]),
            fetch_redirect_response=False,
        )

    def test_search_console_access_alone_is_enough_to_return(self):
        response = self._callback([OBCA], [WITRYNA_WLASNA])

        self.assertRedirects(
            response,
            reverse("auditor:audit_analytics", args=[self.shell.pk]),
            fetch_redirect_response=False,
        )

    def test_search_console_only_account_goes_to_analytics(self):
        # Brak usług GA4 nie znaczy brak analityki: Search Console nadaje się
        # niezależnie. Wcześniej ta sytuacja odsyłała na raport techniczny,
        # ignorując `return_to` - i to był ten sam rodzaj ślepego zaułka.
        response = self._callback([], [WITRYNA_WLASNA])

        self.assertRedirects(
            response,
            reverse("auditor:audit_analytics", args=[self.shell.pk]),
            fetch_redirect_response=False,
        )

    def test_search_console_only_account_is_told_what_was_found(self):
        self._callback([], [WITRYNA_WLASNA])

        komunikaty = [
            str(m) for m in self.client.get(reverse("auditor:hub")).context["messages"]
        ]
        self.assertTrue(
            any("wykryto tylko witrynę Search Console" in m for m in komunikaty),
            komunikaty,
        )

    def test_account_with_only_foreign_services_goes_to_the_entry(self):
        # Konto ma usługi, ale żadna nie dotyczy tej domeny - to jest dowód braku
        # dostępu, w odróżnieniu od pustej odpowiedzi, która może być awarią API.
        response = self._callback([OBCA], [])

        self.assertRedirects(
            response, reverse("auditor:analytics"), fetch_redirect_response=False
        )

    def test_unreadable_property_list_does_not_promise_search_console(self):
        # Puste listy mogą znaczyć awarię API. Komunikat o "wykrytej witrynie
        # Search Console" twierdziłby wtedy więcej, niż wiadomo.
        self._callback([], [])

        komunikaty = [
            str(m) for m in self.client.get(reverse("auditor:hub")).context["messages"]
        ]
        self.assertFalse(any("wykryto tylko witrynę" in m for m in komunikaty), komunikaty)
        self.assertTrue(
            any("nie udało się odczytać listy usług" in m for m in komunikaty), komunikaty
        )

    def test_scanner_flow_still_reaches_the_property_picker(self):
        # `return_to=select` (wejście ze skanera) ma nadal prowadzić do wyboru
        # usługi - ujednolicenie nie może zabrać tej ścieżki.
        from auditor.views import _build_oauth_state

        state = _build_oauth_state(self.shell.pk, "losowy-stan", "select")
        poswiadczenia = MagicMock()
        poswiadczenia.refresh_token = "nowy-token"
        przeplyw = MagicMock()
        przeplyw.credentials = poswiadczenia

        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "google_auth_oauthlib.flow.Flow.from_client_secrets_file", return_value=przeplyw
        ), patch(
            "auditor.services.google_api.fetch_account_email",
            return_value="krzysztof@gmail.com",
        ), patch(
            "auditor.services.google_api.list_ga4_properties", return_value=[WLASNA]
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=[]
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=[WLASNA],
        ):
            response = self.client.get(
                reverse("auditor:ga4_callback"), {"code": "kod", "state": state}
            )

        self.assertRedirects(
            response,
            reverse("auditor:select_ga4_property", args=[self.shell.pk]),
            fetch_redirect_response=False,
        )


class DisabledButtonStyleTests(SimpleTestCase):
    """Zablokowany przycisk musi wyglądać na zablokowany."""

    @property
    def arkusz(self) -> str:
        return (
            Path(settings.BASE_DIR) / "auditor" / "templates" / "auditor" / "base.html"
        ).read_text(encoding="utf-8")

    def _blok(self, selektor: str) -> str:
        tresc = self.arkusz
        poczatek = tresc.index(selektor + " {")
        return tresc[poczatek : tresc.index("}", poczatek)]

    def test_disabled_button_is_greyed_out(self):
        # Samo przygaszenie nie wystarczało: tło to jaskrawozielony gradient,
        # więc przycisk przy 60% krycia nadal czytał się jako aktywny.
        blok = self._blok("button:disabled")

        self.assertIn("filter: grayscale(1)", blok)
        self.assertIn("cursor: not-allowed", blok)

    def test_disabled_button_loses_its_shadow(self):
        blok = self._blok("button:disabled")

        self.assertIn("box-shadow: none", blok)

    def test_hover_does_not_animate_a_disabled_button(self):
        # `button:hover` dopasowuje się TAKŻE do przycisków `disabled`, więc bez
        # osobnego resetu taki przycisk podnosił się i świecił pod kursorem.
        blok = self._blok("button:disabled:hover")

        self.assertIn("transform: none", blok)
        self.assertIn("box-shadow: none", blok)

    def test_cursor_stays_informative(self):
        # `pointer-events: none` wyłączyłoby również kursor, więc `not-allowed`
        # przestałby się pokazywać - a to on niesie tu informację.
        self.assertNotIn("pointer-events: none", self._blok("button:disabled"))
