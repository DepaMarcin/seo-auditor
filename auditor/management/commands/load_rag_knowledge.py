"""Ładuje zanonimizowaną bazę wiedzy SEO/GEO do modelu KnowledgeDocument.

Plik `docs/audits/rag_knowledge_base.json` powstaje z audytów klienckich i jest
jedynym artefaktem z tego katalogu, który trafia do repozytorium (patrz .gitignore).
Ta komenda przenosi go do bazy danych, skąd korzysta `auditor.services.rag.RAGEngine`.

    python manage.py load_rag_knowledge
    python manage.py load_rag_knowledge --dry-run
    python manage.py load_rag_knowledge --index     # dodatkowo przebuduj indeks ChromaDB

Komenda jest idempotentna: rekordy są dopasowywane po `metadata.key`, więc ponowne
uruchomienie po edycji pliku aktualizuje istniejące wpisy zamiast tworzyć duplikaty.

Kategorie bazy wiedzy (performance/indexation/schema_org/geo_llm/architecture/
content_eeat) są przy zapisie odwzorowywane na słownik audytu (seo/technical/
performance/structure) - patrz CATEGORY_MAP. Bez tego kroku wyszukiwanie po kategorii
nie znajdowało wpisów, bo obie strony używały różnych nazw.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from auditor.models import KnowledgeDocument

DEFAULT_PATH = Path(settings.BASE_DIR) / "docs" / "audits" / "rag_knowledge_base.json"

# Pola wymagane w każdym wpisie. Brak któregokolwiek przerywa import w całości -
# baza wiedzy zasila rekomendacje pokazywane użytkownikowi, więc lepiej nie wgrać
# nic niż wgrać wpisy niekompletne.
REQUIRED_FIELDS = (
    "key", "category", "cms_framework", "title",
    "definition", "technical_cause", "action_plan", "code_recipe", "geo_impact",
)

# Odwzorowanie kategorii bazy wiedzy na słownik kategorii używany przez audyt.
#
# `AuditService._make_metric` woła `RAGEngine.retrieve_knowledge(category=...)` z jedną
# z czterech kategorii metryki (seo/technical/performance/structure), a ta wartość trafia
# zarówno do filtra `where` w ChromaDB, jak i do fallbacku ORM. Bez odwzorowania wpisy
# zapisane pod własnymi nazwami kategorii są dla silnika nieosiągalne - pasuje wyłącznie
# "performance", i to przez zbieżność nazwy.
#
# Pierwotna kategoria nie znika: trafia do `metadata["original_category"]`, więc podział
# merytoryczny bazy wiedzy pozostaje dostępny do raportowania i filtrowania.
CATEGORY_MAP = {
    "performance": "performance",
    "indexation": "technical",
    "architecture": "technical",
    "schema_org": "structure",
    "geo_llm": "structure",
    "content_eeat": "seo",
}

# Kategoria dla wpisów spoza CATEGORY_MAP. "technical" jest najszerszym workiem
# w słowniku audytu, więc nowa kategoria w pliku nie zniknie z wyszukiwania.
DEFAULT_CATEGORY = "technical"

# Nagłówki sekcji w polu `content`. Tekst trafia do wyszukiwania semantycznego,
# więc scalamy WSZYSTKIE pola merytoryczne - fragment dopasowany przez embeddingi
# ma nieść pełny kontekst problemu, a nie sam tytuł.
SECTION_HEADINGS = {
    "definition": "CO TO JEST",
    "technical_cause": "PRZYCZYNA TECHNICZNA",
    "action_plan": "PLAN DZIAŁANIA",
    "code_recipe": "PRZEPIS KODOWY",
    "geo_impact": "ZNACZENIE DLA GEO / MODELI JĘZYKOWYCH",
}


class Command(BaseCommand):
    help = "Ładuje docs/audits/rag_knowledge_base.json do modelu KnowledgeDocument."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--path",
            default=str(DEFAULT_PATH),
            help=f"Ścieżka do pliku JSON z bazą wiedzy (domyślnie: {DEFAULT_PATH}).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Pokaż, co zostałoby zapisane, bez zmian w bazie danych.",
        )
        parser.add_argument(
            "--index",
            action="store_true",
            help="Po imporcie przebuduj indeks wektorowy ChromaDB (RAGEngine.index_knowledge_base).",
        )

    def handle(self, *args, **options) -> None:
        path = Path(options["path"])
        entries = self._load_entries(path)
        self._validate_entries(entries)

        if options["dry_run"]:
            self._print_dry_run_report(entries)
            return

        created, updated = self._save_entries(entries)

        self.stdout.write(
            self.style.SUCCESS(
                f"Wczytano {len(entries)} wpis(ów) z {path.name}: "
                f"{created} nowych, {updated} zaktualizowanych."
            )
        )
        self.stdout.write(f"Łącznie w bazie wiedzy: {KnowledgeDocument.objects.count()} dokument(ów).")
        self._print_category_report(entries)

        if options["index"]:
            self._reindex()

    def _print_category_report(self, entries: list[dict]) -> None:
        """Pokazuje, ile wpisów trafiło pod każdą kategorię wyszukiwaną przez audyt."""
        distribution: dict[str, list[str]] = {}
        unknown: set[str] = set()

        for entry in entries:
            source_category = entry["category"]
            if source_category not in CATEGORY_MAP:
                unknown.add(source_category)
            target_category = CATEGORY_MAP.get(source_category, DEFAULT_CATEGORY)
            distribution.setdefault(target_category, []).append(source_category)

        self.stdout.write("\nDostępność dla AuditService (pole category):")
        for target_category in sorted(distribution):
            sources = sorted(set(distribution[target_category]))
            self.stdout.write(
                f"  {target_category:<12} {len(distribution[target_category]):>2} dokument(ów)  <- {', '.join(sources)}"
            )

        if unknown:
            self.stdout.write(
                self.style.WARNING(
                    f"Kategorie spoza CATEGORY_MAP ({', '.join(sorted(unknown))}) "
                    f"zapisano jako '{DEFAULT_CATEGORY}'. Uzupełnij odwzorowanie w komendzie."
                )
            )

    # ------------------------------------------------------------------
    # Wczytanie i walidacja
    # ------------------------------------------------------------------
    def _load_entries(self, path: Path) -> list[dict]:
        if not path.exists():
            raise CommandError(
                f"Nie znaleziono pliku {path}. Plik źródłowy nie jest wersjonowany w repozytorium "
                "- sprawdź, czy katalog docs/audits/ zawiera rag_knowledge_base.json."
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Plik {path} nie jest poprawnym JSON-em: {exc}") from exc

        if not isinstance(data, list) or not data:
            raise CommandError(f"Plik {path} musi zawierać niepustą tablicę obiektów JSON.")
        return data

    def _validate_entries(self, entries: list[dict]) -> None:
        """Waliduje komplet pól i unikalność kluczy PRZED dotknięciem bazy danych."""
        errors: list[str] = []
        seen: set[str] = set()

        for number, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                errors.append(f"pozycja {number}: oczekiwano obiektu JSON")
                continue

            missing = [pole for pole in REQUIRED_FIELDS if not entry.get(pole)]
            if missing:
                errors.append(f"pozycja {number} ({entry.get('key', '?')}): brak pól {missing}")
                continue

            if not isinstance(entry["action_plan"], list):
                errors.append(f"{entry['key']}: action_plan musi być listą kroków")

            if entry["key"] in seen:
                errors.append(f"{entry['key']}: klucz powtarza się w pliku")
            seen.add(entry["key"])

        if errors:
            raise CommandError(
                "Baza wiedzy nie przeszła walidacji - nie zapisano niczego:\n  - "
                + "\n  - ".join(errors)
            )

    # ------------------------------------------------------------------
    # Zapis
    # ------------------------------------------------------------------
    @transaction.atomic
    def _save_entries(self, entries: list[dict]) -> tuple[int, int]:
        created = updated = 0

        for entry in entries:
            # Dopasowanie po metadata.key, bo model nie ma pola unikalnego, a tytuł
            # bywa redagowany. `update_or_create` pomija przy tworzeniu parametry
            # zawierające "__", więc wartość klucza wnosi słownik `defaults`.
            _, was_created = KnowledgeDocument.objects.update_or_create(
                metadata__key=entry["key"],
                defaults={
                    "title": entry["title"],
                    "category": CATEGORY_MAP.get(entry["category"], DEFAULT_CATEGORY),
                    "content": self._build_content(entry),
                    "metadata": {
                        "key": entry["key"],
                        "cms_framework": entry["cms_framework"],
                        "original_category": entry["category"],
                        "action_plan": entry["action_plan"],
                        "code_recipe": entry["code_recipe"],
                        "geo_impact": entry["geo_impact"],
                    },
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1

        return created, updated

    def _build_content(self, entry: dict) -> str:
        """Scala pola merytoryczne w jeden czytelny blok tekstowy pod wyszukiwanie wektorowe."""
        # Kroki są już ponumerowane w źródle ("Krok 1 (Answer-First): ..."), więc
        # dokładanie własnej numeracji dałoby "1. Krok 1: ...".
        steps = "\n".join(f"- {krok}" for krok in entry["action_plan"])
        sections = [
            entry["title"],
            f"{SECTION_HEADINGS['definition']}\n{entry['definition']}",
            f"{SECTION_HEADINGS['technical_cause']}\n{entry['technical_cause']}",
            f"{SECTION_HEADINGS['action_plan']}\n{steps}",
            f"{SECTION_HEADINGS['code_recipe']}\n{entry['code_recipe']}",
            f"{SECTION_HEADINGS['geo_impact']}\n{entry['geo_impact']}",
        ]
        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Tryb próbny i indeksowanie
    # ------------------------------------------------------------------
    def _print_dry_run_report(self, entries: list[dict]) -> None:
        existing = set(
            KnowledgeDocument.objects
            .exclude(metadata__key=None)
            .values_list("metadata__key", flat=True)
        )
        new_keys = [w["key"] for w in entries if w["key"] not in existing]

        self.stdout.write(
            f"[dry-run] Wpisów w pliku: {len(entries)} "
            f"({len(new_keys)} nowych, {len(entries) - len(new_keys)} do aktualizacji)."
        )
        for entry in entries:
            marker = "+" if entry["key"] in new_keys else "~"
            target_category = CATEGORY_MAP.get(entry["category"], DEFAULT_CATEGORY)
            self.stdout.write(
                f"  {marker} [{entry['category']:<13} -> {target_category:<11}] {entry['key']}"
            )

        self._print_category_report(entries)

        orphaned = existing - {w["key"] for w in entries}
        if orphaned:
            self.stdout.write(
                self.style.WARNING(
                    f"[dry-run] W bazie są wpisy spoza pliku ({len(orphaned)}): "
                    f"{', '.join(sorted(orphaned))}. Komenda ich NIE usuwa."
                )
            )

    def _reindex(self) -> None:
        """Przebudowa indeksu wektorowego. Błąd nie unieważnia poprawnego importu do bazy."""
        from auditor.services.rag import RAGEngine

        try:
            count = RAGEngine().index_knowledge_base()
        except Exception as exc:
            self.stdout.write(
                self.style.WARNING(
                    f"Import do bazy się powiódł, ale indeksowanie ChromaDB nie: "
                    f"{type(exc).__name__}: {exc}"
                )
            )
            return

        if not count:
            self.stdout.write(
                self.style.WARNING(
                    "Indeksowanie ChromaDB nie objęło żadnego dokumentu. Import do bazy się "
                    "powiódł, ale wyszukiwanie zejdzie na fallback po kategorii - sprawdź log "
                    "auditor.services.rag."
                )
            )
            return

        self.stdout.write(self.style.SUCCESS(f"Zaindeksowano {count} dokument(ów) w ChromaDB."))
