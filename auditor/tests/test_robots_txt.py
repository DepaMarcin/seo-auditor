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
        self.assertTrue(wynik["error"])
        self.assertEqual(wynik["error_kind"], "unsafe_url")
        # Żądanie pod adres lokalny nigdy nie poszło - liczba podejść nie ma tu
        # znaczenia (po nieudanym pobraniu sprawdzany jest jeszcze wariant "www").
        for _, url in zapis["zadania"]:
            self.assertNotIn("127.0.0.1", url)

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


class RobotsFallbackTests(SimpleTestCase):
    """Drugi wariant hosta i nagłówki żądania."""

    def _sprawdz(self, odpowiedzi_per_host: dict) -> tuple[dict, dict]:
        """Odpowiedzi dobierane po adresie, nie po kolejności - tu liczy się host."""
        zapis: dict = {"zadania": []}

        class _KlientPerHost(_AtrapaKlienta):
            def request(self, method, url):
                self._zapis.setdefault("zadania", []).append((method, str(url)))
                odpowiedz = odpowiedzi_per_host.get(str(url))
                if odpowiedz is None:
                    odpowiedz = _odpowiedz(404, tresc="Not Found")
                if isinstance(odpowiedz, Exception):
                    raise odpowiedz
                # Prawdziwy klient wiąże odpowiedź z wysłanym żądaniem i to z niego
                # bierze się `response.url`. Bez tego atrapa podawałaby adres
                # z szablonu odpowiedzi, a nie ten faktycznie odpytany.
                odpowiedz.request = httpx.Request(method, url)
                return odpowiedz

        with patch(
            "auditor.services.scraper.httpx.Client",
            side_effect=lambda **kwargs: _KlientPerHost([], zapis, **kwargs),
        ), patch(
            "auditor.services.scraper.validate_public_url", side_effect=_atrapa_straznika
        ):
            wynik = SEOScraper().check_robots_txt("https://przyklad.test")
        return wynik, zapis

    def test_the_www_variant_is_tried_when_the_bare_host_has_no_file(self):
        # Część serwerów oddaje plik tylko pod jedną z tych nazw, a przekierowania
        # między nimi nie zawsze są ustawione.
        wynik, zapis = self._sprawdz({
            "https://www.przyklad.test/robots.txt": _odpowiedz(),
        })

        self.assertTrue(wynik["exists"])
        self.assertEqual(wynik["robots_url"], "https://www.przyklad.test/robots.txt")
        self.assertEqual(len(zapis["zadania"]), 2)

    def test_a_file_found_at_the_first_try_needs_no_second_request(self):
        wynik, zapis = self._sprawdz({
            "https://przyklad.test/robots.txt": _odpowiedz(),
        })

        self.assertTrue(wynik["exists"])
        self.assertEqual(len(zapis["zadania"]), 1)

    def test_a_blocked_response_is_not_retried_on_the_other_host(self):
        # Odmowa WAF-a dotyczy całej witryny, nie jednej nazwy hosta - drugie
        # podejście tylko dołożyłoby serwerowi żądań.
        wynik, zapis = self._sprawdz({
            "https://przyklad.test/robots.txt": _odpowiedz(403, tresc="Forbidden"),
        })

        self.assertTrue(wynik["blocked"])
        self.assertEqual(len(zapis["zadania"]), 1)

    def test_the_request_asks_for_plain_text(self):
        # Część serwerów i CDN-ów oddaje wariant HTML żądaniu bez tej deklaracji.
        _, zapis = self._sprawdz({"https://przyklad.test/robots.txt": _odpowiedz()})

        self.assertEqual(zapis["kwargs"]["headers"]["Accept"], "text/plain, */*")

    def test_network_failures_keep_their_kind(self):
        przypadki = (
            (httpx.ConnectTimeout("limit"), "timeout"),
            (httpx.ReadTimeout("limit"), "timeout"),
            (httpx.ConnectError("certificate verify failed"), "ssl"),
            (httpx.ConnectError("connection refused"), "connection"),
        )
        for wyjatek, rodzaj in przypadki:
            with self.subTest(rodzaj=rodzaj):
                wynik, _ = self._sprawdz({"https://przyklad.test/robots.txt": wyjatek})

                self.assertFalse(wynik["checked"])
                self.assertFalse(wynik["exists"])
                self.assertEqual(wynik["error_kind"], rodzaj)


class RobotsDirectiveAnalysisTests(SimpleTestCase):
    """Merytoryczna ocena treści pliku."""

    def _analiza(self, tresc: str) -> dict:
        return SEOScraper().analyze_robots_directives(tresc)

    def _teksty(self, tresc: str, severity: str) -> list[str]:
        return [
            u["text"] for u in self._analiza(tresc)["findings"] if u["severity"] == severity
        ]

    def test_rules_before_the_first_user_agent_are_reported(self):
        # Prawdziwy plik z daszer.pl: Yoast dopisuje swój blok niżej, a reguła
        # z góry pliku nie obowiązuje żadnego robota.
        analiza = self._analiza(
            "Disallow: /wp-content/uploads/lista.json\n"
            "\n"
            "User-agent: *\n"
            "Disallow:\n"
            "Sitemap: https://przyklad.pl/sitemap_index.xml\n"
        )

        self.assertEqual(analiza["orphan_rules"], ["Disallow: /wp-content/uploads/lista.json"])
        self.assertTrue(
            any("nie obowiązują żadnego robota" in u["text"] for u in analiza["findings"])
        )

    def test_a_missing_sitemap_is_a_finding(self):
        ostrzezenia = self._teksty("User-agent: *\nDisallow: /tmp/\n", "warning")

        self.assertTrue(ostrzezenia)
        self.assertIn("Sitemap", " ".join(ostrzezenia))

    def test_a_declared_sitemap_is_reported_as_correct(self):
        analiza = self._analiza(
            "User-agent: *\nDisallow:\nSitemap: https://przyklad.pl/sitemap.xml\n"
        )

        self.assertEqual(analiza["sitemaps"], ["https://przyklad.pl/sitemap.xml"])
        self.assertTrue(any(u["severity"] == "ok" for u in analiza["findings"]))

    def test_blocking_stylesheets_and_scripts_is_reported(self):
        analiza = self._analiza(
            "User-agent: *\nDisallow: /wp-content/\nSitemap: https://przyklad.pl/s.xml\n"
        )

        self.assertEqual(analiza["resource_blocks"], ["/wp-content/"])
        self.assertIn("renderowania", " ".join(u["text"] for u in analiza["findings"]))

    def test_blocking_a_single_file_inside_a_resource_directory_is_not_reported(self):
        # Odwrotność poprzedniego testu: zablokowanie jednego pliku JSON nie
        # przeszkadza renderowaniu, więc ostrzeżenie byłoby fałszywką.
        analiza = self._analiza(
            "User-agent: *\n"
            "Disallow: /wp-content/uploads/wpo-plugins-tables-list.json\n"
            "Sitemap: https://przyklad.pl/s.xml\n"
        )

        self.assertEqual(analiza["resource_blocks"], [])

    def test_a_site_wide_block_is_not_reported_as_a_resource_block(self):
        # Regresja na fałszywkę z linkhouse.pl: "Disallow: /" w grupie konkretnych
        # botów było zgłaszane jako blokada CSS/JS, bo każdy katalog z zasobami
        # zaczyna się od "/". Blokadę całej witryny raportuje osobny test.
        for sciezka in ("/", "/*"):
            with self.subTest(sciezka=sciezka):
                analiza = self._analiza(
                    f"User-agent: Scrapy\nDisallow: {sciezka}\n"
                    "Sitemap: https://przyklad.pl/s.xml\n"
                )

                self.assertEqual(analiza["resource_blocks"], [])

    def test_a_wildcard_css_rule_is_reported(self):
        analiza = self._analiza(
            "User-agent: *\nDisallow: /*.css\nSitemap: https://przyklad.pl/s.xml\n"
        )

        self.assertEqual(analiza["resource_blocks"], ["/*.css"])

    def test_the_unsupported_noindex_directive_is_reported(self):
        analiza = self._analiza(
            "User-agent: *\nNoindex: /tajne/\nDisallow:\nSitemap: https://przyklad.pl/s.xml\n"
        )

        self.assertEqual(analiza["unsupported_directives"], ["Noindex: /tajne/"])
        self.assertIn("2019", " ".join(u["text"] for u in analiza["findings"]))

    def test_a_path_without_a_leading_slash_is_reported(self):
        analiza = self._analiza(
            "User-agent: *\nDisallow: tajne/\nSitemap: https://przyklad.pl/s.xml\n"
        )

        self.assertEqual(analiza["malformed_rules"], ["Disallow: tajne/"])

    def test_crawl_delay_is_noted_without_raising_the_alarm(self):
        analiza = self._analiza(
            "User-agent: *\nCrawl-delay: 10\nDisallow:\nSitemap: https://przyklad.pl/s.xml\n"
        )

        self.assertEqual(analiza["crawl_delay"], "10")
        poziomy = {u["severity"] for u in analiza["findings"]}
        self.assertIn("info", poziomy)
        self.assertNotIn("warning", poziomy)

    def test_an_unknown_field_is_noted(self):
        analiza = self._analiza(
            "User-agent: *\nDisalow: /literowka/\nDisallow:\nSitemap: https://przyklad.pl/s.xml\n"
        )

        self.assertEqual(analiza["unknown_fields"], ["disalow"])

    def test_paths_blocked_for_every_crawler_are_listed(self):
        analiza = self._analiza(
            "User-agent: *\nDisallow: /koszyk/\nDisallow: /konto/\n"
            "User-agent: Bingbot\nDisallow: /tylko-bing/\n"
            "Sitemap: https://przyklad.pl/s.xml\n"
        )

        self.assertEqual(analiza["disallowed_paths"], ["/koszyk/", "/konto/"])
        self.assertEqual(analiza["user_agents"], ["*", "Bingbot"])
        self.assertEqual(analiza["rule_count"], 3)

    def test_an_empty_file_is_described_as_blocking_nothing(self):
        analiza = self._analiza("# tylko komentarz\n")

        self.assertEqual(analiza["rule_count"], 0)
        self.assertIn(
            "nie zawiera żadnej reguły", " ".join(u["text"] for u in analiza["findings"])
        )

    def test_a_clean_file_raises_no_warning(self):
        analiza = self._analiza(
            "User-agent: *\n"
            "Disallow: /koszyk/\n"
            "Disallow: /panel/\n"
            "Sitemap: https://przyklad.pl/sitemap.xml\n"
        )

        self.assertEqual([u for u in analiza["findings"] if u["severity"] == "warning"], [])


class RobotsVerdictTests(SimpleTestCase):
    """Jak ocena dyrektyw przekłada się na status testu w raporcie."""

    def setUp(self):
        self.service = _service_with_mocks()

    def _metric(self, tresc: str) -> dict:
        analiza = SEOScraper().analyze_robots_directives(tresc)
        return self.service._evaluate_robots_txt({
            "checked": True, "exists": True, "disallows_all": False,
            "status_code": 200, "robots_url": "https://przyklad.pl/robots.txt",
            "directives": analiza,
        })

    def test_a_file_with_problems_is_a_warning_not_an_ok(self):
        # Dotąd test kończył się na "istnieje i nie blokuje całej witryny" - plik
        # z błędną składnią dostawał zielone światło.
        metric = self._metric("User-agent: *\nDisallow: tajne/\n")

        self.assertEqual(metric["status"], "warning")
        self.assertIn("wymagają poprawy", metric["value"]["note"])

    def test_a_clean_file_is_ok(self):
        metric = self._metric(
            "User-agent: *\nDisallow: /panel/\nSitemap: https://przyklad.pl/s.xml\n"
        )

        self.assertEqual(metric["status"], "ok")

    def test_the_findings_travel_with_the_metric(self):
        # Szablon karty testu (`_test_card.html`) wypisuje je pod podsumowaniem.
        metric = self._metric("User-agent: *\nDisallow: /wp-content/\n")

        self.assertTrue(metric["value"]["findings"])
        self.assertTrue(
            all("severity" in u and "text" in u for u in metric["value"]["findings"])
        )

    def test_the_details_name_the_address_and_the_sitemap(self):
        metric = self._metric(
            "User-agent: *\nDisallow: /panel/\nSitemap: https://przyklad.pl/s.xml\n"
        )

        self.assertIn("https://przyklad.pl/robots.txt", metric["current_value"])
        self.assertIn("https://przyklad.pl/s.xml", metric["current_value"])

    def test_each_network_failure_gets_its_own_wording(self):
        przypadki = {
            "timeout": "nie odpowiedział w wyznaczonym czasie",
            "ssl": "TLS",
            "connection": "zapora",
            "unsafe_url": "poza publiczny internet",
        }
        for rodzaj, fragment in przypadki.items():
            with self.subTest(rodzaj=rodzaj):
                metric = self.service._evaluate_robots_txt({
                    "checked": False, "exists": False,
                    "error_kind": rodzaj, "error": "szczegóły",
                })

                self.assertEqual(metric["status"], "warning")
                self.assertIn(fragment, metric["value"]["note"])
