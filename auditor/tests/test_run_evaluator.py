"""Testy komendy `run_evaluator` - ewaluacji rekomendacji wzorcem LLM-as-a-Judge.

Ani silnik RAG, ani model-sędzia nie są tu prawdziwe: obie zależności są podmieniane,
więc żaden test nie wykonuje połączenia z OpenAI. Najważniejszy test w tym pliku to
`test_ocena_odroznia_odpowiedz_dobra_od_slabej` - benchmark, który wszystkiemu stawia
tę samą ocenę, nie mierzy niczego.
"""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

KOMENDA = "auditor.management.commands.run_evaluator.Command"


def _przypadek(test_id: str = "lcp_test", category: str = "performance", key: str = "lcp_key") -> dict:
    return {
        "test_id": test_id,
        "category": category,
        "key": key,
        "input_context": {
            "metric_key": "lcp",
            "value": "7.05s",
            "cms": "Nuxt.js",
            "current_value": '<img src="/hero.jpg" loading="lazy">',
        },
        "expected_criteria": {
            "required_sections": ["DIAGNOZA", "PLAN DZIAŁANIA", "RECEPTA KODOWA"],
            "key_action_keywords": ["fetchpriority", "preload"],
            "code_snippet_requirements": ['fetchpriority="high"'],
            "current_value_anchors": ["/hero.jpg"],
            "cms_expectations": "Nuxt 3: useHead() albo komponent <NuxtImg>.",
        },
    }


def _ocena(struktura=0, meryt=0, kod=0, refakt=0, cms=0, uzasadnienie="") -> str:
    """Odpowiedź sędziego dla wszystkich pięciu wymiarów."""
    return json.dumps({
        "structure_score": struktura, "content_score": meryt, "code_score": kod,
        "refactor_score": refakt, "cms_fit_score": cms, "justification": uzasadnienie,
    })


def _dokument(key: str) -> MagicMock:
    dokument = MagicMock()
    dokument.metadata = {"key": key}
    return dokument


class _SedziaAtrapa:
    """Zwraca kolejne oceny z listy; zapamiętuje otrzymane prompty."""

    def __init__(self, oceny: list[str]):
        self.oceny = list(oceny)
        self.prompty: list[str] = []

    def invoke(self, wiadomosci):
        self.prompty.append(wiadomosci[-1].content)
        odpowiedz = MagicMock()
        odpowiedz.content = self.oceny.pop(0) if self.oceny else "{}"
        return odpowiedz


class EvaluatorTestCase(SimpleTestCase):
    """Wspólna infrastruktura: tymczasowy zestaw i podmienione zależności."""

    def setUp(self):
        katalog = TemporaryDirectory()
        self.addCleanup(katalog.cleanup)
        self.katalog = Path(katalog.name)

        self.silnik = MagicMock()
        self.silnik.retrieve_knowledge.return_value = [_dokument("lcp_key")]
        self.silnik.generate_recommendation.return_value = "Dodaj fetchpriority=\"high\"."
        # Sprawny silnik odpowiada modelem wskazanym przez router - pierwszy kandydat
        # to ten sam model. Przypadek degradacji ma własny test niżej.
        self.silnik.candidate_models.side_effect = lambda model: [model]

    def _plik(self, przypadki: list[dict]) -> str:
        sciezka = self.katalog / "golden.json"
        sciezka.write_text(json.dumps(przypadki, ensure_ascii=False), encoding="utf-8")
        return str(sciezka)

    def _uruchom(self, przypadki: list[dict], oceny: list[str], **opcje) -> tuple[str, _SedziaAtrapa]:
        sedzia = _SedziaAtrapa(oceny)
        wyjscie = StringIO()
        with patch(f"{KOMENDA}._zaleznosci", return_value=(self.silnik, sedzia)):
            call_command("run_evaluator", path=self._plik(przypadki), stdout=wyjscie, **opcje)
        return wyjscie.getvalue(), sedzia


class OcenaPrzypadkuTests(EvaluatorTestCase):
    def test_ocena_odroznia_odpowiedz_dobra_od_slabej(self):
        """Benchmark stawiający wszystkiemu tę samą ocenę nie mierzy niczego."""
        wyjscie, _ = self._uruchom(
            [_przypadek("dobry"), _przypadek("slaby")],
            [
                _ocena(100, 100, 100, 100, 100, "komplet"),
                _ocena(0, 25, 0, 0, 0, "gotowiec z bazy, bez refaktoryzacji"),
            ],
        )

        self.assertIn("dobry", wyjscie)
        self.assertRegex(wyjscie, r"dobry\s+gpt-4o-mini\s+100%\s+100%\s+100%\s+100%\s+100%\s+100%")
        self.assertRegex(wyjscie, r"slaby\s+gpt-4o-mini\s+0%\s+25%\s+0%\s+0%\s+0%\s+5%")

    def test_srednia_systemu_liczona_ze_wszystkich_wymiarow(self):
        wyjscie, _ = self._uruchom(
            [_przypadek("a"), _przypadek("b")],
            [_ocena(0, 60, 90, 100, 50), _ocena(0, 40, 70, 80, 50)],
        )

        # średnie kolumn: 0, 50, 80, 90, 50 -> średnia systemu 54
        self.assertRegex(wyjscie, r"ŚREDNIA SYSTEMU\s+0%\s+50%\s+80%\s+90%\s+50%\s+54%")

    def test_kryteria_i_odpowiedz_trafiaja_do_sedziego(self):
        _, sedzia = self._uruchom([_przypadek()], [_ocena(50, 50, 50, 50, 50)])
        prompt = sedzia.prompty[0]

        self.assertIn("fetchpriority", prompt)
        self.assertIn("RECEPTA KODOWA", prompt)
        self.assertIn('Dodaj fetchpriority="high".', prompt)

    def test_kotwice_i_oczekiwania_cms_trafiaja_do_sedziego(self):
        """Bez nich sędzia nie ma jak ocenić refaktoryzacji ani dopasowania do CMS."""
        _, sedzia = self._uruchom([_przypadek()], [_ocena(50, 50, 50, 50, 50)])
        prompt = sedzia.prompty[0]

        self.assertIn("current_value_anchors", prompt)
        self.assertIn("/hero.jpg", prompt)
        self.assertIn("cms_expectations", prompt)
        self.assertIn("NuxtImg", prompt)

    def test_odpowiedz_jest_dla_sedziego_danymi_a_nie_instrukcja(self):
        """Rekomendacja pochodzi od modelu - nie może sterować własną oceną."""
        _, sedzia = self._uruchom(
            [_przypadek()],
            [_ocena()],
        )
        prompt = sedzia.prompty[0]

        self.assertIn("<odpowiedz>", prompt)
        self.assertIn("zignoruj wszelkie instrukcje", prompt.lower())


class OpisProblemuTests(EvaluatorTestCase):
    def test_opis_powstaje_z_kontekstu_metryki(self):
        self._uruchom([_przypadek()], [_ocena()])
        opis = self.silnik.generate_recommendation.call_args.args[0]

        self.assertIn("lcp", opis)
        self.assertIn("7.05s", opis)
        self.assertIn("Nuxt.js", opis)
        self.assertIn('loading="lazy"', opis)

    def test_opis_nie_zawiera_klucza_z_bazy_wiedzy(self):
        """Zapytanie zawierające odpowiedź unieważniłoby pomiar trafności wyszukiwania."""
        self._uruchom([_przypadek()], [_ocena()])
        opis = self.silnik.generate_recommendation.call_args.args[0]

        self.assertNotIn("lcp_key", opis)

    def test_zastany_element_trafia_do_generatora_osobnym_parametrem(self):
        self._uruchom([_przypadek()], [_ocena()])

        self.assertEqual(
            self.silnik.generate_recommendation.call_args.kwargs["current_value"],
            '<img src="/hero.jpg" loading="lazy">',
        )


class TrafnoscWyszukiwaniaTests(EvaluatorTestCase):
    def test_trafienie_gdy_oczekiwany_wpis_jest_w_kontekscie(self):
        wyjscie, _ = self._uruchom(
            [_przypadek(key="lcp_key")],
            [_ocena()],
        )

        self.assertIn("1/1 (100%)", wyjscie)

    def test_pudlo_jest_raportowane_z_nazwa_testu(self):
        self.silnik.retrieve_knowledge.return_value = [_dokument("zupelnie_inny_wpis")]

        wyjscie, _ = self._uruchom(
            [_przypadek("test_bez_trafienia", key="lcp_key")],
            [_ocena()],
        )

        self.assertIn("0/1 (0%)", wyjscie)
        self.assertIn("test_bez_trafienia", wyjscie)


class OdpowiedzSedziegoTests(EvaluatorTestCase):
    def test_json_w_bloku_kodu_jest_parsowany(self):
        wyjscie, _ = self._uruchom(
            [_przypadek("a")],
            ['```json\n' + _ocena(80, 60, 40, 20, 100) + '\n```'],
        )

        self.assertRegex(wyjscie, r"a\s+gpt-4o-mini\s+80%\s+60%\s+40%\s+20%\s+100%")

    def test_odpowiedz_bez_json_daje_zera_i_ostrzezenie(self):
        wyjscie, _ = self._uruchom([_przypadek("a")], ["Nie umiem tego ocenić."])

        self.assertIn("nie zwrócił JSON-a", wyjscie)
        self.assertRegex(wyjscie, r"a\s+gpt-4o-mini\s+0%\s+0%\s+0%\s+0%\s+0%")

    def test_wartosci_spoza_skali_sa_przycinane(self):
        wyjscie, _ = self._uruchom(
            [_przypadek("a")],
            ['{"structure_score": 150, "content_score": -20, "code_score": "brak",'
             ' "refactor_score": null, "cms_fit_score": []}'],
        )

        self.assertRegex(wyjscie, r"a\s+gpt-4o-mini\s+100%\s+0%\s+0%\s+0%\s+0%")


class FiltryIRaportTests(EvaluatorTestCase):
    def test_filtr_kategorii(self):
        wyjscie, _ = self._uruchom(
            [_przypadek("perf", "performance"), _przypadek("seo1", "seo")],
            [_ocena()],
            category="performance",
        )

        self.assertIn("perf", wyjscie)
        self.assertNotIn("seo1", wyjscie)

    def test_filtr_pojedynczego_testu(self):
        wyjscie, _ = self._uruchom(
            [_przypadek("a"), _przypadek("b")],
            [_ocena()],
            only="b",
        )

        self.assertIn("[1/1]", wyjscie)
        self.assertIn("b", wyjscie)

    def test_limit_ogranicza_liczbe_wywolan_api(self):
        self._uruchom(
            [_przypadek("a"), _przypadek("b"), _przypadek("c")],
            [_ocena()] * 3,
            limit=1,
        )

        self.assertEqual(self.silnik.generate_recommendation.call_count, 1)

    def test_filtr_bez_dopasowania_konczy_sie_bledem(self):
        with self.assertRaises(CommandError):
            self._uruchom(
                [_przypadek("a", "performance")],
                [_ocena()],
                category="structure",
            )

    def test_brak_pliku_konczy_sie_czytelnym_bledem(self):
        with patch(f"{KOMENDA}._zaleznosci", return_value=(self.silnik, _SedziaAtrapa([]))):
            with self.assertRaises(CommandError) as ctx:
                call_command("run_evaluator", path=str(self.katalog / "nie-ma.json"))

        self.assertIn("Nie znaleziono zestawu", str(ctx.exception))

    def test_raport_json_zawiera_podsumowanie_i_wyniki(self):
        cel = self.katalog / "raport.json"
        self._uruchom(
            [_przypadek("a"), _przypadek("b")],
            [_ocena(0, 60, 90, 100, 50), _ocena(0, 40, 70, 80, 50)],
            save=str(cel),
        )
        raport = json.loads(cel.read_text(encoding="utf-8"))

        self.assertEqual(raport["summary"]["cases"], 2)
        self.assertEqual(raport["summary"]["content_score"], 50.0)
        self.assertEqual(raport["summary"]["code_score"], 80.0)
        self.assertEqual(raport["summary"]["retrieval_hit_rate"], 100.0)
        self.assertEqual({w["test_id"] for w in raport["results"]}, {"a", "b"})
        self.assertIn("answer", raport["results"][0])


class GoldenDatasetTests(SimpleTestCase):
    """Zestaw referencyjny jest wersjonowany, więc jego kształt można egzekwować."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.conf import settings

        cls.sciezka = Path(settings.BASE_DIR) / "docs" / "eval" / "golden_dataset.json"
        cls.baza = Path(settings.BASE_DIR) / "docs" / "audits" / "rag_knowledge_base.json"

    def test_zestaw_ma_wymagana_strukture_i_pokrycie_kategorii(self):
        przypadki = json.loads(self.sciezka.read_text(encoding="utf-8"))

        self.assertGreaterEqual(len(przypadki), 12)
        for przypadek in przypadki:
            with self.subTest(test_id=przypadek.get("test_id")):
                self.assertEqual(
                    set(przypadek), {"test_id", "category", "key", "input_context", "expected_criteria"}
                )
                self.assertEqual(
                    set(przypadek["expected_criteria"]),
                    {
                        "required_sections", "key_action_keywords", "code_snippet_requirements",
                        "current_value_anchors", "cms_expectations",
                    },
                )
                self.assertIn("metric_key", przypadek["input_context"])

        rozklad = {}
        for przypadek in przypadki:
            rozklad[przypadek["category"]] = rozklad.get(przypadek["category"], 0) + 1
        self.assertEqual(set(rozklad), {"seo", "technical", "performance", "structure"})
        for kategoria, ile in rozklad.items():
            with self.subTest(kategoria=kategoria):
                self.assertGreaterEqual(ile, 3)

    def test_identyfikatory_testow_sa_unikalne(self):
        przypadki = json.loads(self.sciezka.read_text(encoding="utf-8"))
        identyfikatory = [p["test_id"] for p in przypadki]

        self.assertEqual(len(identyfikatory), len(set(identyfikatory)))

    def test_kazdy_przypadek_wskazuje_istniejacy_wpis_bazy_wiedzy(self):
        """Bez tego powiązania nie da się odróżnić błędu wyszukiwania od błędu generowania."""
        przypadki = json.loads(self.sciezka.read_text(encoding="utf-8"))
        klucze = {w["key"] for w in json.loads(self.baza.read_text(encoding="utf-8"))}

        for przypadek in przypadki:
            with self.subTest(test_id=przypadek["test_id"]):
                self.assertIn(przypadek["key"], klucze)

    def test_kotwice_pochodza_z_zastanego_elementu(self):
        """Kotwica spoza current_value nie mierzyłaby refaktoryzacji, tylko przypadek."""
        przypadki = json.loads(self.sciezka.read_text(encoding="utf-8"))

        for przypadek in przypadki:
            zastane = przypadek["input_context"]["current_value"].lower()
            kotwice = przypadek["expected_criteria"]["current_value_anchors"]

            with self.subTest(test_id=przypadek["test_id"]):
                self.assertTrue(kotwice, "przypadek bez kotwic nie zmierzy refaktoryzacji")
            for kotwica in kotwice:
                with self.subTest(test_id=przypadek["test_id"], kotwica=kotwica):
                    self.assertIn(kotwica.lower(), zastane)

    def test_kazdy_przypadek_ma_oczekiwania_wobec_technologii(self):
        przypadki = json.loads(self.sciezka.read_text(encoding="utf-8"))

        for przypadek in przypadki:
            with self.subTest(test_id=przypadek["test_id"]):
                oczekiwania = przypadek["expected_criteria"]["cms_expectations"]
                self.assertIsInstance(oczekiwania, str)
                self.assertGreater(len(oczekiwania), 40, "opis technologii zbyt ogólnikowy")

    def test_zestaw_pokrywa_kilka_technologii(self):
        """Jedna technologia w całym zestawie nie zmierzyłaby dopasowania do CMS."""
        przypadki = json.loads(self.sciezka.read_text(encoding="utf-8"))
        technologie = {p["input_context"]["cms"] for p in przypadki}

        self.assertGreaterEqual(len(technologie), 4)

    def test_kryteria_maja_pokrycie_w_zrodlowym_wpisie(self):
        """Kryterium, którego nie ma w bazie wiedzy, mierzyłoby wiedzę modelu, nie RAG."""
        przypadki = json.loads(self.sciezka.read_text(encoding="utf-8"))
        baza = {w["key"]: w for w in json.loads(self.baza.read_text(encoding="utf-8"))}

        for przypadek in przypadki:
            wpis = baza[przypadek["key"]]
            zrodlo = " ".join(
                [wpis["title"], wpis["definition"], wpis["technical_cause"],
                 " ".join(wpis["action_plan"]), wpis["code_recipe"], wpis["geo_impact"]]
            ).lower()

            for slowo in przypadek["expected_criteria"]["key_action_keywords"]:
                with self.subTest(test_id=przypadek["test_id"], slowo=slowo):
                    self.assertIn(slowo.lower(), zrodlo)

            for fragment in przypadek["expected_criteria"]["code_snippet_requirements"]:
                with self.subTest(test_id=przypadek["test_id"], kod=fragment):
                    self.assertIn(fragment.lower(), wpis["code_recipe"].lower())


class RaportowanieModeluTests(EvaluatorTestCase):
    """Raport musi rozróżniać model WYBRANY przez router od tego, który faktycznie
    odpowiedział - inaczej przy 403 sugerowałby, że rekomendacje powstały modelem,
    do którego konto nie ma dostępu."""

    def test_model_routed_i_model_used_sa_zapisywane(self):
        cel = self.katalog / "raport.json"
        self._uruchom(
            [_przypadek()],
            [_ocena(100, 100, 100, 100, 100)],
            save=str(cel),
        )
        wynik = json.loads(cel.read_text(encoding="utf-8"))["results"][0]

        # Routing jest wycofany (COMPLEX_METRICS pusty), więc metryka "lcp" też
        # trafia na model domyślny - router i wykonanie są zgodne.
        self.assertEqual(wynik["model_routed"], "gpt-4o-mini")
        self.assertEqual(wynik["model_used"], "gpt-4o-mini")

    def test_degradacja_modelu_jest_widoczna_w_raporcie(self):
        """Router chciał gpt-4o, ale silnik zszedł na tańszy - raport ma to pokazać.

        Routing jest domyślnie wycofany, więc na czas testu wymuszamy go podstawionym
        zbiorem - sprawdzamy mechanizm raportowania, nie obowiązującą konfigurację.
        """
        self.silnik.candidate_models.side_effect = lambda model: ["gpt-4o-mini"]

        with patch("auditor.services.rag.COMPLEX_METRICS", {"lcp"}):
            wyjscie, _ = self._uruchom(
                [_przypadek()],
                [_ocena(100, 100, 100, 100, 100)],
            )

        self.assertIn("gpt-4o-mini", wyjscie)
        self.assertIn("zeszło na model zapasowy", wyjscie)

    def test_calkowita_awaria_modeli_raportowana_jako_fallback(self):
        self.silnik.candidate_models.side_effect = lambda model: []

        wyjscie, _ = self._uruchom(
            [_przypadek()],
            [_ocena()],
        )

        self.assertIn("fallback (baza wiedzy)", wyjscie)

    def test_metric_key_trafia_do_generatora(self):
        """Bez tego routing w ewaluacji mierzyłby konfigurację inną niż produkcyjna."""
        self._uruchom([_przypadek()], [_ocena()])

        self.assertEqual(
            self.silnik.generate_recommendation.call_args.kwargs["metric_key"], "lcp"
        )

    def test_model_override_przechodzi_do_generatora_i_raportu(self):
        wyjscie, _ = self._uruchom(
            [_przypadek()],
            [_ocena()],
            model_override="gpt-4o-mini",
        )

        self.assertEqual(
            self.silnik.generate_recommendation.call_args.kwargs["model_override"], "gpt-4o-mini"
        )
        self.assertIn("gpt-4o-mini", wyjscie)
