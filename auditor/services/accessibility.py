"""Wstępna weryfikacja dostępności strony dla robotów (pre-flight).

Audyt opiera się na surowym HTML-u. Gdy serwis buduje treść dopiero w przeglądarce
(CSR) albo odrzuca automaty (WAF, Cloudflare, Akamai), parser dostaje pusty szkielet
i KAŻDY test strukturalny wypada negatywnie: "Brak H1", "Brak Schema.org", "Brak
linków wewnętrznych", "Thin content - 12 słów". To nie są błędy strony, tylko jeden
błąd dostępu powielony kilkanaście razy - i najgorszy możliwy raport dla klienta,
bo wskazuje na nieistniejące problemy i ukrywa ten prawdziwy.

Moduł pobiera stronę DWOMA drogami i zestawia je ze sobą:

  * RAW HTML      - zwykłe żądanie HTTP z User-Agentem Googlebota; to widzi prosty
                    crawler i większość agentów modeli językowych,
  * RENDERED DOM  - bezgłowny Chromium po wykonaniu JavaScriptu; to widzi użytkownik
                    i Googlebot w drugiej fazie indeksacji.

Wynik porównania zamienia kilkanaście fałszywych alarmów w jedną, trafną diagnozę.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from . import renderer
from .url_guard import UnsafeUrlError, validate_public_url

logger = logging.getLogger(__name__)

# User-Agent Googlebota. Pre-flight ma odpowiedzieć na pytanie "co widzi robot
# wyszukiwarki", więc pytamy dokładnie jego tożsamością - a nie przeglądarką, którą
# scraper podszywa się na potrzeby pobrania treści.
GOOGLEBOT_USER_AGENT = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)

# Progi diagnozy. Dobrane tak, by rozdzielić trzy sytuacje: pusty szkielet aplikacji
# (kilkanaście słów nawigacji), stronę o realnej treści oraz stronę zablokowaną.
RAW_EMPTY_WORDS = 50       # poniżej tego surowy HTML nie niesie treści merytorycznej
RENDERED_RICH_WORDS = 300  # powyżej tego wyrenderowany DOM ma realną zawartość

# Selektory nakładek zgody na cookies. Baner OneTrust czy Cookiebot potrafi przykryć
# DOM overlayem i - przy niektórych wdrożeniach - wstrzymać doładowanie treści, więc
# przed pomiarem usuwamy go ze strony.
CONSENT_SELECTORS = (
    "#onetrust-consent-sdk", "#onetrust-banner-sdk", ".onetrust-pc-dark-filter",
    "#CybotCookiebotDialog", "#CybotCookiebotDialogBodyUnderlay",
    "#cookiescript_injected", "#usercentrics-root", "[id^='sp_message_container']",
    ".cookie-consent", ".cookie-banner", "#cookie-banner", "#cookie-law-info-bar",
)

# Przyciski akceptacji - klikamy je przed ukryciem nakładki, bo część wdrożeń
# doładowuje treść dopiero po wyrażeniu zgody.
CONSENT_ACCEPT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "button[aria-label='Akceptuj wszystkie']",
    "button[id*='accept' i]",
)

DIAGNOSIS_OK = "ok"
DIAGNOSIS_CSR = "csr"
# Treść serwowana wyłącznie zadeklarowanym botom (Prerender.io, Rendertron i podobne
# usługi "dynamic rendering"). Audyt widzi pustą powłokę, Googlebot - gotowy snapshot.
DIAGNOSIS_PRERENDER_GATED = "prerender_gated"
DIAGNOSIS_BLOCKED = "blocked"
DIAGNOSIS_UNKNOWN = "unknown"


@dataclass
class RenderSnapshot:
    """Pomiar jednego wariantu pobrania strony."""

    available: bool = False
    error: str | None = None
    status_code: int | None = None
    word_count: int = 0
    h1_count: int = 0
    h1_text: str | None = None
    schema_blocks: int = 0
    internal_links: int = 0

    @property
    def has_h1(self) -> bool:
        return self.h1_count > 0

    @property
    def has_schema(self) -> bool:
        return self.schema_blocks > 0


@dataclass
class AccessibilityReport:
    """Wynik pre-flightu wraz z gotową diagnozą i tabelą porównawczą."""

    url: str
    # To, co faktycznie parsuje audyt - tym samym scraperem i tymi samymi nagłówkami.
    # Wyłącznik bezpieczeństwa opiera się WYŁĄCZNIE na tym pomiarze.
    raw: RenderSnapshot = field(default_factory=RenderSnapshot)
    # Sonda tożsamością Googlebota. Różnica względem `raw` ujawnia serwowanie treści
    # tylko zadeklarowanym botom (dynamic rendering).
    bot: RenderSnapshot = field(default_factory=RenderSnapshot)
    rendered: RenderSnapshot = field(default_factory=RenderSnapshot)
    diagnosis: str = DIAGNOSIS_UNKNOWN
    note: str = ""
    comparison: list[dict] = field(default_factory=list)

    @property
    def is_accessible(self) -> bool:
        """Czy surowy HTML nadaje się do oceny testami strukturalnymi.

        Tylko diagnoza "ok" zwalnia audyt z wyłączenia testów składowych. Przy
        nieznanej diagnozie (np. renderowanie niedostępne) również zwracamy True -
        brak rozstrzygnięcia nie może wyłączać testów, bo wtedy każda instalacja bez
        Playwrighta straciłaby połowę audytu.
        """
        return self.diagnosis in (DIAGNOSIS_OK, DIAGNOSIS_UNKNOWN)

    @property
    def is_prerender_gated(self) -> bool:
        """Czy treść jest serwowana wyłącznie zadeklarowanym botom."""
        return self.diagnosis == DIAGNOSIS_PRERENDER_GATED

    @property
    def checked(self) -> bool:
        """Czy porównanie w ogóle doszło do skutku (oba pobrania się powiodły)."""
        return self.diagnosis != DIAGNOSIS_UNKNOWN

    def as_dict(self) -> dict:
        """Postać zapisywana w metryce (JSONField) i renderowana w interfejsie."""
        return {
            "checked": self.checked,
            "is_accessible": self.is_accessible,
            "diagnosis": self.diagnosis,
            "note": self.note,
            "comparison": self.comparison,
            "raw": {
                "available": self.raw.available,
                "status_code": self.raw.status_code,
                "error": self.raw.error,
                "word_count": self.raw.word_count,
                "h1_count": self.raw.h1_count,
                "schema_blocks": self.raw.schema_blocks,
                "internal_links": self.raw.internal_links,
            },
            "bot": {
                "available": self.bot.available,
                "status_code": self.bot.status_code,
                "error": self.bot.error,
                "word_count": self.bot.word_count,
                "h1_count": self.bot.h1_count,
                "schema_blocks": self.bot.schema_blocks,
                "internal_links": self.bot.internal_links,
            },
            "rendered": {
                "available": self.rendered.available,
                "error": self.rendered.error,
                "word_count": self.rendered.word_count,
                "h1_count": self.rendered.h1_count,
                "schema_blocks": self.rendered.schema_blocks,
                "internal_links": self.rendered.internal_links,
            },
        }


def check_bot_accessibility(url: str, scraper=None) -> AccessibilityReport:
    """Porównuje surowy HTML z wyrenderowanym DOM i stawia diagnozę dostępności.

    `scraper` pozwala wstrzyknąć gotową instancję `SEOScraper` (audyt ma już swoją) -
    dzięki temu pre-flight korzysta z tych samych limitów czasu, ponowień i ochrony
    przed SSRF, co reszta pobierania.
    """
    report = AccessibilityReport(url=url)

    try:
        safe_url = validate_public_url(url)
    except UnsafeUrlError as exc:
        report.raw.error = str(exc)
        report.note = f"Adres odrzucony przez walidację: {exc}"
        return report

    report.raw = _measure_raw(safe_url, scraper, user_agent=None)
    report.bot = _measure_raw(safe_url, scraper, user_agent=GOOGLEBOT_USER_AGENT)
    report.rendered = _measure_rendered(safe_url)
    report.diagnosis, report.note = _diagnose(report.raw, report.bot, report.rendered)
    report.comparison = _build_comparison(report.raw, report.bot, report.rendered)
    return report


# ----------------------------------------------------------------------
# Pomiary
# ----------------------------------------------------------------------
def _measure_raw(url: str, scraper=None, user_agent: str | None = None) -> RenderSnapshot:
    """Surowy HTML bez wykonywania JavaScriptu.

    Bez `user_agent` pytamy nagłówkami audytu - czyli mierzymy dokładnie ten HTML,
    z którego liczone są metryki. Z `user_agent` Googlebota sondujemy, czy serwis
    nie serwuje innej treści zadeklarowanym botom.
    """
    import httpx

    from .scraper import SEOScraper, ScraperError

    snapshot = RenderSnapshot()
    scraper = scraper or SEOScraper()

    try:
        html = scraper.fetch_raw(url, user_agent=user_agent)
    except ScraperError as exc:
        snapshot.error = str(exc)
        snapshot.status_code = _extract_status_code(exc)
        return snapshot
    except httpx.HTTPError as exc:
        snapshot.error = f"{type(exc).__name__}: {exc}"
        return snapshot

    snapshot.available = True
    snapshot.status_code = 200
    _fill_from_html(snapshot, html, url)
    return snapshot


def _measure_rendered(url: str) -> RenderSnapshot:
    """DOM po wykonaniu JavaScriptu - oczami użytkownika i pełnego Googlebota."""
    snapshot = RenderSnapshot()

    try:
        html = renderer.render_html(url, dismiss_consent=True)
    except renderer.RendererUnavailableError as exc:
        snapshot.error = f"Renderowanie niedostępne: {exc}"
        return snapshot
    except renderer.RendererError as exc:
        snapshot.error = str(exc)
        return snapshot

    snapshot.available = True
    _fill_from_html(snapshot, html, url)
    return snapshot


def _fill_from_html(snapshot: RenderSnapshot, html: str, url: str) -> None:
    soup = BeautifulSoup(html or "", "html.parser")

    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    snapshot.word_count = len(soup.get_text(separator=" ", strip=True).split())

    headings = [h.get_text(strip=True) for h in soup.find_all("h1")]
    non_empty = [h for h in headings if h]
    snapshot.h1_count = len(non_empty)
    snapshot.h1_text = non_empty[0] if non_empty else None

    # Bloki JSON-LD liczymy na NIEoczyszczonym drzewie - decompose() usunął <script>.
    snapshot.schema_blocks = len(
        re.findall(r'type=["\']application/ld\+json["\']', html or "", re.I)
    )
    snapshot.internal_links = _count_internal_links(soup, url)


def _count_internal_links(soup: BeautifulSoup, url: str) -> int:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    count = 0
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        target_host = (urlparse(href).hostname or "").lower()
        if not target_host or target_host == host:
            count += 1
    return count


def _extract_status_code(exc: Exception) -> int | None:
    """Wyłuskuje kod HTTP z komunikatu ScraperError (np. "... HTTP 403 ...")."""
    match = re.search(r"\b(4\d\d|5\d\d)\b", str(exc))
    return int(match.group(1)) if match else None


# ----------------------------------------------------------------------
# Diagnoza
# ----------------------------------------------------------------------
def _diagnose(
    raw: RenderSnapshot, bot: RenderSnapshot, rendered: RenderSnapshot
) -> tuple[str, str]:
    """Zamienia trzy pomiary w jedno zdanie, które trafia do raportu.

    Punktem odniesienia jest ZAWSZE `raw` - HTML, z którego audyt liczy metryki.
    `bot` i `rendered` służą wyłącznie do wyjaśnienia, DLACZEGO `raw` jest pusty.
    """
    raw_empty = raw.available and raw.word_count < RAW_EMPTY_WORDS
    bot_rich = bot.available and bot.word_count > RENDERED_RICH_WORDS
    rendered_rich = rendered.available and rendered.word_count > RENDERED_RICH_WORDS

    # 1. Audyt w ogóle nie dostał odpowiedzi.
    if not raw.available:
        if bot.available or rendered.available:
            kod = f" (HTTP {raw.status_code})" if raw.status_code else ""
            zrodlo = "przeglądarka" if rendered.available else "robot Google"
            return DIAGNOSIS_BLOCKED, (
                f"Serwer odrzucił żądanie audytu{kod}, choć {zrodlo} otrzymał stronę. "
                "To blokada wymierzona w automaty (WAF/CDN) - treść jest niedostępna "
                "dla narzędzi analitycznych i części agentów AI."
            )
        return DIAGNOSIS_BLOCKED, (
            f"Strona nie odpowiedziała na żadne z żądań ({raw.error or 'brak odpowiedzi'}). "
            "Audyt nie ma czego oceniać."
        )

    # 2. Audyt dostał powłokę, ale Googlebot dostaje pełną treść - dynamic rendering.
    if raw_empty and bot_rich:
        return DIAGNOSIS_PRERENDER_GATED, (
            f"Treść serwowana jest wyłącznie zadeklarowanym botom: żądanie audytu "
            f"otrzymało {raw.word_count} słów, a to samo żądanie z tożsamością "
            f"Googlebota - {bot.word_count}. To wzorzec usług typu Prerender.io/"
            "dynamic rendering. Google widzi stronę, ale narzędzia analityczne "
            "i agenty AI (GPTBot, ClaudeBot, PerplexityBot) dostają pustą powłokę."
        )

    # 3. Audyt dostał powłokę, treść powstaje dopiero w przeglądarce - klasyczny CSR.
    if raw_empty and rendered_rich:
        return DIAGNOSIS_CSR, (
            f"Wykryto renderowanie po stronie klienta (CSR): surowy HTML ma "
            f"{raw.word_count} słów, a DOM po wykonaniu JavaScriptu - "
            f"{rendered.word_count}. Strona jest niewidoczna dla prostych robotów "
            "i agentów AI, które nie wykonują JavaScriptu."
        )

    # 4. Pusto wszędzie - najpewniej challenge zabezpieczający.
    if raw_empty and not bot_rich and not rendered_rich:
        if not rendered.available and not bot.available:
            return DIAGNOSIS_UNKNOWN, (
                f"Surowy HTML zawiera tylko {raw.word_count} słów, ale nie udało się "
                "tego z niczym porównać - brak wyniku sondy i renderowania. "
                "Diagnoza niepewna."
            )
        return DIAGNOSIS_BLOCKED, (
            f"Każde żądanie zwraca pustą stronę (audyt: {raw.word_count} słów, "
            f"Googlebot: {bot.word_count}, przeglądarka: {rendered.word_count}). "
            "Najprawdopodobniej serwis wyświetla challenge zabezpieczający zamiast treści."
        )

    return DIAGNOSIS_OK, (
        f"Surowy HTML zawiera treść ({raw.word_count} słów) - roboty bez obsługi "
        "JavaScriptu widzą stronę poprawnie."
    )


def _build_comparison(
    raw: RenderSnapshot, bot: RenderSnapshot, rendered: RenderSnapshot
) -> list[dict]:
    """Tabela porównawcza pokazywana w raporcie.

    Trzy kolumny, bo dwie nie wystarczyły: serwis za dynamic renderingiem wygląda
    poprawnie w zestawieniu "robot vs przeglądarka", a problem ujawnia się dopiero
    w zestawieniu "audyt vs zadeklarowany bot".
    """

    def obecnosc(snapshot: RenderSnapshot, ile: int) -> str:
        if not snapshot.available:
            return "—"
        return f"Znaleziono ({ile})" if ile else "Brak"

    def liczba(snapshot: RenderSnapshot, wartosc: int) -> str:
        return str(wartosc) if snapshot.available else "—"

    def rozne(*wartosci) -> bool:
        obecne = [w for w in wartosci if w is not None]
        return len(set(obecne)) > 1

    return [
        {
            "label": "Liczba słów treści",
            "raw": liczba(raw, raw.word_count),
            "bot": liczba(bot, bot.word_count),
            "rendered": liczba(rendered, rendered.word_count),
            "differs": rozne(
                raw.word_count if raw.available else None,
                bot.word_count if bot.available else None,
                rendered.word_count if rendered.available else None,
            ),
        },
        {
            "label": "Nagłówek H1",
            "raw": obecnosc(raw, raw.h1_count),
            "bot": obecnosc(bot, bot.h1_count),
            "rendered": obecnosc(rendered, rendered.h1_count),
            "differs": rozne(
                raw.has_h1 if raw.available else None,
                bot.has_h1 if bot.available else None,
                rendered.has_h1 if rendered.available else None,
            ),
        },
        {
            "label": "Dane strukturalne JSON-LD",
            "raw": obecnosc(raw, raw.schema_blocks),
            "bot": obecnosc(bot, bot.schema_blocks),
            "rendered": obecnosc(rendered, rendered.schema_blocks),
            "differs": rozne(
                raw.has_schema if raw.available else None,
                bot.has_schema if bot.available else None,
                rendered.has_schema if rendered.available else None,
            ),
        },
        {
            "label": "Linki wewnętrzne",
            "raw": liczba(raw, raw.internal_links),
            "bot": liczba(bot, bot.internal_links),
            "rendered": liczba(rendered, rendered.internal_links),
            "differs": rozne(
                raw.internal_links if raw.available else None,
                bot.internal_links if bot.available else None,
                rendered.internal_links if rendered.available else None,
            ),
        },
    ]
