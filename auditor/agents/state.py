"""Wspólny stan badania prowadzonego przez agentów.

Stan jest jednym obiektem przekazywanym kolejnym agentom, a nie zwracaną wartością
każdego z nich. Dzięki temu agent, który padnie, nie przerywa badania: zapisuje swój
błąd w `errors`, a następny pracuje na tym, co już jest.

Gdy agenci pracują równolegle (wątki w jednym procesie albo osobni workerzy Celery),
ten wspólny obiekt przestaje istnieć: każdy wykonawca dostaje własny, czysty stan,
a wyniki trzeba scalić. Regułę scalania trzyma `absorb()` - jedna, używana przez
oba tryby, bo dwie kopie tej samej reguły rozjechałyby się przy pierwszym nowym polu.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Źródła badania, w kolejności prezentacji. Nazwy muszą zgadzać się ze stałymi
# SOURCE_* w `specialists.py` - trzymamy je tutaj, bo `state` nie importuje modeli
# ani serwisów, więc i modele, i widoki mogą po nie sięgnąć bez cyklu importów.
SOURCE_KEYS: tuple[str, ...] = ("technical", "analytics", "geo")


def merge_states(states_data: list[dict]) -> "SEOInvestigatorState":
    """Scala stany zebrane przez równoległych wykonawców w jeden wspólny.

    Wejściem są słowniki (`as_dict()`), bo przez kolejkę Celery nie da się przekazać
    obiektu - a nawet gdyby dało, serializacja jest tu zaletą: wymusza, żeby stan był
    w pełni opisany danymi, a nie referencjami do modeli.

    Domena pochodzi z pierwszej niepustej cząstki - wszystkie dotyczą tej samej
    witryny, więc rozjazd oznaczałby błąd w budowie przepływu.
    """
    czastki = [SEOInvestigatorState.from_dict(dane) for dane in (states_data or []) if dane]
    if not czastki:
        return SEOInvestigatorState(domain="")

    scalony = SEOInvestigatorState(domain=czastki[0].domain)
    for czastka in czastki:
        scalony.absorb(czastka)
    return scalony


def _parse_timestamp(wartosc):
    """Napis ISO z powrotem na `datetime` albo `date`; cokolwiek innego - bez zmian.

    Obecność litery "T" rozstrzyga typ: `datetime.fromisoformat("2026-09-10")` też by
    się udało, ale zamieniłoby czystą datę w północ, czyli dokładałoby precyzję,
    której w danych nie było.
    """
    if not isinstance(wartosc, str):
        return wartosc

    from datetime import date, datetime

    try:
        return datetime.fromisoformat(wartosc) if "T" in wartosc else date.fromisoformat(wartosc)
    except ValueError:
        return None


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

    def absorb(self, other: SEOInvestigatorState) -> None:
        """Dokłada do tego stanu ustalenia zebrane przez inny stan.

        Listy sklejamy, słowniki nadpisujemy. Nadpisywanie jest tu właściwe, bo każde
        źródło wpisuje własne klucze (`technical_*`, `organic_*`, `geo_*`) - kolizji
        nie ma, a gdyby była, znaczyłaby że dwóch agentów mierzy to samo.
        """
        self.technical_issues.extend(other.technical_issues)
        self.analytics_insights.extend(other.analytics_insights)
        self.geo_visibility_notes.extend(other.geo_visibility_notes)
        self.errors.extend(other.errors)

        self.sources.update(other.sources)
        self.metrics.update(other.metrics)
        self.data_timestamps.update(other.data_timestamps)

        # Identyfikator audytu ustawia tylko agent techniczny; pierwszy wygrywa.
        if other.audit_id and self.audit_id is None:
            self.audit_id = other.audit_id

        # Raport składa agent syntezy, czyli już po scaleniu. Gdyby jednak przyszedł
        # w cząstce, nie wolno go zgubić.
        if other.final_synthesis_report and not self.final_synthesis_report:
            self.final_synthesis_report = other.final_synthesis_report

    @classmethod
    def from_dict(cls, dane: dict) -> SEOInvestigatorState:
        """Odtwarza stan z postaci słownikowej - odwrotność `as_dict()`.

        Daty wracają jako `date`/`datetime`, nie jako napisy: liczy się na nich wiek
        danych (auditor.presentation.build_stale_data_warnings), a napis nie odjąłby się
        od dzisiejszej daty.
        """
        return cls(
            domain=dane.get("domain", ""),
            audit_id=dane.get("audit_id"),
            technical_issues=list(dane.get("technical_issues") or []),
            analytics_insights=list(dane.get("analytics_insights") or []),
            geo_visibility_notes=list(dane.get("geo_visibility_notes") or []),
            final_synthesis_report=dane.get("final_synthesis_report") or "",
            sources=dict(dane.get("sources") or {}),
            metrics=dict(dane.get("metrics") or {}),
            data_timestamps={
                nazwa: _parse_timestamp(wartosc)
                for nazwa, wartosc in (dane.get("data_timestamps") or {}).items()
            },
            errors=list(dane.get("errors") or []),
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
            "sources": dict(self.sources),
            "metrics": dict(self.metrics),
            "data_timestamps": {
                nazwa: data.isoformat() if hasattr(data, "isoformat") else data
                for nazwa, data in self.data_timestamps.items()
            },
            "errors": list(self.errors),
        }
