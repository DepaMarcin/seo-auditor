"""Symulator widoczności marki w wyszukiwarkach AI (GEO).

Klasyczne SEO mierzy pozycję w wynikach wyszukiwania. W odpowiedziach generatywnych
nie ma pozycji - jest przypis albo go nie ma. Ten moduł mierzy więc coś innego:
zadaje pytanie intencyjne KILKA RAZY i liczy, w ilu odpowiedziach model przytoczył
domenę klienta jako źródło.

Powtórzenia są tu istotne merytorycznie, nie dla statystyki. Modele językowe są
niedeterministyczne, więc jedno trafienie nie odróżnia marki cytowanej regularnie od
przypadkowej wzmianki. Dopiero rozkład pozwala powiedzieć, czy obecność jest STABILNA
(cytowana niemal zawsze), NIESTABILNA (raz tak, raz nie) czy jej NIE MA.

Pomiar opiera się na narzędziu `web_search` w Responses API OpenAI: model faktycznie
przeszukuje sieć i zwraca przypisy (`url_citation`) z adresami źródeł. To jedyna
dostępna tu droga do prawdziwych cytowań - odpowiedź modelu z pamięci parametrycznej
nie powiedziałaby nic o widoczności w wyszukiwarce AI.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

from django.conf import settings

logger = logging.getLogger(__name__)

# Model odpytywany w symulacji. Tańszy wariant wystarcza: mierzymy, CO wyszukiwarka
# AI cytuje, a nie jak elegancko o tym pisze.
GEO_MODEL = "gpt-4o-mini"

# Model układający pytania intencyjne - to zadanie wymaga znajomości branży klienta.
QUESTION_MODEL = "gpt-4o-mini"

DEFAULT_QUESTION_COUNT = 5
DEFAULT_REPETITIONS = 5

# Progi klasyfikacji stabilności obecności w odpowiedziach.
STABLE_THRESHOLD = 80   # cytowana w co najmniej 80% powtórzeń
ABSENT_THRESHOLD = 0    # ani razu

# Ile domen konkurencji pokazujemy w zestawieniu.
TOP_COMPETITORS = 5

QUESTION_PROMPT = """Jesteś strategiem widoczności w wyszukiwarkach AI (GEO).

Ułóż {count} pytań, które realny klient wpisałby do ChatGPT lub Perplexity, szukając
usług takich jak oferowane przez {domain}.

Zasady:
- pytania po polsku, w formie naturalnego zapytania użytkownika,
- NIE wymieniaj nazwy marki ani domeny - chodzi o pytania, w których marka MOŻE zostać
  zacytowana, a nie o takie, które ją z góry wskazują,
- każde pytanie dotyczy innej intencji (porównanie, wybór, cena, sposób użycia, problem),
- jedno pytanie w linii, bez numeracji i bez komentarza."""


@dataclass
class RunResult:
    """Wynik jednego wywołania modelu."""

    answer: str = ""
    citations: list[dict] = field(default_factory=list)
    brand_cited: bool = False
    brand_position: int | None = None
    error: str = ""


class GeoSimulatorError(RuntimeError):
    """Symulacja nie mogła się wykonać (brak klucza, brak dostępu do narzędzia)."""


# ----------------------------------------------------------------------
# Klient OpenAI
# ----------------------------------------------------------------------
def _client():
    api_key = getattr(settings, "OPENAI_API_KEY", "")
    if not api_key:
        raise GeoSimulatorError(
            "Symulator GEO wymaga OPENAI_API_KEY - bez niego nie da się odpytać "
            "wyszukiwarki AI."
        )
    from openai import OpenAI

    return OpenAI(api_key=api_key)


# ----------------------------------------------------------------------
# Pytania
# ----------------------------------------------------------------------
def generate_questions(domain: str, count: int = DEFAULT_QUESTION_COUNT) -> list[str]:
    """Układa pytania intencyjne dla domeny. Przy błędzie zwraca pytania zapasowe."""
    try:
        response = _client().responses.create(
            model=QUESTION_MODEL,
            input=QUESTION_PROMPT.format(count=count, domain=domain),
        )
        questions = [
            line.strip(" -•\t")
            for line in (response.output_text or "").splitlines()
            if line.strip(" -•\t")
        ]
        if questions:
            return questions[:count]
        logger.warning("Model nie zwrócił pytań dla %s - używam zapasowych.", domain)
    except Exception as exc:
        logger.warning("Nie udało się wygenerować pytań dla %s: %s", domain, exc)

    return _fallback_questions(domain, count)


def _fallback_questions(domain: str, count: int) -> list[str]:
    """Pytania awaryjne - lepsze niż puste badanie, gorsze niż dopasowane do branży."""
    brand = domain.split(".")[0]
    szablony = [
        f"Jakie firmy w Polsce oferują usługi podobne do {brand}?",
        f"Co warto wiedzieć przed wyborem dostawcy usług typu {brand}?",
        f"Który dostawca usług typu {brand} jest najlepiej oceniany?",
        f"Ile kosztują usługi typu {brand} w Polsce?",
        f"Jak porównać ofertę firm takich jak {brand}?",
    ]
    return szablony[:count]


# ----------------------------------------------------------------------
# Pojedyncze zapytanie
# ----------------------------------------------------------------------
def ask_once(question: str, domain: str, client=None) -> RunResult:
    """Zadaje jedno pytanie wyszukiwarce AI i sprawdza, czy domena jest w przypisach."""
    try:
        client = client or _client()
        response = client.responses.create(
            model=GEO_MODEL,
            tools=[{"type": "web_search"}],
            # Wymuszamy wyszukiwanie: bez tego model bywa odpowiada z pamięci i nie
            # zwraca żadnych przypisów, a wtedy pomiar widoczności nie ma podstaw.
            tool_choice="required",
            input=question,
        )
    except Exception as exc:
        logger.warning("Zapytanie GEO nie powiodło się (%s): %s", question[:60], exc)
        return RunResult(error=f"{type(exc).__name__}: {exc}")

    citations = _extract_citations(response)
    brand_position = _find_brand_position(citations, domain)
    return RunResult(
        answer=(response.output_text or "").strip(),
        citations=citations,
        brand_cited=brand_position is not None,
        brand_position=brand_position,
    )


def _extract_citations(response) -> list[dict]:
    """Wyciąga przypisy `url_citation` w kolejności wystąpienia w odpowiedzi."""
    citations: list[dict] = []
    seen: set[str] = set()

    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            for annotation in getattr(content, "annotations", []) or []:
                url = getattr(annotation, "url", None)
                if not url:
                    continue
                domain = normalize_domain(url)
                if domain in seen:
                    # Ta sama domena cytowana kilka razy zajmuje jedną pozycję -
                    # inaczej serwis z pięcioma podstronami zawyżałby listę źródeł.
                    continue
                seen.add(domain)
                citations.append({
                    "url": url,
                    "domain": domain,
                    "title": getattr(annotation, "title", "") or "",
                    "position": len(citations) + 1,
                })
    return citations


def _find_brand_position(citations: list[dict], domain: str) -> int | None:
    target = normalize_domain(domain)
    for citation in citations:
        if citation["domain"] == target or citation["domain"].endswith("." + target):
            return citation["position"]
    return None


def normalize_domain(value: str) -> str:
    """Sprowadza adres lub domenę do postaci porównywalnej (bez schematu i www)."""
    text = (value or "").strip().lower()
    if "://" in text:
        text = urlparse(text).hostname or ""
    else:
        text = text.split("/")[0]
    return text.removeprefix("www.")


# ----------------------------------------------------------------------
# Całe badanie
# ----------------------------------------------------------------------
def run_study(study, client=None, on_progress=None) -> None:
    """Wykonuje badanie: każde pytanie razy liczba powtórzeń.

    `on_progress(done, total)` pozwala raportować postęp; `client` umożliwia
    wstrzyknięcie atrapy w testach.
    """
    from django.utils import timezone

    from auditor.models import GeoRun, GeoStudy

    study.status = GeoStudy.Status.PROCESSING
    study.save(update_fields=["status"])

    try:
        client = client or _client()
    except GeoSimulatorError as exc:
        study.status = GeoStudy.Status.FAILED
        study.error = str(exc)
        study.save(update_fields=["status", "error"])
        return

    queries = list(study.queries.all())
    total = len(queries) * study.repetitions
    done = 0

    try:
        for query in queries:
            # Powtórzenia jednego pytania zapisujemy od razu - przy przerwaniu badania
            # zostaje częściowy, ale prawdziwy wynik zamiast pustki.
            GeoRun.objects.filter(query=query).delete()
            for attempt in range(1, study.repetitions + 1):
                result = ask_once(query.text, study.domain, client=client)
                GeoRun.objects.create(
                    query=query,
                    attempt=attempt,
                    answer=result.answer,
                    citations=result.citations,
                    brand_cited=result.brand_cited,
                    brand_position=result.brand_position,
                    error=result.error,
                )
                done += 1
                if on_progress:
                    on_progress(done, total)

            _summarize_query(query, study.domain)

        study.overall_score = _overall_score(queries)
        study.status = GeoStudy.Status.COMPLETED
    except Exception as exc:
        logger.exception("Badanie GEO %s nie powiodło się.", study.pk)
        study.status = GeoStudy.Status.FAILED
        study.error = f"{type(exc).__name__}: {exc}"
    finally:
        study.finished_at = timezone.now()
        study.save(update_fields=["status", "overall_score", "error", "finished_at"])


def _summarize_query(query, domain: str) -> None:
    """Przelicza wyniki powtórzeń jednego pytania na wskaźniki pokazywane w tabeli."""
    from collections import Counter

    from auditor.models import GeoQuery

    runs = list(query.runs.all())
    usable = [run for run in runs if not run.error]
    cited = [run for run in usable if run.brand_cited]

    rate = round(len(cited) / len(usable) * 100) if usable else 0
    query.citation_rate = rate
    query.cited_positions = [run.brand_position for run in cited if run.brand_position]
    query.stability = _classify_stability(rate)

    # Konkurencja liczona WYŁĄCZNIE z powtórzeń bez marki klienta - to odpowiedź na
    # pytanie "kogo model cytuje, gdy nie cytuje nas".
    competitors: Counter = Counter()
    target = normalize_domain(domain)
    for run in usable:
        if run.brand_cited:
            continue
        for citation in run.citations or []:
            if citation.get("domain") and citation["domain"] != target:
                competitors[citation["domain"]] += 1

    query.competitors = [
        {"domain": d, "count": c} for d, c in competitors.most_common(TOP_COMPETITORS)
    ]
    query.save(
        update_fields=["citation_rate", "cited_positions", "stability", "competitors"]
    )


def _classify_stability(rate: int) -> str:
    from auditor.models import GeoQuery

    if rate >= STABLE_THRESHOLD:
        return GeoQuery.Stability.STABLE
    if rate > ABSENT_THRESHOLD:
        return GeoQuery.Stability.VOLATILE
    return GeoQuery.Stability.ABSENT


def _overall_score(queries) -> int:
    rates = [q.citation_rate for q in queries]
    return round(sum(rates) / len(rates)) if rates else 0
