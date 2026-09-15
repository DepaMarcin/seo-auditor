"""Testy komendy `load_rag_knowledge` - importu bazy wiedzy SEO/GEO do KnowledgeDocument.

Testy operują na tymczasowym pliku JSON, nie na `docs/audits/rag_knowledge_base.json`:
plik źródłowy nie jest wersjonowany w repozytorium (patrz .gitignore), więc zestaw
testów nie może zakładać jego obecności.
"""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import PropertyMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from auditor.management.commands.load_rag_knowledge import CATEGORY_MAP, KATEGORIA_DOMYSLNA
from auditor.models import KnowledgeDocument
from auditor.presentation import OVERVIEW_CATEGORY_ORDER
from auditor.services.rag import RAGEngine


def _wpis(klucz: str = "lcp_hero_image", **nadpisania) -> dict:
    dane = {
        "key": klucz,
        "category": "performance",
        "cms_framework": "uniwersalny",
        "title": "Obraz LCP z atrybutem loading=\"lazy\"",
        "definition": "LCP mierzy czas wyrysowania największego elementu w pierwszym oknie.",
        "technical_cause": "Szablon dokleja loading=\"lazy\" do wszystkich znaczników <img>.",
        "action_plan": [
            "Krok 1 (Answer-First): Usuń loading=\"lazy\" z obrazu LCP.",
            "Krok 2: Dodaj fetchpriority=\"high\".",
            "Krok 3: Wstaw preload w sekcji <head>.",
        ],
        "code_recipe": '<img src="/hero.avif" fetchpriority="high" width="1920" height="900" alt="...">',
        "geo_impact": "Crawler AI ma twardy budżet czasu na dokument.",
    }
    dane.update(nadpisania)
    return dane


class LoadRagKnowledgeTests(TestCase):
    def setUp(self):
        katalog = TemporaryDirectory()
        self.addCleanup(katalog.cleanup)
        self.katalog = Path(katalog.name)

    def _plik(self, wpisy: list[dict], nazwa: str = "baza.json") -> str:
        sciezka = self.katalog / nazwa
        sciezka.write_text(json.dumps(wpisy, ensure_ascii=False), encoding="utf-8")
        return str(sciezka)

    def _uruchom(self, wpisy: list[dict], **opcje) -> str:
        wyjscie = StringIO()
        call_command("load_rag_knowledge", path=self._plik(wpisy), stdout=wyjscie, **opcje)
        return wyjscie.getvalue()

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    def test_wpisy_trafiaja_do_bazy(self):
        self._uruchom([_wpis("a"), _wpis("b", category="indexation")])

        self.assertEqual(KnowledgeDocument.objects.count(), 2)
        dokument = KnowledgeDocument.objects.get(metadata__key="a")
        self.assertEqual(dokument.title, 'Obraz LCP z atrybutem loading="lazy"')

    def test_content_scala_wszystkie_pola_merytoryczne(self):
        """Pole `content` zasila wyszukiwanie wektorowe - musi nieść pełny kontekst."""
        self._uruchom([_wpis()])
        tresc = KnowledgeDocument.objects.get(metadata__key="lcp_hero_image").content

        for fragment in (
            "CO TO JEST",
            "PRZYCZYNA TECHNICZNA",
            "PLAN DZIAŁANIA",
            "PRZEPIS KODOWY",
            "ZNACZENIE DLA GEO",
            "LCP mierzy czas wyrysowania",
            "Krok 2: Dodaj fetchpriority",
            'fetchpriority="high"',
            "twardy budżet czasu",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, tresc)

    def test_kroki_planu_nie_sa_numerowane_podwojnie(self):
        """Kroki mają własną numerację w źródle - dokładanie kolejnej daje \"1. Krok 1:\"."""
        self._uruchom([_wpis()])
        tresc = KnowledgeDocument.objects.get(metadata__key="lcp_hero_image").content

        self.assertIn("- Krok 1 (Answer-First):", tresc)
        self.assertNotIn("1. Krok 1", tresc)

    def test_metadata_ma_dokladnie_wymagane_klucze(self):
        self._uruchom([_wpis()])
        metadane = KnowledgeDocument.objects.get(metadata__key="lcp_hero_image").metadata

        self.assertEqual(
            set(metadane),
            {"key", "cms_framework", "original_category", "action_plan", "code_recipe", "geo_impact"},
        )
        self.assertEqual(metadane["cms_framework"], "uniwersalny")
        self.assertIsInstance(metadane["action_plan"], list)
        self.assertEqual(len(metadane["action_plan"]), 3)


class MapowanieKategoriiTests(TestCase):
    """Kategorie bazy wiedzy muszą trafiać do słownika używanego przez AuditService.

    Regresja: wpisy zapisane pod własnymi nazwami kategorii (schema_org, geo_llm...)
    były dla `RAGEngine.retrieve_knowledge(category=...)` nieosiągalne - pasowało
    wyłącznie "performance", i to przez zbieżność nazwy.
    """

    def setUp(self):
        katalog = TemporaryDirectory()
        self.addCleanup(katalog.cleanup)
        self.katalog = Path(katalog.name)

    def _uruchom(self, wpisy: list[dict], **opcje) -> str:
        sciezka = self.katalog / "baza.json"
        sciezka.write_text(json.dumps(wpisy, ensure_ascii=False), encoding="utf-8")
        wyjscie = StringIO()
        call_command("load_rag_knowledge", path=str(sciezka), stdout=wyjscie, **opcje)
        return wyjscie.getvalue()

    def test_kazda_kategoria_zrodlowa_trafia_do_slownika_audytu(self):
        for zrodlowa, oczekiwana in CATEGORY_MAP.items():
            with self.subTest(kategoria=zrodlowa):
                self._uruchom([_wpis(zrodlowa, category=zrodlowa)])
                dokument = KnowledgeDocument.objects.get(metadata__key=zrodlowa)
                self.assertEqual(dokument.category, oczekiwana)

    def test_mapowanie_pokrywa_kategorie_audytu(self):
        """Odwzorowanie nie może produkować kategorii, której audyt nigdy nie zapyta."""
        kategorie_audytu = {klucz for klucz, _ in OVERVIEW_CATEGORY_ORDER}

        self.assertEqual(set(CATEGORY_MAP.values()), kategorie_audytu)
        self.assertIn(KATEGORIA_DOMYSLNA, kategorie_audytu)

    def test_pierwotna_kategoria_zostaje_w_metadanych(self):
        self._uruchom([_wpis("a", category="geo_llm")])
        dokument = KnowledgeDocument.objects.get(metadata__key="a")

        self.assertEqual(dokument.category, "structure")
        self.assertEqual(dokument.metadata["original_category"], "geo_llm")

    def test_nieznana_kategoria_dostaje_wartosc_domyslna_i_ostrzezenie(self):
        wyjscie = self._uruchom([_wpis("a", category="nowa_kategoria")])
        dokument = KnowledgeDocument.objects.get(metadata__key="a")

        self.assertEqual(dokument.category, KATEGORIA_DOMYSLNA)
        self.assertEqual(dokument.metadata["original_category"], "nowa_kategoria")
        self.assertIn("nowa_kategoria", wyjscie)
        self.assertIn("CATEGORY_MAP", wyjscie)

    def test_rag_engine_znajduje_wpisy_po_kategorii_audytu(self):
        """Test końcowy: to, po co całe odwzorowanie istnieje."""
        self._uruchom([
            _wpis("perf", category="performance"),
            _wpis("indeks", category="indexation"),
            _wpis("schema", category="schema_org"),
            _wpis("eeat", category="content_eeat"),
        ])

        silnik = RAGEngine()
        for kategoria in ("seo", "technical", "performance", "structure"):
            with self.subTest(kategoria=kategoria):
                # Wyłączamy ChromaDB, żeby test sprawdzał fallback ORM, a nie stan
                # indeksu wektorowego współdzielonego między uruchomieniami.
                with patch.object(
                    type(silnik), "chroma_collection",
                    new_callable=PropertyMock,
                    side_effect=RuntimeError("ChromaDB wyłączone w teście"),
                ):
                    wyniki = silnik.retrieve_knowledge("dowolny opis problemu", category=kategoria)
                self.assertTrue(wyniki, f"brak trafień dla kategorii {kategoria}")
                self.assertTrue(all(d.category == kategoria for d in wyniki))

    # ------------------------------------------------------------------
    # Idempotencja
    # ------------------------------------------------------------------
    def test_ponowne_uruchomienie_aktualizuje_zamiast_duplikowac(self):
        wpisy = [_wpis("a"), _wpis("b")]
        self._uruchom(wpisy)
        wyjscie = self._uruchom(wpisy)

        self.assertEqual(KnowledgeDocument.objects.count(), 2)
        self.assertIn("0 nowych, 2 zaktualizowanych", wyjscie)

    def test_zmiana_tytulu_w_pliku_aktualizuje_istniejacy_rekord(self):
        """Dopasowanie idzie po metadata.key, więc tytuł może być redagowany."""
        self._uruchom([_wpis("a")])
        pk = KnowledgeDocument.objects.get(metadata__key="a").pk

        self._uruchom([_wpis("a", title="Nowe brzmienie tytułu")])

        dokument = KnowledgeDocument.objects.get(metadata__key="a")
        self.assertEqual(dokument.pk, pk)
        self.assertEqual(dokument.title, "Nowe brzmienie tytułu")

    def test_dokumenty_spoza_pliku_pozostaja_nietkniete(self):
        obcy = KnowledgeDocument.objects.create(
            title="Dokument dodany ręcznie", content="...", category="seo", metadata={}
        )

        self._uruchom([_wpis("a")])

        obcy.refresh_from_db()
        self.assertEqual(obcy.title, "Dokument dodany ręcznie")
        self.assertEqual(KnowledgeDocument.objects.count(), 2)

    # ------------------------------------------------------------------
    # Walidacja - błędny plik nie może wgrać połowy bazy wiedzy
    # ------------------------------------------------------------------
    def test_brak_pliku_konczy_sie_czytelnym_bledem(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("load_rag_knowledge", path=str(self.katalog / "nie-ma.json"))

        self.assertIn("Nie znaleziono pliku", str(ctx.exception))

    def test_niepoprawny_json_konczy_sie_czytelnym_bledem(self):
        sciezka = self.katalog / "zepsuty.json"
        sciezka.write_text("{to nie jest json", encoding="utf-8")

        with self.assertRaises(CommandError) as ctx:
            call_command("load_rag_knowledge", path=str(sciezka))

        self.assertIn("nie jest poprawnym JSON-em", str(ctx.exception))

    def test_wpis_bez_wymaganego_pola_wstrzymuje_caly_import(self):
        """Baza wiedzy zasila rekomendacje dla użytkownika - lepiej nie wgrać nic
        niż wgrać wpisy niekompletne."""
        niepelny = _wpis("b")
        del niepelny["geo_impact"]

        with self.assertRaises(CommandError) as ctx:
            self._uruchom([_wpis("a"), niepelny])

        self.assertIn("geo_impact", str(ctx.exception))
        self.assertEqual(KnowledgeDocument.objects.count(), 0)

    def test_powtorzony_klucz_w_pliku_jest_wykrywany(self):
        with self.assertRaises(CommandError) as ctx:
            self._uruchom([_wpis("a"), _wpis("a")])

        self.assertIn("powtarza się", str(ctx.exception))
        self.assertEqual(KnowledgeDocument.objects.count(), 0)

    def test_pusta_tablica_jest_odrzucana(self):
        with self.assertRaises(CommandError):
            self._uruchom([])

    # ------------------------------------------------------------------
    # Tryb próbny
    # ------------------------------------------------------------------
    def test_dry_run_nie_zapisuje_niczego(self):
        wyjscie = self._uruchom([_wpis("a"), _wpis("b")], dry_run=True)

        self.assertEqual(KnowledgeDocument.objects.count(), 0)
        self.assertIn("2 nowych", wyjscie)

    def test_dry_run_rozroznia_nowe_od_aktualizowanych(self):
        self._uruchom([_wpis("a")])

        wyjscie = self._uruchom([_wpis("a"), _wpis("b")], dry_run=True)

        self.assertIn("1 nowych", wyjscie)
        self.assertIn("1 do aktualizacji", wyjscie)

    def test_dry_run_ostrzega_o_wpisach_spoza_pliku(self):
        self._uruchom([_wpis("a"), _wpis("b")])

        wyjscie = self._uruchom([_wpis("a")], dry_run=True)

        self.assertIn("spoza pliku", wyjscie)
        self.assertIn("b", wyjscie)
