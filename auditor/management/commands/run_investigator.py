"""Uruchamia badanie SEO prowadzone przez agentów i drukuje raport.

Przykład:
    python manage.py run_investigator --domain enova.pl
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Prowadzi wieloagentowe badanie SEO domeny i drukuje raport w Markdownie."

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True, help="Domena do zbadania, np. enova.pl")
        parser.add_argument(
            "--owner",
            help="Login właściciela audytów. Bez niego badanie widzi dane wszystkich kont.",
        )
        parser.add_argument(
            "--no-llm",
            action="store_true",
            help="Pomija syntezę modelem - raport powstaje z samych ustaleń.",
        )
        parser.add_argument(
            "--parallel",
            action="store_true",
            help=(
                "Uruchamia agentów równolegle. Bezpieczne tylko na bazie znoszącej "
                "współbieżny dostęp - SQLite blokuje wtedy tabelę."
            ),
        )

    def handle(self, *args, **options):
        from auditor.agents.orchestrator import run_seo_investigation
        from auditor.agents.specialists import ReporterAgent, _fallback_report

        owner = None
        if options["owner"]:
            owner = get_user_model().objects.filter(username=options["owner"]).first()
            if owner is None:
                raise CommandError(f"Nie ma użytkownika o loginie {options['owner']!r}.")

        reporter = None
        if options["no_llm"]:
            reporter = _OfflineReporter(_fallback_report)

        self.stdout.write(self.style.MIGRATE_HEADING(f"Badanie domeny: {options['domain']}"))
        self.stdout.write("")

        state = run_seo_investigation(
            options["domain"],
            owner=owner,
            reporter=reporter,
            parallel=options["parallel"],
            on_progress=self._report_progress,
        )

        self.stdout.write("")
        self._print_findings(state)
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("RAPORT"))
        self.stdout.write(state.final_synthesis_report or "(brak raportu)")

        if state.errors:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(f"Ograniczenia badania: {len(state.errors)}")
            )

    def _report_progress(self, agent_name: str, state) -> None:
        """Stan pośredni - widać, który agent już skończył i co wniósł."""
        wniesione = (
            len(state.technical_issues)
            + len(state.analytics_insights)
            + len(state.geo_visibility_notes)
        )
        self.stdout.write(f"  [{agent_name}] ustaleń: {wniesione}, błędów: {len(state.errors)}")

    def _print_findings(self, state) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("ZEBRANE USTALENIA"))
        if state.audit_id:
            self.stdout.write(f"  audyt techniczny: #{state.audit_id}")

        for naglowek, wpisy in (
            ("Techniczne", state.technical_issues),
            ("Analityka", state.analytics_insights),
            ("Widoczność w AI", state.geo_visibility_notes),
        ):
            self.stdout.write(f"  {naglowek}:")
            if not wpisy:
                self.stdout.write("    (brak)")
            for wpis in wpisy:
                self.stdout.write(f"    - {wpis}")

        if state.errors:
            self.stdout.write("  Ograniczenia:")
            for blad in state.errors:
                self.stdout.write(self.style.WARNING(f"    - {blad}"))


class _OfflineReporter:
    """Agent syntezy bez modelu - składa raport z samych ustaleń."""

    name = "ReporterAgent(offline)"

    def __init__(self, builder):
        self._builder = builder

    def run(self, state):
        state.final_synthesis_report = self._builder(state)
        return state
