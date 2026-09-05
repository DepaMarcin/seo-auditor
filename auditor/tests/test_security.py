"""Testy szyfrowania tokenów OAuth, limitu uruchamiania audytów i zlecania zadań w tle."""
from __future__ import annotations

from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase

from auditor.models import Audit
from auditor.ratelimit import is_rate_limited
from auditor.services.crypto import ENCRYPTED_PREFIX, decrypt_secret, encrypt_secret

TEST_KEY = Fernet.generate_key().decode()


class TokenEncryptionTests(SimpleTestCase):
    def test_roundtrip_returns_original_value(self):
        with self.settings(TOKEN_ENCRYPTION_KEY=TEST_KEY):
            encrypted = encrypt_secret("1//tajny-refresh-token")

            self.assertTrue(encrypted.startswith(ENCRYPTED_PREFIX))
            self.assertNotIn("tajny-refresh-token", encrypted)
            self.assertEqual(decrypt_secret(encrypted), "1//tajny-refresh-token")

    def test_plaintext_legacy_value_is_returned_unchanged(self):
        """Tokeny zapisane przed wdrożeniem szyfrowania muszą nadal działać."""
        with self.settings(TOKEN_ENCRYPTION_KEY=TEST_KEY):
            self.assertEqual(decrypt_secret("stary-jawny-token"), "stary-jawny-token")

    def test_wrong_key_returns_none_instead_of_raising(self):
        with self.settings(TOKEN_ENCRYPTION_KEY=TEST_KEY):
            encrypted = encrypt_secret("token")

        with self.settings(TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            self.assertIsNone(decrypt_secret(encrypted))

    def test_empty_value_is_passed_through(self):
        with self.settings(TOKEN_ENCRYPTION_KEY=TEST_KEY):
            self.assertIsNone(encrypt_secret(None))
            self.assertEqual(encrypt_secret(""), "")


class AuditTokenFieldTests(TestCase):
    """Właściwość `Audit.ga4_refresh_token` szyfruje przy zapisie i odszyfrowuje przy odczycie."""

    def test_token_is_not_stored_in_plaintext(self):
        with self.settings(TOKEN_ENCRYPTION_KEY=TEST_KEY):
            audit = Audit.objects.create(url="https://example.com")
            audit.ga4_refresh_token = "1//tajny-refresh-token"
            audit.save(update_fields=["ga4_refresh_token_encrypted"])

            from_db = Audit.objects.get(pk=audit.pk)

            self.assertNotIn("tajny-refresh-token", from_db.ga4_refresh_token_encrypted)
            self.assertEqual(from_db.ga4_refresh_token, "1//tajny-refresh-token")


class RateLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username="limitowany", password="haslo12345")

    def _request(self):
        request = self.factory.post("/")
        request.user = self.user
        return request

    def test_allows_requests_up_to_limit(self):
        with self.settings(AUDIT_RATE_LIMIT_COUNT=3, AUDIT_RATE_LIMIT_WINDOW_SECONDS=3600):
            results = [is_rate_limited(self._request()) for _ in range(3)]

        self.assertEqual(results, [False, False, False])

    def test_blocks_after_limit_is_exceeded(self):
        with self.settings(AUDIT_RATE_LIMIT_COUNT=2, AUDIT_RATE_LIMIT_WINDOW_SECONDS=3600):
            for _ in range(2):
                is_rate_limited(self._request())

            self.assertTrue(is_rate_limited(self._request()))

    def test_limit_is_per_user(self):
        other = User.objects.create_user(username="inny", password="haslo12345")
        with self.settings(AUDIT_RATE_LIMIT_COUNT=1, AUDIT_RATE_LIMIT_WINDOW_SECONDS=3600):
            is_rate_limited(self._request())
            self.assertTrue(is_rate_limited(self._request()))

            other_request = self.factory.post("/")
            other_request.user = other
            self.assertFalse(is_rate_limited(other_request))

    def test_zero_limit_disables_check(self):
        with self.settings(AUDIT_RATE_LIMIT_COUNT=0):
            self.assertFalse(is_rate_limited(self._request()))


class EnqueueAuditTests(TestCase):
    """Zlecanie audytu: Celery, a przy niedostępnym brokerze - wątek tła."""

    def test_uses_celery_when_broker_is_available(self):
        from auditor import tasks

        with patch.object(tasks.run_audit_task, "delay") as mock_delay:
            result = tasks.enqueue_audit(123)

        self.assertEqual(result, "celery")
        mock_delay.assert_called_once_with(123)

    def test_falls_back_to_thread_when_broker_is_down(self):
        from auditor import tasks

        with patch.object(tasks.run_audit_task, "delay", side_effect=OSError("broker down")), \
                patch.object(tasks, "_run_in_thread") as mock_thread:
            result = tasks.enqueue_audit(123)

        self.assertEqual(result, "thread")
        mock_thread.assert_called_once_with(123)

    def test_eager_mode_runs_audit_inline(self):
        from auditor import tasks

        with self.settings(CELERY_TASK_ALWAYS_EAGER=True), \
                patch.object(tasks, "_run_audit_now") as mock_run:
            result = tasks.enqueue_audit(7)

        self.assertEqual(result, "eager")
        mock_run.assert_called_once_with(7)
