"""Renderowanie strony w bezgłownej przeglądarce - fallback dla CSR i WAF-ów.

Statyczne pobranie HTML (httpx) zawodzi w dwóch sytuacjach, których nie da się
obejść samymi nagłówkami:

* **CSR (Client-Side Rendering)** - aplikacje React/Vue/Angular odsyłają niemal pusty
  dokument z `<div id="root">`, a `<title>`, `meta description` i cała treść powstają
  dopiero po wykonaniu JavaScriptu. Parser widzi wtedy stronę bez podstawowych tagów
  SEO i audyt zgłasza błędy, których na stronie nie ma.
* **WAF / Cloudflare** - odpowiedź 403/429 na żądanie bez pełnego profilu przeglądarki
  (JS challenge, cookies, TLS fingerprint). Prawdziwa przeglądarka przechodzi challenge
  i dostaje właściwą treść.

Moduł jest CELOWO opcjonalny: `playwright` nie jest zależnością wymaganą, bo obraz
workera Celery z przeglądarką waży setki megabajtów. Gdy biblioteki lub binarki
przeglądarki brakuje, `render_html` podnosi `RendererUnavailableError`, a scraper
zostaje przy wyniku statycznym - audyt nigdy nie przerywa się z powodu braku Playwrighta.

Instalacja (opcjonalna):

    pip install playwright
    playwright install chromium
"""
from __future__ import annotations

import logging
from importlib.util import find_spec

from django.conf import settings

from .url_guard import UnsafeUrlError, is_public_url, validate_public_url

logger = logging.getLogger(__name__)

# Typy zasobów pomijane podczas renderowania. Audyt czyta DOM, a nie wygląd strony -
# obrazki, czcionki i wideo potrafią stanowić większość czasu ładowania, nie wnosząc
# nic do analizy. Arkusze CSS zostawiamy, bo `_analyze_hidden_content` i
# `_analyze_heading_visibility` opierają się na stylach zapisanych w dokumencie.
BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})

# Po zdarzeniu `domcontentloaded` dajemy skryptom czas na dorysowanie treści. Twardy
# `networkidle` bywa nieosiągalny na stronach z odpytywaniem w tle (czaty, analityka),
# więc jego przekroczenie NIE jest błędem - po prostu czytamy DOM w takim stanie,
# w jakim jest.
NETWORK_IDLE_TIMEOUT_MS = 5_000

# Limity obsługi nakładki zgody na cookies. Krótkie celowo: baner albo jest od razu,
# albo go nie ma - czekanie na niego wydłużałoby każde renderowanie bez powodu.
CONSENT_TIMEOUT_MS = 1_500
CONSENT_SETTLE_MS = 500


class RendererError(RuntimeError):
    """Renderowanie nie powiodło się (błąd nawigacji, timeout przeglądarki)."""


class RendererUnavailableError(RendererError):
    """Playwright nie jest zainstalowany albo brakuje binarki przeglądarki."""


def is_available() -> bool:
    """Czy fallback przeglądarkowy da się w ogóle uruchomić.

    Sprawdzamy sam fakt obecności pakietu, bez importu - import Playwrighta jest
    kosztowny, a ta funkcja bywa wywoływana przy każdym pobraniu strony.
    """
    if not getattr(settings, "SCRAPER_RENDER_FALLBACK_ENABLED", True):
        return False
    return find_spec("playwright") is not None


def render_html(
    url: str,
    *,
    timeout_seconds: float | None = None,
    user_agent: str | None = None,
    extra_headers: dict | None = None,
    dismiss_consent: bool = False,
) -> str:
    """Zwraca HTML strony po wykonaniu JavaScriptu przez bezgłownego Chromium.

    Adres przechodzi tę samą walidację co w scraperze, a dodatkowo KAŻDE żądanie
    wychodzące z przeglądarki jest sprawdzane w locie: strona może przekierować albo
    pobrać zasób spod adresu w sieci lokalnej, a przeglądarka - w odróżnieniu od
    `SEOScraper._fetch_once` - obsługuje przekierowania sama i nie da się ich
    zwalidować "przed" żądaniem inaczej niż przez przechwycenie ruchu.
    """
    if not is_available():
        raise RendererUnavailableError(
            "Fallback przeglądarkowy jest niedostępny "
            "(zainstaluj: pip install playwright && playwright install chromium)."
        )

    try:
        safe_url = validate_public_url(url)
    except UnsafeUrlError as exc:
        raise RendererError(f"Nie udało się wyrenderować {url}: {exc}") from exc

    timeout_ms = int(
        (timeout_seconds or getattr(settings, "SCRAPER_RENDER_TIMEOUT_SECONDS", 30.0)) * 1000
    )

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RendererUnavailableError(
            "Pakiet playwright nie jest zainstalowany (pip install playwright)."
        ) from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                # Domyślne flagi Chromium zdradzają automat (navigator.webdriver,
                # baner "sterowana przez oprogramowanie"). Wyłączenie ich nie jest
                # pełnym maskowaniem, ale usuwa najbardziej oczywisty sygnał.
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            try:
                context = browser.new_context(
                    user_agent=user_agent,
                    extra_http_headers=_navigation_headers(extra_headers),
                    locale="pl-PL",
                    viewport={"width": 1920, "height": 1080},
                    java_script_enabled=True,
                )
                context.set_default_timeout(timeout_ms)
                context.route("**/*", _guard_route)

                page = context.new_page()
                page.goto(safe_url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
                except PlaywrightError:
                    # Strona nigdy nie ucisza sieci (czat, analityka, long-polling) -
                    # DOM i tak jest już zbudowany, więc czytamy go w tym stanie.
                    logger.debug("Strona %s nie osiągnęła stanu networkidle - czytam DOM.", safe_url)
                if dismiss_consent:
                    _dismiss_consent(page)
                return page.content()
            finally:
                browser.close()
    except RendererError:
        raise
    except Exception as exc:
        # Playwright rzuca własną hierarchią wyjątków (w tym przy braku binarki
        # przeglądarki), a poza nią zdarzają się błędy systemowe uruchomienia procesu.
        # Dla wywołującego liczy się jedno: renderowanie nie dało wyniku.
        if _looks_like_missing_browser(exc):
            raise RendererUnavailableError(
                "Brakuje binarki przeglądarki - uruchom: playwright install chromium."
            ) from exc
        raise RendererError(f"Nie udało się wyrenderować {url}: {exc}") from exc


def _dismiss_consent(page) -> None:
    """Zamyka nakładkę zgody na cookies, żeby nie przykrywała mierzonego DOM.

    Najpierw próbujemy kliknąć "akceptuj wszystkie" - część wdrożeń doładowuje treść
    dopiero po wyrażeniu zgody, więc samo ukrycie banera nie wystarcza. Dopiero potem
    usuwamy to, co zostało. Żaden błąd nie może przerwać renderowania: nakładki nie
    ma na większości stron, a brak zgody i tak zwykle nie blokuje treści.
    """
    from .accessibility import CONSENT_ACCEPT_SELECTORS, CONSENT_SELECTORS

    for selector in CONSENT_ACCEPT_SELECTORS:
        try:
            button = page.locator(selector).first
            if button.count() and button.is_visible(timeout=CONSENT_TIMEOUT_MS):
                button.click(timeout=CONSENT_TIMEOUT_MS)
                page.wait_for_timeout(CONSENT_SETTLE_MS)
                break
        except Exception:
            logger.debug("Nie udało się kliknąć zgody selektorem %s.", selector)

    try:
        page.evaluate(
            "selektory => selektory.forEach("
            "  s => document.querySelectorAll(s).forEach(el => el.remove()))",
            list(CONSENT_SELECTORS),
        )
        # Banery blokują przewijanie przez overflow:hidden na <body> - przywracamy je,
        # bo część stron doczytuje treść dopiero przy scrollu.
        page.evaluate(
            "() => { document.documentElement.style.overflow = 'auto';"
            " document.body.style.overflow = 'auto'; }"
        )
    except Exception:
        logger.debug("Nie udało się usunąć nakładki zgody ze strony.")


def _navigation_headers(extra_headers: dict | None) -> dict:
    """Nagłówki przekazywane przeglądarce.

    Playwright sam ustawia `User-Agent`, `Accept-Encoding` i Client Hints spójnie z
    uruchomioną wersją Chromium, więc podanie ich ręcznie tworzyłoby sprzeczność
    (zadeklarowana wersja vs. rzeczywisty odcisk) - czyli dokładnie ten sygnał, przed
    którym fallback ma chronić.
    """
    if not extra_headers:
        return {}
    pomijane = {"user-agent", "accept-encoding", "connection", "upgrade-insecure-requests"}
    return {
        nazwa: wartość
        for nazwa, wartość in extra_headers.items()
        if nazwa.lower() not in pomijane and not nazwa.lower().startswith("sec-")
    }


def _guard_route(route, request) -> None:
    """Przepuszcza żądanie tylko wtedy, gdy prowadzi do adresu publicznego.

    To ochrona przed SSRF wewnątrz przeglądarki: `page.goto` podąża za
    przekierowaniami samodzielnie, a strona może wskazać zasób pod `127.0.0.1`
    czy `169.254.169.254`. Każde żądanie - łącznie z tymi powstałymi z przekierowań -
    przechodzi tu przed wysłaniem.
    """
    if request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
        return
    if not is_public_url(request.url):
        logger.info("Renderowanie: odrzucono żądanie do niedozwolonego adresu %s.", request.url)
        route.abort()
        return
    route.continue_()


def _looks_like_missing_browser(exc: Exception) -> bool:
    """Odróżnia brak zainstalowanej przeglądarki od zwykłego błędu renderowania.

    Playwright nie ma osobnego typu wyjątku dla tej sytuacji - zgłasza ją zwykłym
    `Error` z instrukcją instalacji w treści.
    """
    komunikat = str(exc).lower()
    return "playwright install" in komunikat or "executable doesn't exist" in komunikat
