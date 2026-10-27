"""Pobieranie i ocena pliku /robots.txt.

Audyt raportował "Nie znaleziono pliku robots.txt" dla witryn, które ten plik mają
(np. linkhouse.pl). Przyczyną nie był ani User-Agent, ani WAF: `check_robots_txt`
wywoływało `min(self.timeout, 10.0)`, a `self.timeout` jest obiektem `httpx.Timeout`,
nie liczbą. Każde wywołanie kończyło się więc TypeError-em jeszcze przed żądaniem HTTP,
a `except Exception` w `AuditService._build_extra_checks_metrics` zamieniało tę awarię
w cichy wniosek "brak pliku". Ten sam defekt dotyczył sprawdzenia strony 404 i wagi
obrazków.

Testy pracują na atrapie `httpx.Client` - żaden z nich nie wychodzi do sieci.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx

from django.test import SimpleTestCase

from auditor.services.scraper import SEOScraper
from auditor.services.url_guard import UnsafeUrlError
from auditor.tests.test_audit_service import _service_with_mocks

# Adresy, które prawdziwy `validate_public_url` odrzuca po rozwiązaniu nazwy.
ADRESY_LOKALNE = ("127.0.0.1", "localhost", "169.254.", "10.0.0.", "192.168.")


def _atrapa_straznika(url: str) -> str:
    if any(fragment in url for fragment in ADRESY_LOKALNE):
        raise UnsafeUrlError("Adresy w sieci lokalnej nie podlegają audytowi.")
    return url


def _odpowiedz(
    status: int = 200,
    tresc: str = "User-agent: *\nDisallow: /admin/\n",
    content_type: str = "text/plain; charset=utf-8",
    location: str | None = None,
) -> httpx.Response:
    naglowki = {"content-type": content_type}
    if location:
        naglowki["location"] = location
    odpowiedz = httpx.Response(
        status,
        headers=naglowki,
        text=tresc,
        request=httpx.Request("GET", "https://przyklad.test/robots.txt"),
    )
    if location:
        # Tak samo postępuje `httpx.Client` przy `follow_redirects=False`: buduje
        # żądanie docelowe i zostawia je wywołującemu do samodzielnego wykonania.
        odpowiedz.next_request = httpx.Request("GET", location)
    return odpowiedz


class _AtrapaKlienta:
    """Minimalna atrapa `httpx.Client` zapamiętująca żądania i parametry."""

    def __init__(self, odpowiedzi: list, zapis: dict, **kwargs):
        self._odpowiedzi = list(odpowiedzi)
        self._zapis = zapis
        zapis["kwargs"] = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def request(self, method: str, url: str):
        self._zapis.setdefault("zadania", []).append((method, str(url)))
        odpowiedz = self._odpowiedzi.pop(0)
        if isinstance(odpowiedz, Exception):
            raise odpowiedz
        return odpowiedz


class RobotsFetchTests(SimpleTestCase):
    """Co `check_robots_txt` robi z poszczególnymi odpowiedziami serwera."""

    def _sprawdz(self, *odpowiedzi) -> tuple[dict, dict]:
        """Uruchamia sprawdzenie na podanych odpowiedziach. Zwraca (wynik, zapis)."""
        zapis: dict = {"zadania": []}
        with patch(
            "auditor.services.scraper.httpx.Client",
            side_effect=lambda **kwargs: _AtrapaKlienta(list(odpowiedzi), zapis, **kwargs),
        ), patch(
            # Prawdziwy strażnik rozwiązuje nazwę w DNS, a test nie ma wychodzić do
            # sieci. Atrapa zachowuje jego JEDYNE istotne tu zachowanie: odmowę dla
            # adresów w sieci lokalnej.
            "auditor.services.scraper.validate_public_url",
            side_effect=_atrapa_straznika,
        ):
            wynik = SEOScraper().check_robots_txt("https://przyklad.test")
        return wynik, zapis

    def test_a_reachable_file_is_parsed(self):
        wynik, _ = self._sprawdz(_odpowiedz())

        self.assertTrue(wynik["checked"])
        self.assertTrue(wynik["exists"])
        self.assertEqual(wynik["status_code"], 200)
        self.assertFalse(wynik["blocked"])
        # "Disallow: /admin/" to normalna konfiguracja, nie blokada całej witryny.
        self.assertFalse(wynik["disallows_all"])

    def test_404_means_the_file_really_is_missing(self):
        wynik, _ = self._sprawdz(_odpowiedz(404, tresc="Not Found"))

        self.assertTrue(wynik["checked"])
        self.assertFalse(wynik["exists"])
        self.assertFalse(wynik["blocked"])
        self.assertEqual(wynik["status_code"], 404)

    def test_403_is_reported_as_blocked_access_not_as_a_missing_file(self):
        # Odmowa WAF-a. Plik najprawdopodobniej istnieje - "brak pliku" byłby
        # fałszywą diagnozą i kierował do naprawy czegoś, co jest sprawne.
        wynik, _ = self._sprawdz(_odpowiedz(403, tresc="Forbidden"))

        self.assertTrue(wynik["checked"])
        self.assertTrue(wynik["blocked"])
        self.assertFalse(wynik["exists"])
        self.assertEqual(wynik["status_code"], 403)

    def test_server_errors_are_blocked_access_too(self):
        for status in (500, 502, 503):
            with self.subTest(status=status):
                wynik, _ = self._sprawdz(_odpowiedz(status, tresc="błąd"))

                self.assertTrue(wynik["blocked"])
                self.assertFalse(wynik["exists"])

    def test_rate_limiting_is_blocked_access(self):
        wynik, _ = self._sprawdz(_odpowiedz(429, tresc="Too Many Requests"))

        self.assertTrue(wynik["blocked"])

    def test_an_html_error_page_with_status_200_is_not_a_robots_file(self):
        wynik, _ = self._sprawdz(
            _odpowiedz(tresc="<!DOCTYPE html><html><body>404</body></html>",
                       content_type="text/html; charset=utf-8")
        )

        self.assertTrue(wynik["checked"])
        self.assertTrue(wynik["html_instead"])
        self.assertFalse(wynik["exists"])

    def test_html_is_recognised_even_when_the_content_type_lies(self):
        # Część serwerów oddaje stronę błędu z nagłówkiem `text/plain`.
        wynik, _ = self._sprawdz(
            _odpowiedz(tresc="\n  <html><head><title>Błąd</title></head></html>")
        )

        self.assertTrue(wynik["html_instead"])
        self.assertFalse(wynik["exists"])

    def test_redirects_are_followed(self):
        # Przekierowanie http -> https albo domena -> www jest regułą, nie wyjątkiem.
        # Bez podążania za nim audyt widział status 301 i uznawał plik za nieistniejący.
        wynik, zapis = self._sprawdz(
            _odpowiedz(301, tresc="", location="https://www.przyklad.test/robots.txt"),
            _odpowiedz(),
        )

        self.assertTrue(wynik["exists"])
        self.assertEqual(wynik["status_code"], 200)
        self.assertEqual(
            [url for _, url in zapis["zadania"]],
            ["https://przyklad.test/robots.txt", "https://www.przyklad.test/robots.txt"],
        )

    def test_a_redirect_into_the_local_network_is_refused(self):
        # Każdy skok przechodzi tę samą walidację co adres wejściowy: przekierowanie
        # jest najprostszą drogą do SSRF, jeśli sprawdzić wyłącznie adres pierwotny.
        wynik, zapis = self._sprawdz(
            _odpowiedz(302, tresc="", location="http://127.0.0.1:8000/robots.txt"),
            _odpowiedz(),
        )

        self.assertFalse(wynik["checked"])
        self.assertIn("error", wynik)
        # Żądanie pod adres lokalny nigdy nie poszło.
        self.assertEqual(len(zapis["zadania"]), 1)

    def test_a_connection_error_is_not_reported_as_a_missing_file(self):
        wynik, _ = self._sprawdz(httpx.ConnectTimeout("limit czasu"))

        self.assertFalse(wynik["checked"])
        self.assertFalse(wynik["exists"])
        self.assertFalse(wynik["blocked"])

    def test_the_request_introduces_itself_as_a_browser(self):
        # Systemy antybotowe odrzucają nieznane automaty regułą domyślną, dlatego
        # sprawdzenie idzie z tym samym User-Agentem co reszta audytu.
        _, zapis = self._sprawdz(_odpowiedz())
        user_agent = zapis["kwargs"]["headers"]["User-Agent"]

        self.assertIn("Mozilla/5.0", user_agent)
        self.assertIn("Chrome/", user_agent)

    def test_the_request_has_a_capped_timeout(self):
        # Regresja na przyczynę usterki: `min(self.timeout, 10.0)` podnosiło
        # TypeError, bo `self.timeout` to `httpx.Timeout`, a nie liczba.
        _, zapis = self._sprawdz(_odpowiedz())
        timeout = zapis["kwargs"]["timeout"]

        self.assertIsInstance(timeout, httpx.Timeout)
        for faza in (timeout.connect, timeout.read, timeout.write, timeout.pool):
            self.assertIsNotNone(faza)
            self.assertLessEqual(faza, 10.0)

    def test_a_long_instance_timeout_is_capped_and_a_short_one_kept(self):
        scraper = SEOScraper(timeout=60.0)
        self.assertEqual(scraper._capped_timeout(10.0).read, 10.0)

        krotki = SEOScraper(timeout=3.0)
        self.assertEqual(krotki._capped_timeout(10.0).read, 3.0)


class RobotsEvaluationTests(SimpleTestCase):
    """Jak stan pliku tłumaczy się na treść testu w raporcie."""

    def setUp(self):
        self.service = _service_with_mocks()

    def test_blocked_access_does_not_claim_the_file_is_missing(self):
        metric = self.service._evaluate_robots_txt({
            "checked": True, "exists": False, "blocked": True, "status_code": 403,
        })

        self.assertEqual(metric["status"], "warning")
        self.assertIn("odmówił dostępu", metric["value"]["note"])
        self.assertNotIn("Nie znaleziono pliku", metric["value"]["note"])

    def test_a_failed_check_is_distinguished_from_a_missing_file(self):
        metric = self.service._evaluate_robots_txt({"checked": False, "exists": False})

        self.assertEqual(metric["status"], "warning")
        self.assertIn("Nie udało się sprawdzić", metric["value"]["note"])

    def test_an_html_response_is_named_as_such(self):
        metric = self.service._evaluate_robots_txt({
            "checked": True, "exists": False, "html_instead": True,
            "status_code": 200, "content_type": "text/html",
        })

        self.assertEqual(metric["status"], "warning")
        self.assertIn("HTML", metric["value"]["note"])

    def test_a_real_missing_file_keeps_its_message(self):
        metric = self.service._evaluate_robots_txt({
            "checked": True, "exists": False, "status_code": 404,
        })

        self.assertIn("Nie znaleziono pliku robots.txt", metric["value"]["note"])

    def test_a_site_wide_block_is_still_an_error(self):
        metric = self.service._evaluate_robots_txt({
            "checked": True, "exists": True, "disallows_all": True, "status_code": 200,
        })

        self.assertEqual(metric["status"], "error")

    def test_unreadable_robots_does_not_promise_ai_bots_full_access(self):
        # Przy odmowie serwera treść pliku jest NIEZNANA - twierdzenie "boty AI mają
        # pełny dostęp" byłoby wnioskiem bez pokrycia w żadnym pomiarze.
        metric = self.service._evaluate_robots_ai_bots(
            {"checked": True, "exists": False, "blocked": True, "status_code": 403}, {}
        )

        self.assertEqual(metric["status"], "warning")
        self.assertIn("nie udało się odczytać", metric["value"]["note"].lower())

    def test_a_genuinely_missing_robots_still_means_full_access(self):
        metric = self.service._evaluate_robots_ai_bots(
            {"checked": True, "exists": False, "blocked": False, "status_code": 404}, {}
        )

        self.assertEqual(metric["status"], "ok")
