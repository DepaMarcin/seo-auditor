"""Testy walidatora adresów (`auditor.services.url_guard`).

Rozstrzygnięcie DNS jest mockowane - testy nie mogą zależeć od sieci ani od tego,
na co akurat wskazuje dana domena.
"""
from __future__ import annotations

import socket
from unittest.mock import patch

from django.test import SimpleTestCase

from auditor.services.url_guard import UnsafeUrlError, is_public_url, validate_public_url


def _resolves_to(*addresses: str):
    """Atrapa `socket.getaddrinfo` zwracająca wskazane adresy IP."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]


@patch("auditor.services.url_guard.socket.getaddrinfo", return_value=_resolves_to("93.184.216.34"))
class PublicUrlAcceptanceTests(SimpleTestCase):
    def test_accepts_https_url(self, _mock_dns):
        self.assertEqual(validate_public_url("https://example.com/strona"), "https://example.com/strona")

    def test_adds_missing_scheme(self, _mock_dns):
        self.assertEqual(validate_public_url("example.com"), "https://example.com")

    def test_strips_surrounding_whitespace(self, _mock_dns):
        self.assertEqual(validate_public_url("  https://example.com  "), "https://example.com")


class UnsafeSchemeTests(SimpleTestCase):
    """Schematy inne niż http/https - wektor stored XSS przez <iframe src>/<a href>."""

    def test_rejects_javascript_scheme(self):
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("javascript:alert(document.domain)//")

    def test_rejects_data_scheme(self):
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("data:text/html,<script>alert(1)</script>")

    def test_rejects_file_scheme(self):
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("file:///etc/passwd")

    def test_rejects_empty_url(self):
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("   ")


class SsrfProtectionTests(SimpleTestCase):
    """Adresy w sieci lokalnej - wektor SSRF (metadane chmury, panel admina, usługi wewnętrzne)."""

    def test_rejects_loopback_literal(self):
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("http://127.0.0.1:8000/admin/")

    def test_rejects_cloud_metadata_address(self):
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("http://169.254.169.254/latest/meta-data/")

    def test_rejects_private_network_literal(self):
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("http://10.0.0.5/")

    def test_rejects_ipv6_loopback(self):
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("http://[::1]/")

    @patch("auditor.services.url_guard.socket.getaddrinfo", return_value=_resolves_to("127.0.0.1"))
    def test_rejects_domain_resolving_to_loopback(self, _mock_dns):
        """Sam `URLValidator` tego nie łapie - nazwa domeny wygląda publicznie, dopiero
        rozstrzygnięcie DNS pokazuje adres lokalny."""
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("https://localtest.me")

    @patch(
        "auditor.services.url_guard.socket.getaddrinfo",
        return_value=_resolves_to("93.184.216.34", "192.168.1.10"),
    )
    def test_rejects_when_any_resolved_address_is_private(self, _mock_dns):
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("https://example.com")

    @patch("auditor.services.url_guard.socket.getaddrinfo", side_effect=socket.gaierror)
    def test_rejects_unresolvable_domain(self, _mock_dns):
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("https://nie-istnieje-taka-domena.invalid")


class IsPublicUrlTests(SimpleTestCase):
    @patch("auditor.services.url_guard.socket.getaddrinfo", return_value=_resolves_to("93.184.216.34"))
    def test_returns_true_for_public_url(self, _mock_dns):
        self.assertTrue(is_public_url("https://example.com"))

    def test_returns_false_instead_of_raising(self):
        self.assertFalse(is_public_url("http://127.0.0.1/"))
