"""Wybór usługi Search Console dla audytowanej domeny.

Objaw zgłoszony dla motivationdirect.pl: kafelek Search Console pokazywał
1 kliknięcie w 92 dniach, a GA4 dla tej samej domeny 442 sesje organiczne.

Przyczyną nie były parametry zapytania ani zakres dat: konto ma dla jednej domeny
KILKA usług (osobno "sc-domain:domena.pl", osobno prefiksy "https://domena.pl/"
i "https://www.domena.pl/"), a każda zbiera dane niezależnie. Kod brał PIERWSZĄ
pasującą z listy zwróconej przez Google, więc wynik zależał od kolejności
odpowiedzi - trafiał w prefiks bez ruchu, choć konto miało usługę domenową
z pełnymi danymi.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase

from auditor.services.google_api import (
    pick_gsc_site,
    pick_gsc_site_by_traffic,
    rank_gsc_site,
)

DOMENA = "motivationdirect.pl"

USLUGA_DOMENOWA = {"site_url": f"sc-domain:{DOMENA}"}
PREFIKS_HTTPS = {"site_url": f"https://{DOMENA}/"}
PREFIKS_HTTPS_WWW = {"site_url": f"https://www.{DOMENA}/"}
PREFIKS_HTTP = {"site_url": f"http://{DOMENA}/"}


class DomainPropertyPriorityTests(SimpleTestCase):
    """Usługa domenowa ma pierwszeństwo - to jedyna z pełnymi danymi witryny."""

    def test_the_domain_property_wins_over_a_url_prefix(self):
        wybrana = pick_gsc_site([PREFIKS_HTTPS, USLUGA_DOMENOWA], DOMENA)

        self.assertEqual(wybrana, f"sc-domain:{DOMENA}")

    def test_the_order_returned_by_google_does_not_decide(self):
        # Sedno usterki: lista z API przychodzi w dowolnej kolejności.
        for kolejnosc in (
            [USLUGA_DOMENOWA, PREFIKS_HTTPS, PREFIKS_HTTPS_WWW],
            [PREFIKS_HTTPS_WWW, PREFIKS_HTTPS, USLUGA_DOMENOWA],
            [PREFIKS_HTTPS, USLUGA_DOMENOWA, PREFIKS_HTTPS_WWW],
        ):
            with self.subTest(kolejnosc=[w["site_url"] for w in kolejnosc]):
                self.assertEqual(
                    pick_gsc_site(kolejnosc, DOMENA), f"sc-domain:{DOMENA}"
                )

    def test_https_beats_http_when_there_is_no_domain_property(self):
        wybrana = pick_gsc_site([PREFIKS_HTTP, PREFIKS_HTTPS], DOMENA)

        self.assertEqual(wybrana, f"https://{DOMENA}/")

    def test_the_www_variant_is_still_matched(self):
        wybrana = pick_gsc_site([PREFIKS_HTTPS_WWW], DOMENA)

        self.assertEqual(wybrana, f"https://www.{DOMENA}/")

    def test_a_parent_domain_property_loses_to_an_exact_one(self):
        # Audyt subdomeny: usługa dokładnie tej subdomeny opisuje ją lepiej.
        wybrana = pick_gsc_site(
            [{"site_url": "sc-domain:motivationdirect.pl"},
             {"site_url": "sc-domain:sklep.motivationdirect.pl"}],
            "sklep.motivationdirect.pl",
        )

        self.assertEqual(wybrana, "sc-domain:sklep.motivationdirect.pl")

    def test_a_foreign_domain_is_never_matched(self):
        self.assertEqual(pick_gsc_site([{"site_url": "sc-domain:innafirma.pl"}], DOMENA), "")

    def test_an_empty_list_yields_nothing(self):
        self.assertEqual(pick_gsc_site([], DOMENA), "")

    def test_entries_without_an_address_are_skipped(self):
        wybrana = pick_gsc_site([{"site_url": ""}, {}, USLUGA_DOMENOWA], DOMENA)

        self.assertEqual(wybrana, f"sc-domain:{DOMENA}")


class AmbiguousPrefixTests(SimpleTestCase):
    """Dwa prefiksy bez usługi domenowej - jedyny przypadek wyboru po konwencji."""

    def test_the_choice_is_deterministic(self):
        pierwszy = pick_gsc_site([PREFIKS_HTTPS, PREFIKS_HTTPS_WWW], DOMENA)
        drugi = pick_gsc_site([PREFIKS_HTTPS_WWW, PREFIKS_HTTPS], DOMENA)

        self.assertEqual(pierwszy, drugi)

    def test_the_ambiguity_is_recorded_in_the_log(self):
        # Bez odpytania API nie da się stwierdzić, na którym wariancie zbiera się
        # ruch. Różnica między nimi bywa całą wartością raportu, więc wybór
        # dokonany konwencją musi być widoczny.
        with self.assertLogs("auditor.services.google_api", level="WARNING") as log:
            pick_gsc_site([PREFIKS_HTTPS, PREFIKS_HTTPS_WWW], DOMENA)

        zapis = "\n".join(log.output)
        self.assertIn(DOMENA, zapis)
        self.assertIn("zgadywaniem", zapis.lower())

    def test_no_warning_when_a_domain_property_settles_it(self):
        # Przy usłudze domenowej wybór jest wnioskiem, nie konwencją - ostrzeżenie
        # byłoby wtedy szumem.
        with self.assertNoLogs("auditor.services.google_api", level="WARNING"):
            pick_gsc_site([PREFIKS_HTTPS, PREFIKS_HTTPS_WWW, USLUGA_DOMENOWA], DOMENA)

    def test_no_warning_for_a_single_candidate(self):
        with self.assertNoLogs("auditor.services.google_api", level="WARNING"):
            pick_gsc_site([PREFIKS_HTTPS], DOMENA)


class RankingKeyTests(SimpleTestCase):
    """Sam klucz sortowania - niezależnie od listy wejściowej."""

    def test_the_ranking_puts_the_domain_property_first(self):
        klucze = [
            rank_gsc_site(f"sc-domain:{DOMENA}", DOMENA),
            rank_gsc_site(f"https://{DOMENA}/", DOMENA),
            rank_gsc_site(f"http://{DOMENA}/", DOMENA),
        ]

        self.assertEqual(klucze, sorted(klucze))

    def test_the_scheme_comparison_ignores_letter_case(self):
        self.assertEqual(
            rank_gsc_site(f"HTTPS://{DOMENA}/", DOMENA)[0],
            rank_gsc_site(f"https://{DOMENA}/", DOMENA)[0],
        )
        self.assertEqual(
            rank_gsc_site(f"SC-DOMAIN:{DOMENA}", DOMENA)[0],
            rank_gsc_site(f"sc-domain:{DOMENA}", DOMENA)[0],
        )


class TrafficBasedChoiceTests(SimpleTestCase):
    """Rozstrzyganie między równorzędnymi prefiksami pomiarem ruchu.

    Liczby w `RUCH` to POMIAR z konta zgłoszonego w usterce (okres 92 dni):
    wariant bez "www." miał 1 kliknięcie i 8 wyświetleń, wariant z "www." -
    1248 kliknięć i 118653 wyświetlenia. Z samej postaci adresu tego nie widać,
    bo zależy od tego, na który host witryna przekierowuje.
    """

    RUCH = {
        f"https://{DOMENA}/": 8,
        f"https://www.{DOMENA}/": 118_653,
        f"https://www.{DOMENA}/en/": 2_936,
        f"http://{DOMENA}/": 0,
    }

    def setUp(self):
        super().setUp()
        cache.clear()

    def _usluga(self, ruch: dict | None = None, bledne: set | None = None):
        """Atrapa klienta Search Console zwracająca podaną liczbę wyświetleń."""
        ruch = self.RUCH if ruch is None else ruch
        bledne = bledne or set()
        odpytane: list[str] = []

        class _Zapytanie:
            def __init__(self, site_url):
                self._site_url = site_url

            def execute(self):
                odpytane.append(self._site_url)
                if self._site_url in bledne:
                    raise RuntimeError("API odmówiło")
                wyswietlenia = ruch.get(self._site_url)
                if wyswietlenia is None:
                    return {"rows": []}
                return {"rows": [{"clicks": 0, "impressions": wyswietlenia}]}

        class _Analityka:
            def query(self, siteUrl, body):  # noqa: N803 - nazwa z API Google
                return _Zapytanie(siteUrl)

        class _Serwis:
            def searchanalytics(self):
                return _Analityka()

        return _Serwis(), odpytane

    def _wybierz(self, adresy: list[str], **kwargs) -> tuple[str, list[str]]:
        serwis, odpytane = self._usluga(**kwargs)
        with patch("googleapiclient.discovery.build", return_value=serwis):
            wynik = pick_gsc_site_by_traffic(
                MagicMock(), [{"site_url": a} for a in adresy], DOMENA
            )
        return wynik, odpytane

    def test_the_variant_holding_the_traffic_wins(self):
        # Regresja na zgłoszoną usterkę: kod wybierał wariant z 1 kliknięciem.
        wynik, _ = self._wybierz(list(self.RUCH))

        self.assertEqual(wynik, f"https://www.{DOMENA}/")

    def test_the_result_does_not_depend_on_the_order_from_google(self):
        prosto, _ = self._wybierz(list(self.RUCH))
        odwrotnie, _ = self._wybierz(list(reversed(list(self.RUCH))))

        self.assertEqual(prosto, odwrotnie)

    def test_a_subdirectory_property_is_not_compared_with_the_root(self):
        # "https://www.domena.pl/en/" opisuje CZĘŚĆ witryny, więc ma gorszą rangę
        # i nie bierze udziału w porównaniu - nawet gdyby miało więcej wyświetleń.
        _, odpytane = self._wybierz(list(self.RUCH))

        self.assertNotIn(f"https://www.{DOMENA}/en/", odpytane)

    def test_http_variants_are_not_compared_with_https(self):
        _, odpytane = self._wybierz(list(self.RUCH))

        self.assertNotIn(f"http://{DOMENA}/", odpytane)

    def test_a_domain_property_needs_no_measurement(self):
        # Usługa domenowa obejmuje wszystko - pomiar byłby zbędnym zapytaniem.
        wynik, odpytane = self._wybierz([f"sc-domain:{DOMENA}", f"https://www.{DOMENA}/"])

        self.assertEqual(wynik, f"sc-domain:{DOMENA}")
        self.assertEqual(odpytane, [])

    def test_a_single_candidate_needs_no_measurement(self):
        wynik, odpytane = self._wybierz([f"https://www.{DOMENA}/"])

        self.assertEqual(wynik, f"https://www.{DOMENA}/")
        self.assertEqual(odpytane, [])

    def test_a_failing_candidate_does_not_block_the_choice(self):
        wynik, _ = self._wybierz(
            [f"https://{DOMENA}/", f"https://www.{DOMENA}/"],
            bledne={f"https://{DOMENA}/"},
        )

        self.assertEqual(wynik, f"https://www.{DOMENA}/")

    def test_a_total_failure_falls_back_to_the_address_form(self):
        # Brak danych nie może zablokować raportu - zostaje wybór deterministyczny.
        adresy = [f"https://{DOMENA}/", f"https://www.{DOMENA}/"]
        wynik, _ = self._wybierz(adresy, bledne=set(adresy))

        self.assertEqual(wynik, pick_gsc_site([{"site_url": a} for a in adresy], DOMENA))

    def test_zero_impressions_everywhere_still_yields_a_choice(self):
        adresy = [f"https://{DOMENA}/", f"https://www.{DOMENA}/"]
        wynik, _ = self._wybierz(adresy, ruch={a: 0 for a in adresy})

        self.assertIn(wynik, adresy)

    def test_the_measurement_is_cached(self):
        adresy = [f"https://{DOMENA}/", f"https://www.{DOMENA}/"]
        self._wybierz(adresy)

        _, odpytane = self._wybierz(adresy)

        self.assertEqual(odpytane, [])

    def test_a_changed_set_of_properties_is_measured_again(self):
        # Klucz cache'u pochodzi z listy kandydatów: po zyskaniu lub utracie usługi
        # poprzedni pomiar nie opisuje już rzeczywistości.
        self._wybierz([f"https://{DOMENA}/", f"https://www.{DOMENA}/"])

        _, odpytane = self._wybierz(
            [f"https://{DOMENA}/", f"https://www.{DOMENA}/", f"https://sklep.{DOMENA}/"]
        )

        self.assertTrue(odpytane)

    def test_the_query_asks_for_web_results_and_final_data(self):
        zapisane: list[dict] = []

        class _Zapytanie:
            def execute(self):
                return {"rows": [{"clicks": 1, "impressions": 1}]}

        class _Analityka:
            def query(self, siteUrl, body):  # noqa: N803 - nazwa z API Google
                zapisane.append(body)
                return _Zapytanie()

        class _Serwis:
            def searchanalytics(self):
                return _Analityka()

        with patch("googleapiclient.discovery.build", return_value=_Serwis()):
            pick_gsc_site_by_traffic(
                MagicMock(),
                [{"site_url": f"https://{DOMENA}/"}, {"site_url": f"https://www.{DOMENA}/"}],
                DOMENA,
            )

        self.assertTrue(zapisane)
        for body in zapisane:
            with self.subTest(body=body):
                self.assertEqual(body["type"], "web")
                self.assertEqual(body["dataState"], "final")
                # Suma dla całej usługi: rozbicie na wymiary pomija rzadkie frazy.
                self.assertEqual(body["dimensions"], [])
                self.assertNotIn("dimensionFilterGroups", body)
