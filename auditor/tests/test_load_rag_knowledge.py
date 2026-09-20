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

from auditor.management.commands.load_rag_knowledge import CATEGORY_MAP, DEFAULT_CATEGORY
from auditor.models import KnowledgeDocument
from auditor.presentation import OVERVIEW_CATEGORY_ORDER
from auditor.services.rag import RAGEngine


def _entry(key: str = "lcp_hero_image", **nadpisania) -> dict:
    data = {
        "key": key,
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
    data.update(nadpisania)
    return data


class LoadRagKnowledgeTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def _write_dataset(self, entries: list[dict], name: str = "baza.json") -> str:
        path = self.directory / name
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def _run(self, entries: list[dict], **options) -> str:
        output = StringIO()
        call_command("load_rag_knowledge", path=self._write_dataset(entries), stdout=output, **options)
        return output.getvalue()

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    def test_entries_are_written_to_the_database(self):
        self._run([_entry("a"), _entry("b", category="indexation")])

        self.assertEqual(KnowledgeDocument.objects.count(), 2)
        document = KnowledgeDocument.objects.get(metadata__key="a")
        self.assertEqual(document.title, 'Obraz LCP z atrybutem loading="lazy"')

    def test_content_merges_every_substantive_field(self):
        """Pole `content` zasila wyszukiwanie wektorowe - musi nieść pełny kontekst."""
        self._run([_entry()])
        content = KnowledgeDocument.objects.get(metadata__key="lcp_hero_image").content

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
                self.assertIn(fragment, content)

    def test_action_plan_steps_are_not_numbered_twice(self):
        """Kroki mają własną numerację w źródle - dokładanie kolejnej daje \"1. Krok 1:\"."""
        self._run([_entry()])
        content = KnowledgeDocument.objects.get(metadata__key="lcp_hero_image").content

        self.assertIn("- Krok 1 (Answer-First):", content)
        self.assertNotIn("1. Krok 1", content)

    def test_metadata_holds_exactly_the_required_keys(self):
        self._run([_entry()])
        metadata_fields = KnowledgeDocument.objects.get(metadata__key="lcp_hero_image").metadata

        self.assertEqual(
            set(metadata_fields),
            {"key", "cms_framework", "original_category", "action_plan", "code_recipe", "geo_impact"},
        )
        self.assertEqual(metadata_fields["cms_framework"], "uniwersalny")
        self.assertIsInstance(metadata_fields["action_plan"], list)
        self.assertEqual(len(metadata_fields["action_plan"]), 3)


class CategoryMappingTests(TestCase):
    """Kategorie bazy wiedzy muszą trafiać do słownika używanego przez AuditService.

    Regresja: wpisy zapisane pod własnymi nazwami kategorii (schema_org, geo_llm...)
    były dla `RAGEngine.retrieve_knowledge(category=...)` nieosiągalne - pasowało
    wyłącznie "performance", i to przez zbieżność nazwy.
    """

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def _run(self, entries: list[dict], **options) -> str:
        path = self.directory / "baza.json"
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        output = StringIO()
        call_command("load_rag_knowledge", path=str(path), stdout=output, **options)
        return output.getvalue()

    def test_every_source_category_maps_to_the_audit_vocabulary(self):
        for source_category, expected_model in CATEGORY_MAP.items():
            with self.subTest(category_name=source_category):
                self._run([_entry(source_category, category=source_category)])
                document = KnowledgeDocument.objects.get(metadata__key=source_category)
                self.assertEqual(document.category, expected_model)

    def test_mapping_covers_the_audit_categories(self):
        """Odwzorowanie nie może produkować kategorii, której audyt nigdy nie zapyta."""
        audit_categories = {key for key, _ in OVERVIEW_CATEGORY_ORDER}

        self.assertEqual(set(CATEGORY_MAP.values()), audit_categories)
        self.assertIn(DEFAULT_CATEGORY, audit_categories)

    def test_original_category_is_kept_in_metadata(self):
        self._run([_entry("a", category="geo_llm")])
        document = KnowledgeDocument.objects.get(metadata__key="a")

        self.assertEqual(document.category, "structure")
        self.assertEqual(document.metadata["original_category"], "geo_llm")

    def test_unknown_category_falls_back_and_warns(self):
        output = self._run([_entry("a", category="nowa_kategoria")])
        document = KnowledgeDocument.objects.get(metadata__key="a")

        self.assertEqual(document.category, DEFAULT_CATEGORY)
        self.assertEqual(document.metadata["original_category"], "nowa_kategoria")
        self.assertIn("nowa_kategoria", output)
        self.assertIn("CATEGORY_MAP", output)

    def test_rag_engine_finds_entries_by_audit_category(self):
        """Test końcowy: to, po co całe odwzorowanie istnieje."""
        self._run([
            _entry("perf", category="performance"),
            _entry("indeks", category="indexation"),
            _entry("schema", category="schema_org"),
            _entry("eeat", category="content_eeat"),
        ])

        engine = RAGEngine()
        for category_name in ("seo", "technical", "performance", "structure"):
            with self.subTest(category_name=category_name):
                # Wyłączamy ChromaDB, żeby test sprawdzał fallback ORM, a nie stan
                # indeksu wektorowego współdzielonego między uruchomieniami.
                with patch.object(
                    type(engine), "chroma_collection",
                    new_callable=PropertyMock,
                    side_effect=RuntimeError("ChromaDB wyłączone w teście"),
                ):
                    results = engine.retrieve_knowledge("dowolny opis problemu", category=category_name)
                self.assertTrue(results, f"brak trafień dla kategorii {category_name}")
                self.assertTrue(all(d.category == category_name for d in results))

    # ------------------------------------------------------------------
    # Idempotencja
    # ------------------------------------------------------------------
    def test_rerun_updates_instead_of_duplicating(self):
        entries = [_entry("a"), _entry("b")]
        self._run(entries)
        output = self._run(entries)

        self.assertEqual(KnowledgeDocument.objects.count(), 2)
        self.assertIn("0 nowych, 2 zaktualizowanych", output)

    def test_changed_title_updates_the_existing_record(self):
        """Dopasowanie idzie po metadata.key, więc tytuł może być redagowany."""
        self._run([_entry("a")])
        pk = KnowledgeDocument.objects.get(metadata__key="a").pk

        self._run([_entry("a", title="Nowe brzmienie tytułu")])

        document = KnowledgeDocument.objects.get(metadata__key="a")
        self.assertEqual(document.pk, pk)
        self.assertEqual(document.title, "Nowe brzmienie tytułu")

    def test_documents_outside_the_file_stay_untouched(self):
        foreign_document = KnowledgeDocument.objects.create(
            title="Dokument dodany ręcznie", content="...", category="seo", metadata={}
        )

        self._run([_entry("a")])

        foreign_document.refresh_from_db()
        self.assertEqual(foreign_document.title, "Dokument dodany ręcznie")
        self.assertEqual(KnowledgeDocument.objects.count(), 2)

    # ------------------------------------------------------------------
    # Walidacja - błędny plik nie może wgrać połowy bazy wiedzy
    # ------------------------------------------------------------------
    def test_missing_file_fails_with_a_readable_error(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("load_rag_knowledge", path=str(self.directory / "nie-ma.json"))

        self.assertIn("Nie znaleziono pliku", str(ctx.exception))

    def test_malformed_json_fails_with_a_readable_error(self):
        path = self.directory / "zepsuty.json"
        path.write_text("{to nie jest json", encoding="utf-8")

        with self.assertRaises(CommandError) as ctx:
            call_command("load_rag_knowledge", path=str(path))

        self.assertIn("nie jest poprawnym JSON-em", str(ctx.exception))

    def test_entry_missing_a_field_aborts_the_whole_import(self):
        """Baza wiedzy zasila rekomendacje dla użytkownika - lepiej nie wgrać nic
        niż wgrać wpisy niekompletne."""
        niepelny = _entry("b")
        del niepelny["geo_impact"]

        with self.assertRaises(CommandError) as ctx:
            self._run([_entry("a"), niepelny])

        self.assertIn("geo_impact", str(ctx.exception))
        self.assertEqual(KnowledgeDocument.objects.count(), 0)

    def test_duplicate_key_in_the_file_is_detected(self):
        with self.assertRaises(CommandError) as ctx:
            self._run([_entry("a"), _entry("a")])

        self.assertIn("powtarza się", str(ctx.exception))
        self.assertEqual(KnowledgeDocument.objects.count(), 0)

    def test_empty_array_is_rejected(self):
        with self.assertRaises(CommandError):
            self._run([])

    # ------------------------------------------------------------------
    # Tryb próbny
    # ------------------------------------------------------------------
    def test_dry_run_writes_nothing(self):
        output = self._run([_entry("a"), _entry("b")], dry_run=True)

        self.assertEqual(KnowledgeDocument.objects.count(), 0)
        self.assertIn("2 nowych", output)

    def test_dry_run_separates_new_from_updated(self):
        self._run([_entry("a")])

        output = self._run([_entry("a"), _entry("b")], dry_run=True)

        self.assertIn("1 nowych", output)
        self.assertIn("1 do aktualizacji", output)

    def test_dry_run_warns_about_entries_outside_the_file(self):
        self._run([_entry("a"), _entry("b")])

        output = self._run([_entry("a")], dry_run=True)

        self.assertIn("spoza pliku", output)
        self.assertIn("b", output)
