"""Wspólny stan badania prowadzonego przez agentów.

Stan jest jednym obiektem przekazywanym kolejnym agentom, a nie zwracaną wartością
każdego z nich. Dzięki temu agent, który padnie, nie przerywa badania: zapisuje swój
błąd w `errors`, a następny pracuje na tym, co już jest.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SEOInvestigatorState:
    """Wszystko, co badanie wie o domenie w danym momencie."""

    domain: str
    audit_id: int | None = None

    # Twarde wnioski specjalistów - każdy wpis to jedno zdanie gotowe do raportu.
    technical_issues: list[str] = field(default_factory=list)
    analytics_insights: list[str] = field(default_factory=list)
    geo_visibility_notes: list[str] = field(default_factory=list)

    final_synthesis_report: str = ""

    # Awarie pojedynczych agentów. Badanie trwa dalej, ale raport ma o nich wiedzieć -
    # inaczej brak wniosków wyglądałby jak "wszystko w porządku".
    errors: list[str] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        """Czy zebrano cokolwiek, co da się zsyntetyzować."""
        return bool(
            self.technical_issues or self.analytics_insights or self.geo_visibility_notes
        )

    def record_error(self, agent: str, exc: Exception | str) -> None:
        """Zapisuje awarię agenta w formie czytelnej w raporcie i w logach."""
        opis = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        self.errors.append(f"{agent}: {opis}")

    def as_dict(self) -> dict:
        """Postać słownikowa - do serializacji w komendzie CLI i w testach."""
        return {
            "domain": self.domain,
            "audit_id": self.audit_id,
            "technical_issues": list(self.technical_issues),
            "analytics_insights": list(self.analytics_insights),
            "geo_visibility_notes": list(self.geo_visibility_notes),
            "final_synthesis_report": self.final_synthesis_report,
            "errors": list(self.errors),
        }
