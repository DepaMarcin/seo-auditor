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
import re
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

# Jak stary audyt wciąż nadaje się na źródło kontekstu. Oferta firmy zmienia się
# rzadko, więc dwa tygodnie oszczędzają pobranie bez ryzyka opisania nieaktualnej
# branży.
CONTEXT_MAX_AGE_DAYS = 14

# Ile słów treści trafia do promptu. Tyle wystarcza, by rozpoznać branżę, a nie
# rozdmuchuje kosztu zapytania.
CONTEXT_SNIPPET_WORDS = 300

# Ile nagłówków bierzemy pod uwagę - nagłówki niosą ofertę gęściej niż tekst ciągły.
CONTEXT_MAX_HEADINGS = 15

# Prompt bez kontekstu strony: model zna wyłącznie nazwę domeny i musi zgadywać
# branżę. Zostaje jako ostatnia deska ratunku, gdy strony nie da się pobrać.
QUESTION_PROMPT = """Jesteś strategiem widoczności w wyszukiwarkach AI (GEO).

Ułóż {count} pytań, które realny klient wpisałby do ChatGPT lub Perplexity, szukając
usług takich jak oferowane przez {domain}.

Zasady:
- pytania po polsku, w formie naturalnego zapytania użytkownika,
- NIE wymieniaj nazwy marki ani domeny - chodzi o pytania, w których marka MOŻE zostać
  zacytowana, a nie o takie, które ją z góry wskazują,
- każde pytanie dotyczy innej intencji (porównanie, wybór, cena, sposób użycia, problem),
- jedno pytanie w linii, bez numeracji i bez komentarza."""

# Separatory, którymi strony oddzielają nazwę marki od reszty tytułu:
# "Early Stage | Szkoła językowa dla dzieci".
BRAND_SEPARATORS = ("|", "—", "–", "-", "·", "»", ":")

# Najkrótsza sensowna nazwa marki. Jednoliterowe fragmenty tytułu to zwykle śmieci
# po podziale, a nie nazwa firmy.
MIN_BRAND_LENGTH = 2

# Od tej długości nazwa jednoczłonowa może być w treści rozdzielona spacją
# ("Helendoron" z domeny vs "Helen Doron" w odpowiedzi). Krótsze nazwy zostawiamy
# sztywne - przy 3-4 literach rozdzielanie dawałoby przypadkowe trafienia.
MIN_SPLITTABLE_BRAND_LENGTH = 7

# Prompt z kontekstem pobranym ze strony. Różnica jest zasadnicza: bez treści model
# zgaduje po nazwie domeny i regularnie trafia w pytania o SEO i marketing, bo tak
# wygląda większość stron, które zna. Z ofertą przed oczami opisuje właściwą branżę.
CONTEXT_QUESTION_PROMPT = """Jesteś ekspertem ds. analizy intencji zakupowych i wyszukiwań B2B/B2C.
Oto dane pobrane bezpośrednio ze strony internetowej pod podanym adresem:

URL: {url}
TYTUŁ: {title}
OPIS META: {meta_description}
NAGŁÓWKI: {headers_text}
FRAGMENT TREŚCI: {body_snippet}

ZADANIE:
1. Na podstawie powyższych danych zidentyfikuj DOKŁADNĄ branżę, produkty lub usługi
   oferowane pod tym adresem.
2. Wygeneruj {count} realistycznych zapytań intencyjnych (porównawczych lub problemowych),
   jakie potencjalny klient TEJ KONKRETNEJ FIRMY wpisałby do ChatGPT lub Perplexity,
   szukając takich produktów/usług.
3. BEZWZGLĘDNY ZAKAZ: Nie używaj nazwy marki ani nazwy własnej firmy w pytaniach.
4. BEZWZGLĘDNY ZAKAZ: Nie generuj pytań o pozycjonowanie, SEO ani marketing, CHYBA ŻE
   podany URL to strona agencji marketingowej/SEO.
5. Zwróć wynik wyłącznie jako tablicę JSON z {count} pytaniami w języku strony."""

# Prompt nastawiony wyłącznie na intencję zakupową. Powód rozdzielenia: pytania
# poradnikowe ("Jak nauczyć dziecko angielskiego?") model odpowiada artykułami z
# blogów i poradników, więc firma nie ma szansy zostać zacytowana - pomiar mierzy
# wtedy widoczność treści eksperckich, a nie widoczność oferty.
COMMERCIAL_QUESTION_PROMPT = """Jesteś analitykiem intencji zakupowych w wyszukiwarkach AI (ChatGPT, Perplexity).
Przeanalizuj ofertę strony: {url} (Marka: {brand_name}, Tytuł: {title}, Opis: {meta_description}).

NAGŁÓWKI: {headers_text}
FRAGMENT TREŚCI: {body_snippet}

ZADANIE:
Wygeneruj dokładnie {count} pytań, jakie potencjalny KLIENT wpisuje do AI, gdy szuka
KONKRETNEJ USŁUGI, SZKOŁY LUB PRODUKTU z tej oferty.

KATEGORYCZNE ZASADY:
1. Pytania MUSZĄ wymuszać na AI rekomendację konkretnych firm lub ofert (używaj:
   "Jaka szkoła/firma...", "Gdzie zapisać/kupić...", "Które oferty są polecane dla...",
   "Ranking najlepszych...").
2. BEZWZGLĘDNY ZAKAZ pytań poradnikowych i teoretycznych ("Jak nauczyć...",
   "Czy istnieją różnice...", "Jakie są metody...").
3. BEZWZGLĘDNY ZAKAZ używania nazwy marki ({brand_name}) w pytaniach.
4. BEZWZGLĘDNY ZAKAZ pytań o pozycjonowanie, SEO ani marketing, CHYBA ŻE podany URL
   to strona agencji marketingowej/SEO.
5. Zwróć wynik wyłącznie jako tablicę JSON z {count} stringami w języku strony."""


@dataclass
class SiteContext:
    """Kontekst branżowy strony: to, co pozwala modelowi rozpoznać ofertę."""

    url: str
    title: str = ""
    meta_description: str = ""
    headings: list[str] = field(default_factory=list)
    body_snippet: str = ""
    brand_name: str = ""
    # "audit" - odczytane z gotowego audytu w bazie, "scan" - pobrane na żywo.
    source: str = ""
    error: str = ""

    @property
    def usable(self) -> bool:
        """Czy zebrało się cokolwiek, co niesie informację o branży."""
        return bool(self.title or self.meta_description or self.headings or self.body_snippet)

    def headers_text(self) -> str:
        return " | ".join(self.headings) if self.headings else "(brak nagłówków)"

    def as_prompt_fields(self) -> dict:
        return {
            "url": self.url,
            "brand_name": self.brand_name or extract_brand_name(self.url) or "(nieznana)",
            "title": self.title or "(brak tytułu)",
            "meta_description": self.meta_description or "(brak opisu meta)",
            "headers_text": self.headers_text(),
            "body_snippet": self.body_snippet or "(brak treści)",
        }


@dataclass
class RunResult:
    """Wynik jednego wywołania modelu."""

    answer: str = ""
    citations: list[dict] = field(default_factory=list)
    brand_cited: bool = False
    brand_position: int | None = None
    brand_mentioned: bool = False
    error: str = ""

    @property
    def visibility(self) -> str:
        """Poziom widoczności marki w tej odpowiedzi.

        Cytowanie z linkiem ma pierwszeństwo: przynosi ruch, a sama wzmianka nie.
        """
        from auditor.models import GeoRun

        if self.brand_cited:
            return GeoRun.Visibility.LINKED_CITATION
        if self.brand_mentioned:
            return GeoRun.Visibility.BRAND_MENTION
        return GeoRun.Visibility.ABSENT


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
def extract_brand_name(url: str, title: str = "", site_name: str = "") -> str:
    """Odgaduje nazwę marki ze strony.

    Kolejność źródeł od najpewniejszego: `og:site_name` jest deklaracją właściciela,
    tytuł bywa nazwą sklejoną z opisem oferty, a domena zostaje jako ostatnia deska
    ratunku. Nazwa jest potrzebna, bo model pisze "Early Stage", nie "earlystage.pl" -
    bez niej wzmianki w treści odpowiedzi byłyby dla pomiaru niewidoczne.
    """
    kandydat = (site_name or "").strip()

    if not kandydat and title:
        czesci = [title]
        for separator in BRAND_SEPARATORS:
            if separator in title:
                czesci = [fragment.strip() for fragment in title.split(separator)]
                break
        czesci = [fragment for fragment in czesci if len(fragment) >= MIN_BRAND_LENGTH]
        if czesci:
            # Marka stoi zwykle na skraju tytułu; krótszy skraj to prawie zawsze
            # nazwa, dłuższy - opis oferty.
            kandydat = min((czesci[0], czesci[-1]), key=len)

    if not kandydat:
        kandydat = _brand_from_domain(url)

    return kandydat[:120].strip()


def _brand_from_domain(url: str) -> str:
    """Nazwa z samej domeny: "earlystage.pl" -> "Earlystage"."""
    domena = normalize_domain(url)
    if not domena:
        return ""
    rdzen = domena.split(".")[0].replace("-", " ")
    return rdzen[:1].upper() + rdzen[1:]


def brand_mention_pattern(brand_name: str) -> re.Pattern | None:
    """Wzorzec szukający nazwy marki w treści odpowiedzi.

    Dopasowanie na granicach słów, żeby "Early Stage" nie trafiało w środek innego
    wyrazu, i z elastyczną spacją - model bywa pisze "EarlyStage" łącznie.

    Odwrotny przypadek dotyczy nazw wyprowadzonych z domeny: z "helendoron.pl" wychodzi
    jednoczłonowe "Helendoron", a model pisze "Helen Doron". Dla dostatecznie długich
    nazw jednoczłonowych dopuszczamy więc separator między literami - przy tej długości
    przypadkowe trafienie wymagałoby dokładnie tej samej sekwencji liter.
    """
    nazwa = (brand_name or "").strip()
    if len(nazwa) < MIN_BRAND_LENGTH:
        return None

    czesci = [re.escape(fragment) for fragment in nazwa.split() if fragment]
    if not czesci:
        return None

    if len(czesci) == 1 and len(nazwa) >= MIN_SPLITTABLE_BRAND_LENGTH:
        czesci = [re.escape(litera) for litera in nazwa]

    return re.compile(r"\b" + r"[\s\-]*".join(czesci) + r"\b", re.IGNORECASE)


def detect_brand_mention(answer: str, brand_name: str) -> bool:
    """Czy odpowiedź wymienia markę z nazwy."""
    wzorzec = brand_mention_pattern(brand_name)
    return bool(wzorzec and answer and wzorzec.search(answer))


# Ilu konkurentów ma sens porównywać. Powyżej tej liczby tabela przestaje być
# zestawieniem, a staje się listą wszystkich cytowanych domen - od tego jest
# osobna sekcja źródeł.
MAX_COMPETITORS = 5

# Ilu rywali typujemy sami, gdy użytkownik nie wskazał nikogo.
AUTO_COMPETITORS = 3


def parse_competitors_input(raw: str | list | None, exclude: str = "") -> list[str]:
    """Zamienia wpisane domeny konkurentów na znormalizowaną listę.

    Przyjmuje tekst rozdzielony przecinkami, średnikami lub nowymi liniami - użytkownik
    wkleja te domeny skądkolwiek i nie ma powodu wymagać jednego formatu.
    """
    if isinstance(raw, list):
        surowe = raw
    else:
        surowe = re.split(r"[,;\n]+", raw or "")

    wlasna = normalize_domain(exclude)
    domeny: list[str] = []
    for fragment in surowe:
        domena = normalize_domain(str(fragment).strip())
        # Własna domena na liście rywali dawałaby porównanie z samym sobą.
        if domena and domena != wlasna and domena not in domeny:
            domeny.append(domena)

    return domeny[:MAX_COMPETITORS]


def get_or_fetch_site_context(url: str, max_age_days: int = CONTEXT_MAX_AGE_DAYS) -> SiteContext:
    """Zbiera kontekst branżowy strony: najpierw z bazy, potem pobraniem na żywo.

    Kolejność nie jest optymalizacją kosztu, tylko jakości: audyt w bazie przeszedł
    pełny tor pobierania (rotacja User-Agenta, fallback przeglądarkowy dla stron CSR),
    więc jego dane bywają bogatsze niż to, co zwróci pojedyncze żądanie.
    """
    context = _context_from_recent_audit(url, max_age_days)
    if context is not None and context.usable:
        return context
    return _scan_site(url)


def _context_from_recent_audit(url: str, max_age_days: int) -> SiteContext | None:
    """Kontekst z zakończonego audytu tej samej domeny, o ile jest dostatecznie świeży."""
    from datetime import timedelta

    from django.utils import timezone

    from auditor.models import Audit

    domain = normalize_domain(url)
    if not domain:
        return None

    cutoff = timezone.now() - timedelta(days=max_age_days)
    audit = (
        Audit.objects.filter(
            url__icontains=domain,
            status=Audit.Status.COMPLETED,
            created_at__gte=cutoff,
        )
        .order_by("-created_at")
        .first()
    )
    if audit is None:
        return None

    context = SiteContext(url=url, source="audit")
    for metric in audit.metrics.filter(key__in=("title", "meta_description", "h1_structure")):
        payload = metric.value if isinstance(metric.value, dict) else {}
        if metric.key == "title":
            context.title = (payload.get("value") or "").strip()
        elif metric.key == "meta_description":
            context.meta_description = (payload.get("value") or "").strip()
        elif metric.key == "h1_structure":
            context.headings = [h for h in (payload.get("headings") or []) if h][:CONTEXT_MAX_HEADINGS]

    if not context.usable:
        return None

    context.brand_name = extract_brand_name(url, title=context.title)
    logger.info("Kontekst GEO dla %s odczytany z audytu #%s.", domain, audit.pk)
    return context


def _scan_site(url: str) -> SiteContext:
    """Szybkie pobranie strony wyłącznie po to, by rozpoznać branżę.

    Korzystamy ze `SEOScraper`, a nie z gołego `httpx`/`requests`: każdy adres (łącznie
    z każdym przekierowaniem) musi przejść walidację chroniącą przed SSRF, a strony
    renderowane po stronie klienta wymagają fallbacku przeglądarkowego - inaczej
    dostalibyśmy pusty szkielet i znowu zgadywanie branży.
    """
    from bs4 import BeautifulSoup

    from .scraper import SEOScraper, ScraperError

    context = SiteContext(url=url, source="scan")
    try:
        html = SEOScraper().fetch(url)
    except ScraperError as exc:
        logger.warning("Nie udało się pobrać treści %s: %s", url, exc)
        context.error = str(exc)
        return context
    except Exception as exc:  # noqa: BLE001 - awaria pobrania nie może wywrócić panelu
        logger.warning("Nieoczekiwany błąd pobrania %s: %s", url, exc)
        context.error = str(exc)
        return context

    soup = BeautifulSoup(html, "html.parser")

    if soup.title and soup.title.string:
        context.title = soup.title.string.strip()

    for nazwa in ("description", "og:description", "twitter:description"):
        tag = soup.find("meta", attrs={"name": nazwa}) or soup.find("meta", attrs={"property": nazwa})
        if tag and tag.get("content", "").strip():
            context.meta_description = tag["content"].strip()
            break

    naglowki = []
    for poziom in ("h1", "h2", "h3"):
        for element in soup.find_all(poziom):
            tekst = element.get_text(" ", strip=True)
            if tekst:
                naglowki.append(tekst)
    context.headings = naglowki[:CONTEXT_MAX_HEADINGS]

    # Skrypty i style trafiłyby do treści jako bełkot i zajęły limit słów.
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    body = soup.body or soup
    context.body_snippet = " ".join(body.get_text(" ", strip=True).split()[:CONTEXT_SNIPPET_WORDS])

    site_name_tag = soup.find("meta", attrs={"property": "og:site_name"})
    site_name = site_name_tag.get("content", "").strip() if site_name_tag else ""
    context.brand_name = extract_brand_name(url, title=context.title, site_name=site_name)

    if not context.usable:
        context.error = "Strona nie zawiera treści, z której dałoby się odczytać branżę."

    return context


def _parse_question_list(raw: str) -> list[str]:
    """Wyciąga pytania z odpowiedzi modelu - tablica JSON albo lista w liniach.

    Prosimy o JSON, ale model bywa opakuje go w blok ```json albo zwróci zwykłą listę.
    Oba warianty są poprawnymi pytaniami, więc nie ma powodu ich odrzucać.
    """
    import json
    import re

    tekst = (raw or "").strip()
    if not tekst:
        return []

    blok = re.search(r"\[.*\]", tekst, re.DOTALL)
    if blok:
        try:
            dane = json.loads(blok.group(0))
        except json.JSONDecodeError:
            dane = None
        if isinstance(dane, list):
            pytania = [str(p).strip() for p in dane if str(p).strip()]
            if pytania:
                return pytania

    return [
        linia.strip(' -•\t"')
        for linia in tekst.splitlines()
        if linia.strip(' -•\t"') and not linia.strip().startswith("```")
    ]


def generate_questions(
    domain: str,
    count: int = DEFAULT_QUESTION_COUNT,
    context: SiteContext | None = None,
) -> list[str]:
    """Układa pytania intencyjne dla domeny. Przy błędzie zwraca pytania zapasowe.

    `context` pozwala podać gotowy kontekst strony (np. pobrany raz dla całego
    przepływu). Bez niego pytania powstają na podstawie samej nazwy domeny.
    """
    if context is not None and context.usable:
        prompt = COMMERCIAL_QUESTION_PROMPT.format(count=count, **context.as_prompt_fields())
    else:
        prompt = QUESTION_PROMPT.format(count=count, domain=domain)

    try:
        response = _client().responses.create(model=QUESTION_MODEL, input=prompt)
        questions = _parse_question_list(response.output_text)
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
def ask_once(question: str, domain: str, client=None, brand_name: str = "") -> RunResult:
    """Zadaje jedno pytanie wyszukiwarce AI i mierzy widoczność marki na dwóch poziomach.

    Sam przypis to za mało: model regularnie poleca firmę z nazwy, nie podlinkowując
    jej. Taka odpowiedź nie daje ruchu, ale znaczy, że marka jest w ogóle obecna w
    odpowiedziach - i to inna sytuacja niż całkowita nieobecność.
    """
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
    answer = (response.output_text or "").strip()
    return RunResult(
        answer=answer,
        citations=citations,
        brand_cited=brand_position is not None,
        brand_position=brand_position,
        brand_mentioned=detect_brand_mention(answer, brand_name),
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
                result = ask_once(
                    query.text, study.domain, client=client, brand_name=study.brand_name
                )
                GeoRun.objects.create(
                    query=query,
                    attempt=attempt,
                    answer=result.answer,
                    citations=result.citations,
                    brand_cited=result.brand_cited,
                    brand_position=result.brand_position,
                    brand_mentioned=result.brand_mentioned,
                    visibility=result.visibility,
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
    # Jedna próba = jeden punkt za OBECNOŚĆ marki: link i sama nazwa liczą się tak
    # samo, a próba z obojgiem nadal jako jeden punkt. Rozbicie na linki i wzmianki
    # zostaje informacją szczegółową, nie osobnym procentem.
    visible = [run for run in usable if run.brand_cited or run.brand_mentioned]

    rate = round(len(visible) / len(usable) * 100) if usable else 0
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
    """Odsetek WSZYSTKICH prób, w których marka była obecna.

    Liczymy globalnie (13 obecności na 25 prób = 52%), a nie jako średnią ze średnich
    per pytanie: gdy w którymś pytaniu część wywołań padnie, ma ono mniej prób i średnia
    z procentów dałaby mu tę samą wagę co pytaniu z pełnym kompletem.
    """
    visible = total = 0
    for query in queries:
        for run in query.runs.all():
            if run.error:
                continue
            total += 1
            if run.brand_cited or run.brand_mentioned:
                visible += 1

    return round(visible * 100 / total) if total else 0
