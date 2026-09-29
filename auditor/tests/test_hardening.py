"""Szyfrowanie tokenów OAuth, konfiguracja sesji i ochrona logowania.

`refresh_token` Google to długoterminowe poświadczenie dające dostęp do Analytics
i Search Console klienta. Zapisany jawnie trafiałby do każdej kopii zapasowej bazy,
a przy DEBUG=True także do tracebacków.
"""
from __future__ import annotations

from io import StringIO

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from auditor.models import Audit
from auditor.services.crypto import ENCRYPTED_PREFIX, decrypt_secret, encrypt_secret

User = get_user_model()

TEST_KEY = Fernet.generate_key().decode()
TOKEN = "1//04-przykladowy-refresh-token-google"


@override_settings(TOKEN_ENCRYPTION_KEY=TEST_KEY)
class TokenEncryptionTests(TestCase):
    """Token trafia do bazy zaszyfrowany, a z obiektu wraca czytelny."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="szyfr@przyklad.pl",
            email="szyfr@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def test_token_is_stored_encrypted(self):
        audit = Audit.objects.create(url="https://przyklad.pl/", owner=self.user)
        audit.ga4_refresh_token = TOKEN
        audit.save()

        # Czytamy surową kolumnę, omijając właściwość odszyfrowującą.
        zapisane = Audit.objects.values_list(
            "ga4_refresh_token_encrypted", flat=True
        ).get(pk=audit.pk)

        self.assertTrue(zapisane.startswith(ENCRYPTED_PREFIX))
        self.assertNotIn(TOKEN, zapisane)

    def test_token_reads_back_decrypted(self):
        audit = Audit.objects.create(url="https://przyklad.pl/", owner=self.user)
        audit.ga4_refresh_token = TOKEN
        audit.save()

        odczytany = Audit.objects.get(pk=audit.pk)

        self.assertEqual(odczytany.ga4_refresh_token, TOKEN)

    def test_ciphertext_differs_between_saves(self):
        # Fernet dokłada losowy wektor inicjujący, więc ten sam token daje za każdym
        # razem inny zapis - po samym szyfrogramie nie da się poznać wspólnego tokenu.
        pierwszy = encrypt_secret(TOKEN)
        drugi = encrypt_secret(TOKEN)

        self.assertNotEqual(pierwszy, drugi)
        self.assertEqual(decrypt_secret(pierwszy), decrypt_secret(drugi))

    def test_empty_token_stays_empty(self):
        self.assertIsNone(encrypt_secret(None))
        self.assertEqual(encrypt_secret(""), "")

    def test_wrong_key_does_not_crash_the_application(self):
        zaszyfrowany = encrypt_secret(TOKEN)

        with override_settings(TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            # Użytkownik zobaczy prośbę o ponowne połączenie konta zamiast błędu 500.
            self.assertIsNone(decrypt_secret(zaszyfrowany))

    def test_plaintext_from_before_encryption_is_still_readable(self):
        # Wdrożenie szyfrowania nie może unieważnić istniejących połączeń z Google.
        self.assertEqual(decrypt_secret(TOKEN), TOKEN)


class EncryptTokensCommandTests(TestCase):
    """Komenda dozbrajająca rekordy zapisane przed dodaniem klucza."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="komenda@przyklad.pl",
            email="komenda@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def _audit_with_plaintext(self) -> Audit:
        audit = Audit.objects.create(url="https://jawny.przyklad.pl/", owner=self.user)
        # Zapis w obejściu właściwości - tak wygląda rekord sprzed wdrożenia klucza.
        Audit.objects.filter(pk=audit.pk).update(ga4_refresh_token_encrypted=TOKEN)
        return audit

    @override_settings(TOKEN_ENCRYPTION_KEY=TEST_KEY)
    def test_plaintext_token_gets_encrypted(self):
        audit = self._audit_with_plaintext()

        call_command("encrypt_tokens", stdout=StringIO())

        zapisane = Audit.objects.values_list(
            "ga4_refresh_token_encrypted", flat=True
        ).get(pk=audit.pk)
        self.assertTrue(zapisane.startswith(ENCRYPTED_PREFIX))
        self.assertEqual(Audit.objects.get(pk=audit.pk).ga4_refresh_token, TOKEN)

    @override_settings(TOKEN_ENCRYPTION_KEY=TEST_KEY)
    def test_dry_run_changes_nothing(self):
        audit = self._audit_with_plaintext()

        call_command("encrypt_tokens", "--dry-run", stdout=StringIO())

        zapisane = Audit.objects.values_list(
            "ga4_refresh_token_encrypted", flat=True
        ).get(pk=audit.pk)
        self.assertEqual(zapisane, TOKEN)

    @override_settings(TOKEN_ENCRYPTION_KEY=TEST_KEY)
    def test_already_encrypted_tokens_are_left_alone(self):
        audit = Audit.objects.create(url="https://przyklad.pl/", owner=self.user)
        audit.ga4_refresh_token = TOKEN
        audit.save()
        przed = Audit.objects.values_list(
            "ga4_refresh_token_encrypted", flat=True
        ).get(pk=audit.pk)

        call_command("encrypt_tokens", stdout=StringIO())

        po = Audit.objects.values_list(
            "ga4_refresh_token_encrypted", flat=True
        ).get(pk=audit.pk)
        self.assertEqual(przed, po)

    @override_settings(TOKEN_ENCRYPTION_KEY="")
    def test_command_refuses_to_run_without_a_key(self):
        self._audit_with_plaintext()

        with self.assertRaises(CommandError):
            call_command("encrypt_tokens", stdout=StringIO())

    @override_settings(TOKEN_ENCRYPTION_KEY=TEST_KEY)
    def test_token_value_is_not_printed(self):
        self._audit_with_plaintext()
        wyjscie = StringIO()

        call_command("encrypt_tokens", stdout=wyjscie)

        # Token w historii terminala byłby tym samym wyciekiem, co token w bazie.
        self.assertNotIn(TOKEN, wyjscie.getvalue())


class SessionSettingsTests(TestCase):
    """Konfiguracja sesji i ciasteczek."""

    def test_cookies_are_not_readable_from_javascript(self):
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(settings.CSRF_COOKIE_HTTPONLY)

    def test_session_ends_with_the_browser(self):
        self.assertTrue(settings.SESSION_EXPIRE_AT_BROWSER_CLOSE)

    def test_session_expires_after_a_day(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 86_400)

    def test_cookies_do_not_travel_cross_site(self):
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertEqual(settings.CSRF_COOKIE_SAMESITE, "Lax")

    def test_protective_headers_apply_in_every_environment(self):
        self.assertTrue(settings.SECURE_CONTENT_TYPE_NOSNIFF)
        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")

    def test_request_size_is_capped(self):
        self.assertLessEqual(settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 10 * 1024 * 1024)

    def test_session_cookie_is_marked_httponly_in_the_response(self):
        User.objects.create_user(
            username="sesja@przyklad.pl",
            email="sesja@przyklad.pl",
            password="haslo-kontrolne-1",
        )

        # Logujemy się formularzem, a nie `force_login`: ta druga metoda wstawia
        # ciasteczko z pominięciem odpowiedzi HTTP, więc nie ma na nim atrybutów.
        self.client.post("/login/", {
            "username": "sesja@przyklad.pl",
            "password": "haslo-kontrolne-1",
        })

        self.assertTrue(self.client.cookies["sessionid"]["httponly"])


@override_settings(LOGIN_RATE_LIMIT_COUNT=3, LOGIN_RATE_LIMIT_WINDOW_SECONDS=900)
class LoginThrottleTests(TestCase):
    """Zgadywanie haseł ma się opłacać jak najmniej."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="ofiara@przyklad.pl",
            email="ofiara@przyklad.pl",
            password="prawidlowe-haslo-1",
        )

    def setUp(self):
        from django.core.cache import cache

        # Licznik siedzi w cache współdzielonym między testami.
        cache.clear()

    def _zla_proba(self):
        return self.client.post("/login/", {
            "username": "ofiara@przyklad.pl",
            "password": "zle-haslo",
        })

    def test_attempts_below_the_limit_are_allowed(self):
        for _ in range(3):
            self.assertEqual(self._zla_proba().status_code, 200)

    def test_exceeding_the_limit_is_refused(self):
        for _ in range(3):
            self._zla_proba()

        response = self._zla_proba()

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Zbyt wiele prób logowania", status_code=429)

    def test_correct_password_does_not_help_after_the_limit(self):
        # Inaczej atakujący mógłby zgadywać bez końca, byle trafić w oknie.
        for _ in range(4):
            self._zla_proba()

        response = self.client.post("/login/", {
            "username": "ofiara@przyklad.pl",
            "password": "prawidlowe-haslo-1",
        })

        self.assertEqual(response.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_opening_the_login_page_does_not_count(self):
        for _ in range(10):
            self.assertEqual(self.client.get("/login/").status_code, 200)

    def test_login_works_normally_below_the_limit(self):
        self._zla_proba()

        response = self.client.post("/login/", {
            "username": "ofiara@przyklad.pl",
            "password": "prawidlowe-haslo-1",
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)


class AdminTokenExposureTests(TestCase):
    """Token OAuth nie jest edytowalny w panelu."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="admin@przyklad.pl",
            email="admin@przyklad.pl",
            password="haslo-kontrolne-3",
        )

    @override_settings(TOKEN_ENCRYPTION_KEY=TEST_KEY)
    def test_token_field_is_not_editable(self):
        audit = Audit.objects.create(url="https://przyklad.pl/", owner=self.admin)
        audit.ga4_refresh_token = TOKEN
        audit.save()
        self.client.force_login(self.admin)

        html = self.client.get(f"/admin/auditor/audit/{audit.pk}/change/").content.decode()

        self.assertNotIn('name="ga4_refresh_token_encrypted"', html)
        self.assertIn("token zaszyfrowany", html)
