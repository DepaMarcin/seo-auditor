"""PKCE i zgodność hosta w przepływie OAuth z Google.

Dwa objawy zgłoszone z logów kontenera miały JEDNĄ przyczynę:

    oauthlib.oauth2.rfc6749.errors.InvalidGrantError: (invalid_grant) Missing code verifier.
    oraz niepożądane przekierowanie na /login/ w trakcie callbacku.

Ciasteczko sesji jest przypisane do hosta. Gdy użytkownik pracuje na
http://localhost:8000, a adres powrotny wskazuje 127.0.0.1, przeglądarka nie wysyła
tam sesji: `@login_required` widzi anonima (stąd /login/), a zapisany w sesji
weryfikator PKCE jest nieosiągalny (stąd "Missing code verifier"). Dlatego adres
powrotny budujemy na hoście, na którym użytkownik faktycznie jest.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from auditor.models import Audit
from auditor.views import _build_oauth_state, _loopback_counterpart, _oauth_redirect_uri

User = get_user_model()

ADRES_POWROTNY = "http://127.0.0.1:8000/ga4/callback/"


class OAuthTestCase(TestCase):
    """Zalogowany użytkownik z jednym audytem i atrapą przepływu OAuth."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="jan@przyklad.pl", email="jan@przyklad.pl", password="haslo-kontrolne-1"
        )
        cls.audit = Audit.objects.create(url="https://przyklad.pl/", owner=cls.user)

    def setUp(self):
        self.client.force_login(self.user)

    def _flow(self, code_verifier: str | None = "weryfikator-pkce") -> MagicMock:
        flow = MagicMock()
        flow.authorization_url.return_value = ("https://accounts.google.com/auth", "stan")
        flow.code_verifier = code_verifier
        flow.credentials = MagicMock(refresh_token="token-odswiezania")
        return flow

    def _start(self, flow: MagicMock, host: str = "127.0.0.1:8000"):
        with patch("auditor.views.Flow.from_client_secrets_file", return_value=flow):
            return self.client.get(
                reverse("auditor:start_ga4_auth", args=[self.audit.pk]), HTTP_HOST=host
            )

    def _callback(self, flow: MagicMock, state: str, host: str = "127.0.0.1:8000"):
        with patch("auditor.views.Flow.from_client_secrets_file", return_value=flow), \
             patch(
                 "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
                 return_value=[],
             ), patch(
                 "auditor.services.google_api.list_ga4_properties", return_value=[]
             ), patch(
                 "auditor.services.google_api.list_gsc_sites", return_value=[]
             ), patch(
                 "auditor.services.google_api.fetch_account_email",
                 return_value="jan@gmail.com",
             ):
            return self.client.get(
                "/ga4/callback/", {"state": state, "code": "kod"}, HTTP_HOST=host
            )


class CodeVerifierTests(OAuthTestCase):
    """Weryfikator PKCE musi przeżyć podróż do Google i z powrotem."""

    def test_the_verifier_is_stored_in_the_session(self):
        # Obiekt `Flow` generujący adres autoryzacji i ten obsługujący powrót to
        # DWA różne obiekty, w dwóch różnych żądaniach - weryfikator przechodzi
        # między nimi wyłącznie przez sesję.
        self._start(self._flow())

        self.assertEqual(self.client.session["code_verifier"], "weryfikator-pkce")

    def test_the_state_is_stored_in_the_session(self):
        self._start(self._flow())

        self.assertTrue(self.client.session["ga4_oauth_state"])

    def test_the_verifier_is_handed_to_the_token_exchange(self):
        self._start(self._flow())
        flow = self._flow(code_verifier=None)

        self._callback(flow, _build_oauth_state(self.audit.pk, "losowy"))

        # To przypisanie było przedmiotem błędu: bez niego `fetch_token` wysyłał
        # żądanie bez weryfikatora, a Google odpowiadał "Missing code verifier".
        self.assertEqual(flow.code_verifier, "weryfikator-pkce")
        flow.fetch_token.assert_called_once()

    def test_a_missing_verifier_is_not_written_to_the_session(self):
        # Biblioteka może nie wygenerować weryfikatora (PKCE wyłączone).
        self._start(self._flow(code_verifier=None))

        self.assertNotIn("code_verifier", self.client.session)

    def test_a_stale_verifier_is_cleared_when_the_new_flow_has_none(self):
        # Zostawienie wartości z POPRZEDNIEJ autoryzacji jest gorsze niż jej brak:
        # Google odrzuciłby wymianę, bo weryfikator nie zgadzałby się z wysłanym.
        self._start(self._flow(code_verifier="stary-weryfikator"))
        self._start(self._flow(code_verifier=None))

        self.assertNotIn("code_verifier", self.client.session)

    def test_the_session_is_cleaned_after_a_successful_exchange(self):
        self._start(self._flow())

        self._callback(self._flow(), _build_oauth_state(self.audit.pk, "losowy"))

        for klucz in ("code_verifier", "ga4_oauth_state", "pending_audit_id", "ga4_redirect_uri"):
            with self.subTest(klucz=klucz):
                self.assertNotIn(klucz, self.client.session)

    def test_the_exchange_still_runs_when_no_verifier_was_stored(self):
        # Przepływ rozpoczęty przed tą zmianą albo po wyczyszczeniu sesji.
        flow = self._flow(code_verifier=None)

        self._callback(flow, _build_oauth_state(self.audit.pk, "losowy"))

        flow.fetch_token.assert_called_once()


class RedirectUriHostTests(OAuthTestCase):
    """Adres powrotny musi wskazywać host, na którym jest użytkownik."""

    def _uzyty_adres(self, flow: MagicMock) -> str:
        return flow.from_client_secrets_file.call_args.kwargs["redirect_uri"]

    def test_the_configured_host_is_used_as_is(self):
        with patch("auditor.views.Flow.from_client_secrets_file") as fabryka:
            fabryka.return_value = self._flow()
            self.client.get(
                reverse("auditor:start_ga4_auth", args=[self.audit.pk]),
                HTTP_HOST="127.0.0.1:8000",
            )

        self.assertEqual(fabryka.call_args.kwargs["redirect_uri"], ADRES_POWROTNY)

    def test_the_loopback_counterpart_keeps_the_user_on_their_own_host(self):
        # Sedno poprawki: użytkownik na localhost wraca na localhost, więc
        # ciasteczko sesji (a z nim weryfikator PKCE) nie przepada.
        with self.settings(ALLOWED_HOSTS=["localhost", "127.0.0.1", "testserver"]), \
             patch("auditor.views.Flow.from_client_secrets_file") as fabryka:
            fabryka.return_value = self._flow()
            self.client.get(
                reverse("auditor:start_ga4_auth", args=[self.audit.pk]),
                HTTP_HOST="localhost:8000",
            )

        self.assertEqual(
            fabryka.call_args.kwargs["redirect_uri"],
            "http://localhost:8000/ga4/callback/",
        )

    def test_an_explicitly_allowed_host_is_accepted(self):
        with self.settings(
            ALLOWED_HOSTS=["app.przyklad.pl", "testserver"],
            GA4_REDIRECT_HOSTS=["app.przyklad.pl"],
        ), patch("auditor.views.Flow.from_client_secrets_file") as fabryka:
            fabryka.return_value = self._flow()
            self.client.get(
                reverse("auditor:start_ga4_auth", args=[self.audit.pk]),
                HTTP_HOST="app.przyklad.pl",
            )

        self.assertEqual(
            fabryka.call_args.kwargs["redirect_uri"],
            "http://app.przyklad.pl/ga4/callback/",
        )

    def test_an_unknown_host_falls_back_to_the_canonical_address(self):
        # Każdy adres powrotny musi być zarejestrowany w konsoli Google, więc dla
        # hosta spoza listy zostaje adres kanoniczny - ten jest zarejestrowany.
        with self.settings(ALLOWED_HOSTS=["obcy.przyklad.pl", "testserver"]), \
             patch("auditor.views.Flow.from_client_secrets_file") as fabryka:
            fabryka.return_value = self._flow()
            self.client.get(
                reverse("auditor:start_ga4_auth", args=[self.audit.pk]),
                HTTP_HOST="obcy.przyklad.pl",
            )

        self.assertEqual(fabryka.call_args.kwargs["redirect_uri"], ADRES_POWROTNY)

    def test_an_unknown_host_leaves_a_warning_for_the_administrator(self):
        # To usterka konfiguracji: pod tym adresem powrót z Google zgubi sesję.
        with self.settings(ALLOWED_HOSTS=["obcy.przyklad.pl", "testserver"]), \
             patch("auditor.views.Flow.from_client_secrets_file") as fabryka:
            fabryka.return_value = self._flow()
            with self.assertLogs("auditor.views", level="WARNING") as log:
                self.client.get(
                    reverse("auditor:start_ga4_auth", args=[self.audit.pk]),
                    HTTP_HOST="obcy.przyklad.pl",
                )

        self.assertIn("GA4_REDIRECT_HOSTS", "\n".join(log.output))

    def test_the_callback_reuses_the_address_sent_to_google(self):
        # Adres powrotny idzie do Google DWA razy - przy autoryzacji i przy wymianie
        # kodu. Różnica między nimi to odmowa "redirect_uri_mismatch".
        with self.settings(ALLOWED_HOSTS=["localhost", "127.0.0.1", "testserver"]):
            self._start(self._flow(), host="localhost:8000")
            self.assertEqual(
                self.client.session["ga4_redirect_uri"],
                "http://localhost:8000/ga4/callback/",
            )

            with patch("auditor.views.Flow.from_client_secrets_file") as fabryka:
                fabryka.return_value = self._flow()
                with patch(
                    "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
                    return_value=[],
                ), patch(
                    "auditor.services.google_api.list_ga4_properties", return_value=[]
                ), patch(
                    "auditor.services.google_api.list_gsc_sites", return_value=[]
                ), patch(
                    "auditor.services.google_api.fetch_account_email",
                    return_value="jan@gmail.com",
                ):
                    self.client.get(
                        "/ga4/callback/",
                        {"state": _build_oauth_state(self.audit.pk, "losowy"), "code": "kod"},
                        HTTP_HOST="localhost:8000",
                    )

        self.assertEqual(
            fabryka.call_args.kwargs["redirect_uri"],
            "http://localhost:8000/ga4/callback/",
        )


class RedirectUriHelperTests(TestCase):
    """Pomocniki wyliczające adres powrotny - bez żądania HTTP."""

    def test_the_loopback_pair_is_symmetric_and_keeps_the_port(self):
        self.assertEqual(_loopback_counterpart("127.0.0.1:8000"), "localhost:8000")
        self.assertEqual(_loopback_counterpart("localhost:8000"), "127.0.0.1:8000")
        self.assertEqual(_loopback_counterpart("localhost"), "127.0.0.1")

    def test_a_regular_host_has_no_counterpart(self):
        self.assertIsNone(_loopback_counterpart("app.przyklad.pl"))
        self.assertIsNone(_loopback_counterpart("przyklad.pl:8000"))

    def test_an_unknown_host_yields_the_configured_address(self):
        zadanie = MagicMock()
        zadanie.get_host.return_value = "obcy.przyklad.pl"

        with self.settings(GA4_REDIRECT_URI=ADRES_POWROTNY):
            self.assertEqual(_oauth_redirect_uri(zadanie), ADRES_POWROTNY)

    def test_the_path_from_the_configuration_is_preserved(self):
        # Zmieniamy wyłącznie host - ścieżka i schemat pochodzą z konfiguracji.
        zadanie = MagicMock()
        zadanie.get_host.return_value = "localhost:9000"

        with self.settings(GA4_REDIRECT_URI="https://127.0.0.1:9000/inna/sciezka/"):
            self.assertEqual(
                _oauth_redirect_uri(zadanie), "https://localhost:9000/inna/sciezka/"
            )
