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

    # Które źródła faktycznie dostarczyły dane. Nie da się tego wywnioskować z samych
    # list ustaleń: agent bez danych wpisuje tam zdanie o ich braku, więc niepusta
    # lista nie znaczy, że źródło było dostępne.
    sources: dict = field(default_factory=dict)

    # Surowe liczby do kart KPI: wynik techniczny, sesje, zmiana R/R, wynik GEO.
    # Trzymamy je obok zdań, bo zdanie jest dla czytelnika, a liczba dla wykresu -
    # wyciąganie jej z powrotem z tekstu byłoby parsowaniem własnego raportu.
    metrics: dict = field(default_factory=dict)

    # Z kiedy pochodzą dane każdego źródła (klucze jak w `sources`). Raport złożony
    # z zapisów sprzed miesiąca czyta się identycznie jak ze świeżych, więc bez tych
    # dat użytkownik nie miałby jak odróżnić diagnozy od archiwum.
    data_timestamps: dict = field(default_factory=dict)

    # Awarie pojedynczych agentów. Badanie trwa dalej, ale raport ma o nich wiedzieć -
    # inaczej brak wniosków wyglądałby jak "wszystko w porządku".
    errors: list[str] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        """Czy zebrano cokolwiek, co da się zsyntetyzować."""
        return bool(
            self.technical_issues or self.analytics_insights or self.geo_visibility_notes
        )

    def mark_source(self, name: str, available: bool) -> None:
        """Odnotowuje, czy dane źródło wniosło dane do badania."""
        self.sources[name] = available

    def has_source(self, name: str) -> bool:
        return bool(self.sources.get(name))

    @property
    def source_count(self) -> int:
        """Ile źródeł dostarczyło dane - "2 z 3" w nagłówku syntezy."""
        return sum(1 for dostepne in self.sources.values() if dostepne)

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
            "sources": dict(self.sources),
            "metrics": dict(self.metrics),
            "data_timestamps": {
                nazwa: data.isoformat() if hasattr(data, "isoformat") else data
                for nazwa, data in self.data_timestamps.items()
            },
            "errors": list(self.errors),
        }
