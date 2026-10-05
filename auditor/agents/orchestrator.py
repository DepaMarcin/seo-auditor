"""Orkiestrator badania: uruchamia specjalistów i zbiera ich ustalenia.

Agenci czekają na trzy niezależne źródła (własny skaner, GA4/GSC, baza badań GEO),
więc kuszą do zrównoleglenia. Domyślnie jednak pracują PO KOLEI, bo każdy z nich
czyta z bazy, a SQLite - której używa ta aplikacja - blokuje tabelę przy równoległym
dostępie z wielu wątków ("database table is locked"). Agent przegrywa wtedy wyścig
i jego ustalenia przepadają, co w raporcie wygląda jak brak problemów.

`parallel=True` zostaje dla wdrożeń na bazie znoszącej współbieżność (PostgreSQL) -
zysk to kilka sekund na wywołaniach GA4/GSC.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from .specialists import AnalyticsAgent, GeoAgent, ReporterAgent, TechnicalAgent
from .state import SEOInvestigatorState

logger = logging.getLogger(__name__)

# Agenci zbierający dane. Kolejność nie ma znaczenia dla wyniku - każdy dokłada do
# własnej listy w stanie.
COLLECTORS = (TechnicalAgent, AnalyticsAgent, GeoAgent)


def run_seo_investigation(
    domain: str,
    owner=None,
    reporter=None,
    parallel: bool = False,
    on_progress=None,
) -> SEOInvestigatorState:
    """Prowadzi badanie domeny i zwraca komplet ustaleń wraz z raportem.

    `owner` ogranicza dane do audytów jednego użytkownika - bez niego badanie widzi
    wszystko, co jest sensowne tylko w komendzie CLI uruchamianej przez administratora.
    `reporter` pozwala wstrzyknąć własnego agenta syntezy (w testach - bez sieci).
    `parallel=True` uruchamia zbieraczy w wątkach - bezpieczne tylko na bazie
    znoszącej współbieżny dostęp (patrz docstring modułu).
    `on_progress(nazwa_agenta, state)` raportuje postęp do terminala.

    Awaria pojedynczego agenta nie przerywa badania: zapisuje się w `state.errors`
    i trafia do raportu jako ograniczenie. Raport z trzech źródeł, z których jedno
    zawiodło, jest użyteczny; brak raportu nie jest.
    """
    state = SEOInvestigatorState(domain=_clean_domain(domain))
    if not state.domain:
        state.record_error("Orchestrator", "Nie podano domeny do zbadania.")
        state.final_synthesis_report = ""
        return state

    agenci = [klasa() for klasa in COLLECTORS]

    if parallel:
        _run_parallel(agenci, state, owner, on_progress)
    else:
        for agent in agenci:
            _run_one(agent, state, owner, on_progress)

    reporter = reporter or ReporterAgent()
    reporter.run(state)
    if on_progress:
        on_progress(getattr(reporter, "name", "ReporterAgent"), state)

    return state


def _run_parallel(agenci: list, state: SEOInvestigatorState, owner, on_progress) -> None:
    """Uruchamia zbieraczy równolegle.

    Każdy dopisuje do własnej listy w stanie, więc nie potrzebujemy blokady - ale
    `errors` i `audit_id` są wspólne, dlatego scalamy wyniki po zakończeniu wątków,
    a nie w ich trakcie.
    """
    czesciowe = [SEOInvestigatorState(domain=state.domain) for _ in agenci]

    with ThreadPoolExecutor(max_workers=len(agenci)) as executor:
        zadania = {
            executor.submit(_run_one, agent, czastka, owner, None): (agent, czastka)
            for agent, czastka in zip(agenci, czesciowe)
        }
        for zadanie in zadania:
            agent, czastka = zadania[zadanie]
            try:
                zadanie.result()
            except Exception as exc:  # noqa: BLE001 - wyjątek z wątku nie może przepaść
                logger.exception("Agent %s zakończył się wyjątkiem.", agent.name)
                czastka.record_error(agent.name, exc)
            if on_progress:
                on_progress(agent.name, czastka)

    for czastka in czesciowe:
        state.technical_issues.extend(czastka.technical_issues)
        state.analytics_insights.extend(czastka.analytics_insights)
        state.geo_visibility_notes.extend(czastka.geo_visibility_notes)
        state.errors.extend(czastka.errors)
        state.sources.update(czastka.sources)
        if czastka.audit_id and state.audit_id is None:
            state.audit_id = czastka.audit_id


def _run_one(agent, state: SEOInvestigatorState, owner, on_progress) -> None:
    """Uruchamia jednego agenta, zamieniając jego awarię na wpis w stanie."""
    try:
        agent.run(state, owner=owner)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent %s zakończył się wyjątkiem.", agent.name)
        state.record_error(agent.name, exc)
    if on_progress:
        on_progress(agent.name, state)


def _clean_domain(domain: str) -> str:
    """Domena w postaci porównywalnej - przyjmujemy też pełny adres."""
    from auditor.services.google_api import audit_domain

    surowa = (domain or "").strip()
    if not surowa:
        return ""
    return audit_domain(surowa) or surowa.lower()
