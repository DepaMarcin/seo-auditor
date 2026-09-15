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

DOMYSLNA_SCIEZKA = Path(settings.BASE_DIR) / "docs" / "audits" / "rag_knowledge_base.json"

# Pola wymagane w każdym wpisie. Brak któregokolwiek przerywa import w całości -
# baza wiedzy zasila rekomendacje pokazywane użytkownikowi, więc lepiej nie wgrać
# nic niż wgrać wpisy niekompletne.
POLA_WYMAGANE = (
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
KATEGORIA_DOMYSLNA = "technical"

# Nagłówki sekcji w polu `content`. Tekst trafia do wyszukiwania semantycznego,
# więc scalamy WSZYSTKIE pola merytoryczne - fragment dopasowany przez embeddingi
# ma nieść pełny kontekst problemu, a nie sam tytuł.
NAGLOWKI = {
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
            default=str(DOMYSLNA_SCIEZKA),
            help=f"Ścieżka do pliku JSON z bazą wiedzy (domyślnie: {DOMYSLNA_SCIEZKA}).",
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
        sciezka = Path(options["path"])
        wpisy = self._wczytaj(sciezka)
        self._sprawdz_wpisy(wpisy)

        if options["dry_run"]:
            self._raport_probny(wpisy)
            return

        utworzone, zaktualizowane = self._zapisz(wpisy)

        self.stdout.write(
            self.style.SUCCESS(
                f"Wczytano {len(wpisy)} wpis(ów) z {sciezka.name}: "
                f"{utworzone} nowych, {zaktualizowane} zaktualizowanych."
            )
        )
        self.stdout.write(f"Łącznie w bazie wiedzy: {KnowledgeDocument.objects.count()} dokument(ów).")
        self._raport_kategorii(wpisy)

        if options["index"]:
            self._zaindeksuj()

    def _raport_kategorii(self, wpisy: list[dict]) -> None:
        """Pokazuje, ile wpisów trafiło pod każdą kategorię wyszukiwaną przez audyt."""
        rozklad: dict[str, list[str]] = {}
        nieznane: set[str] = set()

        for wpis in wpisy:
            zrodlowa = wpis["category"]
            if zrodlowa not in CATEGORY_MAP:
                nieznane.add(zrodlowa)
            docelowa = CATEGORY_MAP.get(zrodlowa, KATEGORIA_DOMYSLNA)
            rozklad.setdefault(docelowa, []).append(zrodlowa)

        self.stdout.write("\nDostępność dla AuditService (pole category):")
        for docelowa in sorted(rozklad):
            zrodla = sorted(set(rozklad[docelowa]))
            self.stdout.write(
                f"  {docelowa:<12} {len(rozklad[docelowa]):>2} dokument(ów)  <- {', '.join(zrodla)}"
            )

        if nieznane:
            self.stdout.write(
                self.style.WARNING(
                    f"Kategorie spoza CATEGORY_MAP ({', '.join(sorted(nieznane))}) "
                    f"zapisano jako '{KATEGORIA_DOMYSLNA}'. Uzupełnij odwzorowanie w komendzie."
                )
            )

    # ------------------------------------------------------------------
    # Wczytanie i walidacja
    # ------------------------------------------------------------------
    def _wczytaj(self, sciezka: Path) -> list[dict]:
        if not sciezka.exists():
            raise CommandError(
                f"Nie znaleziono pliku {sciezka}. Plik źródłowy nie jest wersjonowany w repozytorium "
                "- sprawdź, czy katalog docs/audits/ zawiera rag_knowledge_base.json."
            )
        try:
            dane = json.loads(sciezka.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Plik {sciezka} nie jest poprawnym JSON-em: {exc}") from exc

        if not isinstance(dane, list) or not dane:
            raise CommandError(f"Plik {sciezka} musi zawierać niepustą tablicę obiektów JSON.")
        return dane

    def _sprawdz_wpisy(self, wpisy: list[dict]) -> None:
        """Waliduje komplet pól i unikalność kluczy PRZED dotknięciem bazy danych."""
        bledy: list[str] = []
        widziane: set[str] = set()

        for numer, wpis in enumerate(wpisy, start=1):
            if not isinstance(wpis, dict):
                bledy.append(f"pozycja {numer}: oczekiwano obiektu JSON")
                continue

            brakujace = [pole for pole in POLA_WYMAGANE if not wpis.get(pole)]
            if brakujace:
                bledy.append(f"pozycja {numer} ({wpis.get('key', '?')}): brak pól {brakujace}")
                continue

            if not isinstance(wpis["action_plan"], list):
                bledy.append(f"{wpis['key']}: action_plan musi być listą kroków")

            if wpis["key"] in widziane:
                bledy.append(f"{wpis['key']}: klucz powtarza się w pliku")
            widziane.add(wpis["key"])

        if bledy:
            raise CommandError(
                "Baza wiedzy nie przeszła walidacji - nie zapisano niczego:\n  - "
                + "\n  - ".join(bledy)
            )

    # ------------------------------------------------------------------
    # Zapis
    # ------------------------------------------------------------------
    @transaction.atomic
    def _zapisz(self, wpisy: list[dict]) -> tuple[int, int]:
        utworzone = zaktualizowane = 0

        for wpis in wpisy:
            # Dopasowanie po metadata.key, bo model nie ma pola unikalnego, a tytuł
            # bywa redagowany. `update_or_create` pomija przy tworzeniu parametry
            # zawierające "__", więc wartość klucza wnosi słownik `defaults`.
            _, czy_utworzony = KnowledgeDocument.objects.update_or_create(
                metadata__key=wpis["key"],
                defaults={
                    "title": wpis["title"],
                    "category": CATEGORY_MAP.get(wpis["category"], KATEGORIA_DOMYSLNA),
                    "content": self._zloz_tresc(wpis),
                    "metadata": {
                        "key": wpis["key"],
                        "cms_framework": wpis["cms_framework"],
                        "original_category": wpis["category"],
                        "action_plan": wpis["action_plan"],
                        "code_recipe": wpis["code_recipe"],
                        "geo_impact": wpis["geo_impact"],
                    },
                },
            )
            if czy_utworzony:
                utworzone += 1
            else:
                zaktualizowane += 1

        return utworzone, zaktualizowane

    def _zloz_tresc(self, wpis: dict) -> str:
        """Scala pola merytoryczne w jeden czytelny blok tekstowy pod wyszukiwanie wektorowe."""
        # Kroki są już ponumerowane w źródle ("Krok 1 (Answer-First): ..."), więc
        # dokładanie własnej numeracji dałoby "1. Krok 1: ...".
        kroki = "\n".join(f"- {krok}" for krok in wpis["action_plan"])
        sekcje = [
            wpis["title"],
            f"{NAGLOWKI['definition']}\n{wpis['definition']}",
            f"{NAGLOWKI['technical_cause']}\n{wpis['technical_cause']}",
            f"{NAGLOWKI['action_plan']}\n{kroki}",
            f"{NAGLOWKI['code_recipe']}\n{wpis['code_recipe']}",
            f"{NAGLOWKI['geo_impact']}\n{wpis['geo_impact']}",
        ]
        return "\n\n".join(sekcje)

    # ------------------------------------------------------------------
    # Tryb próbny i indeksowanie
    # ------------------------------------------------------------------
    def _raport_probny(self, wpisy: list[dict]) -> None:
        istniejace = set(
            KnowledgeDocument.objects
            .exclude(metadata__key=None)
            .values_list("metadata__key", flat=True)
        )
        nowe = [w["key"] for w in wpisy if w["key"] not in istniejace]

        self.stdout.write(
            f"[dry-run] Wpisów w pliku: {len(wpisy)} "
            f"({len(nowe)} nowych, {len(wpisy) - len(nowe)} do aktualizacji)."
        )
        for wpis in wpisy:
            znacznik = "+" if wpis["key"] in nowe else "~"
            docelowa = CATEGORY_MAP.get(wpis["category"], KATEGORIA_DOMYSLNA)
            self.stdout.write(
                f"  {znacznik} [{wpis['category']:<13} -> {docelowa:<11}] {wpis['key']}"
            )

        self._raport_kategorii(wpisy)

        osierocone = istniejace - {w["key"] for w in wpisy}
        if osierocone:
            self.stdout.write(
                self.style.WARNING(
                    f"[dry-run] W bazie są wpisy spoza pliku ({len(osierocone)}): "
                    f"{', '.join(sorted(osierocone))}. Komenda ich NIE usuwa."
                )
            )

    def _zaindeksuj(self) -> None:
        """Przebudowa indeksu wektorowego. Błąd nie unieważnia poprawnego importu do bazy."""
        from auditor.services.rag import RAGEngine

        try:
            ile = RAGEngine().index_knowledge_base()
        except Exception as exc:
            self.stdout.write(
                self.style.WARNING(
                    f"Import do bazy się powiódł, ale indeksowanie ChromaDB nie: "
                    f"{type(exc).__name__}: {exc}"
                )
            )
            return

        self.stdout.write(self.style.SUCCESS(f"Zaindeksowano {ile} dokument(ów) w ChromaDB."))
