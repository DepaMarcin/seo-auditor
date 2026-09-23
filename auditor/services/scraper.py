from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup

from . import renderer
from .url_guard import MAX_REDIRECT_HOPS, UnsafeUrlError, validate_public_url

logger = logging.getLogger(__name__)

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

# ------------------------------------------------------------------
# Pobieranie strony: limity czasu, nagłówki i ponawianie
# ------------------------------------------------------------------
# Nawiązanie połączenia musi być szybkie - host, który nie odpowiada na handshake,
# nie ma po co blokować audytu. Na samo wygenerowanie strony dajemy znacznie więcej
# czasu: serwisy na współdzielonym hostingu potrafią budować stronę główną 10-20 s.
CONNECT_TIMEOUT_SECONDS = 15.0
READ_TIMEOUT_SECONDS = 30.0

# Limit odczytu ROŚNIE z każdą kolejną próbą. Pomiary na niestabilnym serwerze
# pokazały rozrzut od 7 s do 39 s dla tej samej strony, więc stały limit wymuszałby
# wybór między szybkim audytem a skutecznością. Pierwsze podejście jest szybkie,
# a dopiero ostatnie naprawdę cierpliwe - strony sprawne kończą się w kilka sekund.
READ_TIMEOUT_MULTIPLIERS = (1.0, 1.5, 2.0)

# Ile razy łącznie próbujemy pobrać stronę (pierwsze podejście + ponowienia).
FETCH_MAX_ATTEMPTS = 3

# Odstęp między próbami. Krótki, bo audyt skanuje do 5 szablonów i każda sekunda
# zwłoki mnoży się przez liczbę podstron.
FETCH_RETRY_BACKOFF_SECONDS = 1.5

# Wersja Chrome deklarowana w User-Agencie i w Client Hints. JEDNO miejsce do
# aktualizacji - rozjechanie się tych dwóch deklaracji to sygnał automatu wyraźniejszy
# niż sam nieaktualny numer wersji, dlatego oba nagłówki budujemy z tej stałej.
#
# Wersja mocno wyprzedzająca rzeczywistość też szkodzi: WAF zestawia ją z odciskiem
# TLS i kolejnością nagłówków, więc "Chrome z przyszłości" wygląda podejrzanie.
# Przy aktualizacji podnieś numer do wersji, która NAPRAWDĘ jest wydana.
CHROME_MAJOR_VERSION = 141

# User-Agent pierwszej próby: pełnoprawna przeglądarka desktopowa. Systemy antybotowe
# (Cloudflare, Imperva) odrzucają nieznane automaty regułą domyślną, więc uczciwe
# przedstawienie się botem kosztowało audyt dostęp do części witryn.
DEFAULT_USER_AGENT = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR_VERSION}.0.0.0 Safari/537.36"
)

# Zapasowy User-Agent uczciwie przedstawiający bota. Część serwerów działa odwrotnie
# niż WAF-y: przepuszcza zadeklarowane roboty (bo chce być indeksowana), a blokuje
# automaty udające przeglądarkę. Zamiast zgadywać, która strategia zadziała na danym
# hoście, kolejne próby ROTUJĄ User-Agenta.
FALLBACK_USER_AGENT = "SEOAuditorBot/1.0 (+https://example.com)"

# Client Hints wysyłane wyłącznie razem z User-Agentem przeglądarki - patrz
# `SEOScraper._build_headers`. Chrome wysyła je przy KAŻDYM żądaniu nawigacyjnym,
# więc ich brak przy UA deklarującym Chrome jest jednym z prostszych testów na automat.
BROWSER_CLIENT_HINTS = {
    "sec-ch-ua": (
        f'"Chromium";v="{CHROME_MAJOR_VERSION}", '
        f'"Google Chrome";v="{CHROME_MAJOR_VERSION}", '
        f'"Not?A_Brand";v="24"'
    ),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Kody odpowiedzi, przy których ma sens sięgnięcie po bezgłowną przeglądarkę:
# 403 to typowa odmowa WAF-a, 429 - limit żądań nakładany na ruch uznany za automat.
# Powtórzenie tego samego żądania statycznego nic by nie zmieniło (patrz `fetch`).
RENDER_FALLBACK_STATUS_CODES = (403, 429)

# User-Agent Googlebota używany przy ponowieniu, gdy serwer odda audytowi pustą
# powłokę aplikacji. Nie chodzi o obejście zabezpieczeń - snapshot serwowany
# Googlebotowi jest publicznie dostępny, a audyt ma ocenić dokładnie to, co widzi
# wyszukiwarka. Tak samo działają komercyjne crawlery SEO.
GOOGLEBOT_RETRY_USER_AGENT = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)

# Poniżej tylu słów uznajemy odpowiedź za powłokę bez treści, a nie za stronę.
STUB_WORD_THRESHOLD = 50

# Boty modeli językowych (LLM), których zablokowanie wyklucza witrynę z odpowiedzi
# generowanych przez AI - kluczowe dla GEO (Generative Engine Optimization).
#
# "Google-Extended" nie jest crawlerem w zwykłym sensie: Google używa go wyłącznie jako
# przełącznika zgody na wykorzystanie treści w Gemini i AI Overviews. Jego zablokowanie
# NIE wpływa na zwykłą indeksację w wyszukiwarce, ale wyklucza stronę z odpowiedzi AI.
AI_BOT_USER_AGENTS = ("GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended", "Bytespider")

# Sygnały ukrycia treści możliwe do wykrycia w statycznym HTML (bez renderowania CSS).
_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_HIDDEN_CLASS_RE = re.compile(
    r"(^|\s)(is-hidden|d-none|hidden|hide|sr-only|visually-hidden|screen-reader-text)(\s|$)", re.I
)
_HIDEABLE_BLOCK_TAGS = ("div", "section", "article", "aside", "ul", "ol", "dl", "table", "p")

# Minimalna liczba słów, od której ukryty blok uznajemy za istotną utratę treści.
HIDDEN_BLOCK_MIN_WORDS = 25

# Elementy, w których realnie występuje cena, oraz wzorce jej rozpoznania.
_PRICE_CARRIER_TAGS = ("span", "div", "p", "b", "strong", "em", "ins", "bdi", "td", "dd", "li")
_PRICE_HINT_RE = re.compile(r"(zł|pln|eur|usd|€|\$|gbp|£)", re.I)
_PRICE_NUMBER_RE = re.compile(r"\d[\d\s\u00a0.,]*\d|\d")

# Domeny uznawane za autorytatywne źródła zewnętrzne (weryfikacja faktów w treści).
_TRUSTED_DOMAIN_RE = re.compile(
    # (^|\.) obejmuje zarówno "gov.pl", jak i "www.sejm.gov.pl" - bez tego domeny
    # rządowe i edukacyjne bez subdomeny nie były rozpoznawane jako źródła zaufane.
    r"((^|\.)gov(\.[a-z]{2})?$|(^|\.)edu(\.[a-z]{2})?$|\.ac\.uk$"
    r"|wikipedia\.org$|who\.int$|europa\.eu$|nature\.com$|ncbi\.nlm\.nih\.gov$)", re.I
)

# Teksty zastępcze, które nie powinny trafić na produkcję.
PLACEHOLDER_TEXT_MARKERS = (
    "lorem ipsum", "dolor sit amet", "tekst zastępczy", "tekst do uzupełnienia",
    "opis w przygotowaniu", "wpisz opis", "todo:", "placeholder",
    "brak opisu", "przykładowy tekst",
)

# Maksymalna głębokość rozwijania dokumentu JSON-LD przy spłaszczaniu encji.
SCHEMA_MAX_DEPTH = 12

# Zakres długości akapitu uznawanego za bezpośrednią odpowiedź pod nagłówkiem sekcji
# (wzorzec "Answer-First" w optymalizacji pod modele językowe).
ANSWER_FIRST_MIN_WORDS = 20
ANSWER_FIRST_MAX_WORDS = 40

# Próg (liczba słów widocznego tekstu) i minimalna liczba <script>, poniżej/powyżej
# których strona jest podejrzewana o renderowanie wyłącznie po stronie klienta (CSR) -
# treść "pusta" bez wykonania JS jest niewidoczna dla części robotów/modeli LLM.
JS_CSR_WORD_COUNT_THRESHOLD = 80
JS_CSR_MIN_SCRIPT_COUNT = 3

# Limit wagi pliku graficznego (KB), powyżej którego zgłaszamy problem z kompresją.
IMAGE_SIZE_LIMIT_KB = 100
# Ile obrazków sprawdzamy realnie (żądaniami HEAD) - reszta jest pomijana, żeby nie
# wydłużać audytu w nieskończoność na stronach z dziesiątkami zdjęć.
IMAGE_SIZE_CHECK_LIMIT = 8

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)

# Frazy typowe dla elementów nawigacyjnych/szablonowych, a nie treści merytorycznej.
HX_NOISE_KEYWORDS = (
    "zobacz też",
    "czytaj też",
    "newsletter",
    "popularne",
    "polecane",
    "powiązane",
    "najnowsze wpisy",
    "udostępnij",
    "skomentuj",
    "subskrybuj",
)

# Walidacji atrybutu ALT podlegają wyłącznie standardowe rastrowe pliki graficzne.
# Grafiki wektorowe (SVG) są zwykle ikonami/elementami UI, dla których wymóg ALT
# nie ma sensu biznesowego i sztucznie zawyżałby liczbę wykrytych błędów.
RASTER_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_DECORATIVE_HINT_RE = re.compile(r"\bicons?\b|\bplaceholder\b|\bdecorative\b", re.I)

# ------------------------------------------------------------------
# Klasyfikacja typu podstrony (używana do walidacji oczekiwanych typów Schema.org)
# ------------------------------------------------------------------
PAGE_TYPE_HOMEPAGE = "homepage"
PAGE_TYPE_PRODUCT = "product"
PAGE_TYPE_ARTICLE = "article"
PAGE_TYPE_CATEGORY = "category"
PAGE_TYPE_GENERIC = "generic"

# ------------------------------------------------------------------
# Intencja podstrony - redakcyjna czy komercyjna
# ------------------------------------------------------------------
# Podział prostszy niż `page_type` i służący jednemu celowi: rozstrzygnięciu, czy od
# strony należy wymagać imiennego autora (encji Person). Na artykule brak autora to
# realna luka E-E-A-T; na stronie ofertowej B2B podmiotem odpowiedzialnym jest firma,
# więc żądanie autora to fałszywy alarm - i dokładnie taki zgłaszał audyt dla
# orlen.pl/pl/dla-biznesu/karty-i-uslugi-flotowe.
PAGE_INTENT_EDITORIAL = "editorial"
PAGE_INTENT_COMMERCIAL = "commercial"

# Typy Schema.org jednoznacznie wskazujące treść redakcyjną.
EDITORIAL_SCHEMA_TYPES = frozenset({
    "article", "blogposting", "newsarticle", "techarticle",
    "scholarlyarticle", "liveblogposting", "report", "advertisercontentarticle",
})

# Typy Schema.org wskazujące ofertę. Świadomie NIE ma tu "WebPage" - to typ domyślny,
# występujący także na artykułach, więc nie rozstrzyga o niczym.
COMMERCIAL_SCHEMA_TYPES = frozenset({
    "product", "service", "offer", "financialproduct", "loanorcredit",
    "itemlist", "collectionpage", "store", "localbusiness",
})

# Segmenty ścieżki typowe dla treści redakcyjnej (polskie i angielskie).
EDITORIAL_PATH_SEGMENTS = (
    "/blog", "/poradnik", "/poradniki", "/porady", "/artykul", "/artykuly",
    "/aktualnosci", "/baza-wiedzy", "/wiedza", "/news", "/nowosci", "/magazyn",
    "/przewodnik", "/case-stud", "/insights", "/publikacje",
)

# Segmenty ścieżki typowe dla treści ofertowej.
COMMERCIAL_PATH_SEGMENTS = (
    "/dla-biznesu", "/dla-firm", "/biznes", "/b2b", "/oferta", "/oferty",
    "/uslugi", "/usluga", "/produkt", "/produkty", "/cennik", "/cenniki",
    "/sklep", "/kategoria", "/karty", "/rozwiazania", "/pakiety", "/abonament",
)

PRODUCT_PATH_SEGMENTS = ("/produkt/", "/p/", "/item/")
ARTICLE_PATH_SEGMENTS = ("/blog/", "/artykul/")
CATEGORY_PATH_SEGMENTS = ("/kategoria/", "/category/")

_CART_OR_PRICE_CLASS_RE = re.compile(
    r"add-to-cart|add_to_cart|dodaj-do-koszyka|do-koszyka|\bkoszyk\b|\bprice\b|\bcena\b", re.I
)
_PRODUCT_LIST_CLASS_RE = re.compile(
    r"product-item|product-card|product-list|produkt-item|products-list|listing-item", re.I
)

# Frazy typowe dla nagłówków sekcji FAQ.
FAQ_HEADING_KEYWORDS = (
    "faq",
    "często zadawane pytania",
    "czesto zadawane pytania",
    "pytania i odpowiedzi",
)
_FAQ_CONTAINER_CLASS_RE = re.compile(r"\bfaq\b|accordion", re.I)
_FAQ_CONTAINER_ID_RE = re.compile(r"\bfaq\b", re.I)
_SCHEMA_QUESTION_TYPE_RE = re.compile(r"schema\.org/Question", re.I)


def _is_browser_user_agent(user_agent: str) -> bool:
    """Czy dany User-Agent podaje się za przeglądarkę (a nie za robota)."""
    return "Mozilla/" in (user_agent or "")


def _supported_accept_encoding() -> str:
    """Lista kodowań, które klient POTRAFI rozpakować.

    Deklarowanie `br` bez zainstalowanego pakietu `brotli` było realnym błędem:
    Cloudflare chętnie odsyła brotli, a httpx bez dekodera podnosi wtedy
    `DecodingError` - pobranie kończyło się niepowodzeniem mimo poprawnej odpowiedzi
    serwera. Listę bierzemy wprost od httpx, żeby nie rozjechała się z jego
    możliwościami po zmianie zależności.
    """
    try:
        from httpx._decoders import SUPPORTED_DECODERS
    except ImportError:
        return "gzip, deflate"
    kodowania = [nazwa for nazwa in SUPPORTED_DECODERS if nazwa != "identity"]
    return ", ".join(kodowania) or "identity"


class ScraperError(Exception):
    """Podnoszony gdy nie udało się pobrać lub sparsować strony."""


class SEOScraper:
    """Pobiera stronę HTTP i wyciąga z niej dane istotne dla audytu SEO/GEO."""

    def __init__(
        self,
        timeout: httpx.Timeout | float | None = None,
        user_agent: str | None = None,
        max_attempts: int = FETCH_MAX_ATTEMPTS,
    ):
        # Rozdzielony limit czasu: nawiązanie połączenia musi być szybkie (martwy host
        # nie ma po co blokować audytu), ale samo wygenerowanie strony bywa wolne -
        # pojedynczy wspólny timeout zmuszał do wyboru między jednym a drugim.
        self._timeout_override = (
            httpx.Timeout(timeout) if isinstance(timeout, (int, float)) else timeout
        )
        self.timeout = self._timeout_override or httpx.Timeout(
            READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
        )
        self.max_attempts = max(1, max_attempts)
        self.user_agent_override = user_agent
        self.headers = self._build_headers(user_agent or DEFAULT_USER_AGENT)
        # Stan ostatniej odpowiedzi ustawiany przez fetch(). Trzymany PER WĄTEK, bo
        # audyt skanuje szablony podstron równolegle jedną instancją scrapera - wspólne
        # pole instancji mieszałoby liczbę przekierowań i nagłówki między podstronami.
        self._state = threading.local()

    @property
    def _last_redirect_count(self) -> int:
        return getattr(self._state, "redirect_count", 0)

    @_last_redirect_count.setter
    def _last_redirect_count(self, value: int) -> None:
        self._state.redirect_count = value

    @property
    def _last_render_used(self) -> bool:
        return getattr(self._state, "render_used", False)

    @_last_render_used.setter
    def _last_render_used(self, value: bool) -> None:
        self._state.render_used = value

    @property
    def _last_response_headers(self) -> dict:
        return getattr(self._state, "response_headers", {})

    @_last_response_headers.setter
    def _last_response_headers(self, value: dict) -> None:
        self._state.response_headers = value

    def _build_headers(self, user_agent: str) -> dict:
        """Komplet nagłówków zwykłego klienta HTTP.

        Sam User-Agent to za mało: serwery i systemy antybotowe oceniają spójność
        całego zestawu, a żądanie bez `Accept` czy `Accept-Language` wygląda na
        automat nawet wtedy, gdy przedstawia się jako przeglądarka.
        """
        headers = {
            "User-Agent": user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": _supported_accept_encoding(),
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        # Client Hints mają sens WYŁĄCZNIE przy UA przeglądarki. Nagłówek
        # `sec-ch-ua: "Google Chrome"` obok `User-Agent: SEOAuditorBot` to sprzeczność,
        # którą systemy antybotowe wykrywają łatwiej niż sam nietypowy User-Agent.
        if _is_browser_user_agent(user_agent):
            headers.update(BROWSER_CLIENT_HINTS)
        return headers

    def _headers_for_attempt(self, attempt: int) -> dict:
        """Nagłówki dla danej próby - kolejne podejścia rotują User-Agenta.

        Jawnie podany `user_agent` wyłącza rotację: skoro ktoś go narzucił, zmiana
        byłaby zaskoczeniem.
        """
        if self.user_agent_override:
            return self.headers
        agents = (DEFAULT_USER_AGENT, FALLBACK_USER_AGENT)
        return self._build_headers(agents[attempt % len(agents)])

    def _timeout_for_attempt(self, attempt: int) -> httpx.Timeout:
        """Limit czasu dla danej próby - odczyt wydłuża się z każdym podejściem.

        Gdy `timeout` podano jawnie w konstruktorze, zostaje bez zmian: narzucona
        wartość ma być respektowana (korzystają z tego testy i wywołania specjalne).
        """
        if self._timeout_override is not None:
            return self._timeout_override

        mnoznik = READ_TIMEOUT_MULTIPLIERS[min(attempt, len(READ_TIMEOUT_MULTIPLIERS) - 1)]
        return httpx.Timeout(READ_TIMEOUT_SECONDS * mnoznik, connect=CONNECT_TIMEOUT_SECONDS)

    def scrape(self, url: str) -> dict:
        """Pobiera i analizuje stronę, w razie potrzeby dorenderowując ją przeglądarką.

        Statyczny HTML aplikacji CSR (React/Vue/Angular) nie zawiera ani `<title>`,
        ani treści - powstają dopiero po wykonaniu JavaScriptu. Gdy parser nie znajdzie
        podstawowych tagów SEO, druga próba idzie przez bezgłowną przeglądarkę, żeby
        audyt oceniał to, co widzi użytkownik i robot Google (który JS wykonuje).
        """
        normalized_url = self._normalize_url(url)
        html = self.fetch(normalized_url)
        data = self.parse(html, normalized_url)

        # Zanim sięgniemy po przeglądarkę: może treść jest dostępna od ręki, tylko
        # pod inną tożsamością. To tańsze i wierniejsze niż renderowanie, bo daje
        # DOKŁADNIE ten HTML, który indeksuje wyszukiwarka.
        if self._looks_like_stub(data):
            richer = self._retry_as_googlebot(normalized_url, data)
            if richer is not None:
                return richer

        if self._last_render_used or not self._needs_browser_rendering(data):
            return data

        rendered_html = self._render(normalized_url)
        if rendered_html is None:
            # Renderowanie niedostępne albo nieudane - zostawiamy wynik statyczny.
            # Audyt na niepełnych danych jest gorszy niż audyt pełny, ale wciąż
            # znacznie lepszy niż brak audytu.
            return data

        self._last_render_used = True
        rendered = self.parse(rendered_html, normalized_url)
        logger.info(
            "Strona %s wymagała renderowania JS (statycznie: title=%s, description=%s).",
            normalized_url, bool(data.get("title")), bool(data.get("meta_description")),
        )
        return rendered

    def _looks_like_stub(self, data: dict) -> bool:
        """Czy odpowiedź to powłoka aplikacji, a nie strona z treścią."""
        return data.get("word_count", 0) < STUB_WORD_THRESHOLD

    def _retry_as_googlebot(self, url: str, data: dict) -> dict | None:
        """Ponawia pobranie tożsamością Googlebota. None, gdy nic to nie zmieniło.

        Wynik przyjmujemy TYLKO wtedy, gdy realnie przybyło treści - inaczej
        zostawiamy pierwotny wynik i ewentualnie idziemy do renderowania.
        """
        if self.user_agent_override:
            # Ktoś narzucił tożsamość jawnie - podmiana byłaby zaskoczeniem.
            return None

        try:
            html = self.fetch_raw(url, user_agent=GOOGLEBOT_RETRY_USER_AGENT)
        except (ScraperError, httpx.HTTPError) as exc:
            logger.info("Ponowienie jako Googlebot nie powiodło się dla %s: %s", url, exc)
            return None

        richer = self.parse(html, url)
        if richer.get("word_count", 0) <= data.get("word_count", 0):
            return None

        logger.info(
            "Serwis %s oddaje treść wyłącznie zadeklarowanym botom (%s -> %s słów) - "
            "audyt korzysta z wersji serwowanej Googlebotowi.",
            url, data.get("word_count", 0), richer.get("word_count", 0),
        )
        richer["served_to_declared_bots_only"] = True
        return richer

    def _needs_browser_rendering(self, data: dict) -> bool:
        """Czy statyczny HTML wygląda na niekompletny na tyle, by sięgnąć po przeglądarkę.

        Renderowanie jest drogie (sekundy i setki MB pamięci na stronę), więc nie
        uruchamiamy go "na wszelki wypadek". Sięgamy po nie tylko wtedy, gdy brakuje
        tagu, który ma KAŻDA poprawnie zbudowana strona - albo gdy heurystyka CSR
        wskazuje pusty dokument czekający na JavaScript.
        """
        if not renderer.is_available():
            return False
        if not data.get("title") or not data.get("meta_description"):
            return True
        return bool(data.get("js_rendering", {}).get("likely_csr"))

    def _render(self, url: str) -> str | None:
        """Renderuje stronę przeglądarką. Zwraca None, gdy się nie udało.

        Błąd renderowania nigdy nie przerywa audytu: fallback ma poprawiać wynik,
        a nie stwarzać nowy powód niepowodzenia.
        """
        try:
            return renderer.render_html(
                url,
                user_agent=self.user_agent_override or DEFAULT_USER_AGENT,
                extra_headers=self.headers,
            )
        except renderer.RendererUnavailableError as exc:
            logger.info("Fallback przeglądarkowy niedostępny dla %s: %s", url, exc)
        except renderer.RendererError as exc:
            logger.warning("Renderowanie %s nie powiodło się: %s", url, exc)
        return None

    def fetch(self, url: str) -> str:
        """Pobiera stronę, ponawiając próbę przy błędach przejściowych.

        Ponawiamy WYŁĄCZNIE błędy sieciowe (timeout, zerwane połączenie) i odpowiedzi
        5xx - przy 404 czy 403 kolejna identyczna próba nic nie zmieni, a tylko wydłuży
        audyt. Każde podejście rotuje User-Agenta, bo w praktyce spotykamy serwery
        spowalniające ruch botów ORAZ takie, które blokują automaty udające przeglądarkę.
        """
        ostatni_blad: Exception | None = None
        self._last_render_used = False

        for attempt in range(self.max_attempts):
            if attempt:
                time.sleep(FETCH_RETRY_BACKOFF_SECONDS * attempt)

            try:
                return self._fetch_once(
                    url, self._headers_for_attempt(attempt), self._timeout_for_attempt(attempt)
                )
            except ScraperError:
                # Adres odrzucony przez walidację (SSRF) - ponawianie nic nie da.
                raise
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in RENDER_FALLBACK_STATUS_CODES:
                    # Odmowa WAF-a albo limit żądań: powtórzenie tego samego zapytania
                    # statycznego nic nie zmieni, ale prawdziwa przeglądarka potrafi
                    # przejść challenge, którego httpx nie wykona.
                    rendered = self._render(url)
                    if rendered is not None:
                        self._last_render_used = True
                        logger.info(
                            "Strona %s odmówiła statycznie (HTTP %s) - treść pobrana przeglądarką.",
                            url, exc.response.status_code,
                        )
                        return rendered
                    raise ScraperError(f"Nie udało się pobrać {url}: {exc}") from exc
                if exc.response.status_code < 500:
                    raise ScraperError(f"Nie udało się pobrać {url}: {exc}") from exc
                ostatni_blad = exc
                logger.info(
                    "Pobranie %s nie powiodło się (HTTP %s), próba %s/%s.",
                    url, exc.response.status_code, attempt + 1, self.max_attempts,
                )
            except httpx.HTTPError as exc:
                ostatni_blad = exc
                logger.info(
                    "Pobranie %s nie powiodło się (%s), próba %s/%s.",
                    url, type(exc).__name__, attempt + 1, self.max_attempts,
                )

        raise ScraperError(
            f"Nie udało się pobrać {url} po {self.max_attempts} próbach: {ostatni_blad}"
        ) from ostatni_blad

    def fetch_raw(self, url: str, user_agent: str | None = None) -> str:
        """Jedno żądanie, bez ponowień, rotacji UA i fallbacku przeglądarkowego.

        Służy weryfikacji pre-flight (`auditor.services.accessibility`), która musi
        zobaczyć DOKŁADNIE to, co dostaje wskazany robot. `fetch()` byłby tu mylący:
        rotuje User-Agenta i przy 403 sięga po przeglądarkę, więc zamaskowałby
        właśnie tę blokadę, którą pre-flight ma wykryć.
        """
        normalized_url = self._normalize_url(url)
        headers = self._build_headers(user_agent) if user_agent else self.headers
        return self._fetch_once(normalized_url, headers, self._timeout_for_attempt(0))

    def _fetch_once(self, url: str, headers: dict, timeout: httpx.Timeout | None = None) -> str:
        """Pojedyncze podejście do pobrania strony wraz z obsługą przekierowań.

        Przekierowania obsługujemy ręcznie (follow_redirects=False), bo publiczny adres
        może przekierować w głąb sieci lokalnej - każdy skok musi przejść tę samą
        walidację co adres podany przez użytkownika (ochrona przed SSRF).
        """
        try:
            safe_url = validate_public_url(url)
        except UnsafeUrlError as exc:
            raise ScraperError(f"Nie udało się pobrać {url}: {exc}") from exc

        hops = 0
        with httpx.Client(
            headers=headers, timeout=timeout or self.timeout, follow_redirects=False
        ) as client:
            response = client.get(safe_url)
            while response.is_redirect and hops < MAX_REDIRECT_HOPS:
                next_request = response.next_request
                if next_request is None:
                    break
                try:
                    safe_url = validate_public_url(str(next_request.url))
                except UnsafeUrlError as exc:
                    raise ScraperError(
                        f"Przekierowanie z {url} prowadzi do niedozwolonego adresu: {exc}"
                    ) from exc
                response = client.get(safe_url)
                hops += 1
            response.raise_for_status()

        # Liczba przekierowań napotkanych po drodze - parse() zgłasza na jej podstawie
        # test "Przekierowania 301/302" (zero dodatkowych zapytań).
        self._last_redirect_count = hops
        # X-Robots-Tag istnieje WYŁĄCZNIE w nagłówkach HTTP - bez ich zapamiętania
        # dyrektywa noindex podana po stronie serwera byłaby dla audytu niewidoczna.
        self._last_response_headers = dict(response.headers)
        return response.text

    def _normalize_url(self, url: str) -> str:
        """httpx (i większość serwerów) odrzuca adresy bez schematu lub z otaczającymi
        białymi znakami - np. "example.com" albo " https://example.com " - dlatego
        normalizujemy je przed wysłaniem żądania, zamiast przerywać audyt ScraperError."""
        normalized = (url or "").strip()
        if normalized and not _SCHEME_RE.match(normalized):
            normalized = f"https://{normalized}"
        return normalized

    def parse(self, html: str, url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")

        title, title_source = self._extract_title(soup)
        meta_description, description_source = self._extract_description(soup)

        headings = {
            f"h{level}": [h.get_text(strip=True) for h in soup.find_all(f"h{level}")]
            for level in range(1, 7)
        }
        # Nagłówek bez treści (<h1></h1>, <h1>   </h1>) istnieje w drzewie DOM, ale nie
        # niesie żadnej informacji dla wyszukiwarki. Liczymy je osobno, żeby testy
        # nagłówków (h1_structure i heading_order) opierały się na TEJ SAMEJ definicji
        # "nagłówka, który się liczy" - wcześniej pusty H1 dawał OK w jednym teście
        # i OSTRZEŻENIE w drugim.
        h1_non_empty = [text for text in headings["h1"] if text]

        canonical_tag = soup.find("link", rel="canonical")
        # .strip() jest istotne: <link rel="canonical" href="   "> to canonical PUSTY,
        # a bez przycięcia białych znaków wartość byłaby prawdziwa logicznie i test
        # zwracałby OK dla znacznika, który niczego nie wskazuje.
        canonical = (canonical_tag.get("href") or "").strip() or None if canonical_tag else None

        open_graph = {
            tag["property"][3:]: tag.get("content", "")
            for tag in soup.find_all("meta", property=True)
            if tag["property"].startswith("og:") and tag.get("content")
        }

        images = self._analyze_images(soup.find_all("img"), url)
        schema = self._extract_schema(soup)
        page_type = self._detect_page_type(url, soup)
        page_intent = self._detect_page_intent(url, soup, schema, page_type)
        faq_detected = self._detect_faq_section(soup)
        heading_noise = self._analyze_heading_noise(soup)
        heading_quality = self._analyze_heading_quality(soup)
        eeat = self._analyze_eeat_signals(soup)
        meta_keywords_present = bool(self._get_meta_content(soup, "keywords"))
        internal_links_count = self._count_internal_links(soup, url)
        js_rendering = self._analyze_js_rendering(soup)
        twitter_card = self._extract_twitter_card(soup)
        favicon = self._extract_favicon(soup, url)
        meta_robots = self._extract_meta_robots(soup)
        x_robots_tag = self._analyze_x_robots_tag()
        answer_first = self._analyze_answer_first(soup)
        visible_prices = self._extract_visible_prices(soup)
        hidden_content = self._analyze_hidden_content(soup)
        heading_visibility = self._analyze_heading_visibility(soup)
        outbound_links = self._analyze_outbound_links(soup, url)
        placeholder_hits = self._find_placeholder_text(soup)
        structured_content = self._analyze_structured_content(soup)

        return {
            "url": url,
            "title": title,
            "title_length": len(title) if title else 0,
            # Skąd pochodzi wartość: "title" / "og:title" / "twitter:title" (i analogicznie
            # dla opisu) albo None. Bez tej informacji audyt zgłaszałby OK dla strony,
            # która nie ma tagu <title> - patrz AuditService._evaluate_title.
            "title_source": title_source,
            "meta_description": meta_description,
            "meta_description_length": len(meta_description) if meta_description else 0,
            "meta_description_source": description_source,
            # Czy treść pochodzi z bezgłownej przeglądarki (fallback dla CSR/WAF).
            "rendered_with_browser": self._last_render_used,
            "meta_keywords_present": meta_keywords_present,
            "headings": headings,
            "h1_count": len(headings["h1"]),
            "h1_non_empty": h1_non_empty,
            "h1_non_empty_count": len(h1_non_empty),
            "h1_empty_count": len(headings["h1"]) - len(h1_non_empty),
            "canonical": canonical,
            "open_graph": open_graph,
            "images_total": images["total"],
            "images_with_alt": images["with_alt"],
            "images_without_alt": images["without_alt"],
            "images_without_alt_examples": images["without_alt_examples"],
            "images_with_title": images["with_title"],
            "images_without_title": images["without_title"],
            "images_non_ascii_src_count": images["non_ascii_src_count"],
            "images_non_ascii_src_examples": images["non_ascii_src_examples"],
            "images_checkable_srcs": images["checkable_srcs"],
            "schema": schema,
            "page_type": page_type,
            # "editorial" albo "commercial" - decyduje, czy od strony wymagamy
            # imiennego autora (Person), czy reprezentacji firmy (Organization).
            "page_intent": page_intent,
            "faq_detected": faq_detected,
            "heading_noise": heading_noise,
            "heading_quality": heading_quality,
            "eeat": eeat,
            "internal_links_count": internal_links_count,
            "js_rendering": js_rendering,
            # Liczba słów widocznej treści wystawiona na wierzch - poza detekcją CSR
            # korzysta z niej także test "thin content" (patrz AuditService).
            "word_count": js_rendering.get("word_count", 0),
            "twitter_card": twitter_card,
            "favicon": favicon,
            "meta_robots": meta_robots,
            "x_robots_tag": x_robots_tag,
            "answer_first": answer_first,
            "structured_content": structured_content,
            "visible_prices": visible_prices,
            "hidden_content": hidden_content,
            "heading_visibility": heading_visibility,
            "outbound_links": outbound_links,
            "placeholder_hits": placeholder_hits,
            "redirect_count": self._last_redirect_count,
        }

    def _is_hidden_element(self, element) -> bool:
        """Czy element (lub któryś z jego przodków) jest ukryty stylem inline albo klasą CSS.

        Sprawdzamy wyłącznie sygnały widoczne w statycznym HTML - atrybut `hidden`,
        `style="display:none"`, `aria-hidden` oraz popularne klasy frameworków. Reguł
        z zewnętrznych arkuszy CSS nie da się ocenić bez renderowania strony, więc
        analiza jest zachowawcza: wykryje typowe przypadki, ale nie wszystkie.
        """
        for node in [element, *element.parents]:
            if getattr(node, "name", None) in (None, "[document]"):
                continue
            if node.has_attr("hidden"):
                return True
            if _HIDDEN_STYLE_RE.search(node.get("style", "") or ""):
                return True
            classes = " ".join(node.get("class") or []).lower()
            if classes and _HIDDEN_CLASS_RE.search(classes):
                return True
            if (node.get("aria-hidden") or "").lower() == "true":
                return True
        return False

    def _parse_prices(self, text: str) -> list[float]:
        """Zamienia liczby z tekstu na wartości, obsługując zapis PL (1 234,56) i EN (1,234.56)."""
        wartosci: list[float] = []
        for surowa in _PRICE_NUMBER_RE.findall(text):
            znormalizowana = surowa.replace("\u00a0", "").replace(" ", "")
            if "," in znormalizowana and "." in znormalizowana:
                # Ostatni separator decyduje o roli przecinka i kropki.
                if znormalizowana.rfind(",") > znormalizowana.rfind("."):
                    znormalizowana = znormalizowana.replace(".", "").replace(",", ".")
                else:
                    znormalizowana = znormalizowana.replace(",", "")
            elif "," in znormalizowana:
                znormalizowana = znormalizowana.replace(",", ".")
            try:
                wartosc = float(znormalizowana)
            except ValueError:
                continue
            if 0 < wartosc < 10_000_000:
                wartosci.append(wartosc)
        return wartosci

    def _extract_visible_prices(self, soup: BeautifulSoup) -> dict:
        """Wyciąga ceny obecne w drzewie DOM, rozróżniając widoczne od ukrytych.

        Służy do porównania z ceną zadeklarowaną w danych strukturalnych: rozbieżność
        oznacza, że wyszukiwarka i modele AI pokazują inną cenę niż ta, którą użytkownik
        realnie zobaczy na stronie.
        """
        kandydaci: list[dict] = []

        for element in soup.find_all(_PRICE_CARRIER_TAGS):
            if element.find(_PRICE_CARRIER_TAGS):
                continue  # bierzemy tylko najgłębsze węzły, żeby nie liczyć ceny wielokrotnie
            text = element.get_text(" ", strip=True)
            if not text or len(text) > 60 or not _PRICE_HINT_RE.search(text):
                continue
            ukryty = self._is_hidden_element(element)
            for wartosc in self._parse_prices(text):
                kandydaci.append({"value": wartosc, "text": text[:60], "hidden": ukryty})

        widoczne = [k["value"] for k in kandydaci if not k["hidden"]]
        return {
            "found": bool(kandydaci),
            "values": sorted({round(k["value"], 2) for k in kandydaci}),
            "visible_values": sorted(set(widoczne)),
            "min_visible": min(widoczne) if widoczne else None,
            "max_visible": max(widoczne) if widoczne else None,
            "samples": kandydaci[:10],
        }

    def _analyze_hidden_content(self, soup: BeautifulSoup) -> dict:
        """Wykrywa duże bloki treści ukryte przed crawlerem stylem CSS.

        Crawlery RAG czytają tekst z DOM bez renderowania stylów, ale wyszukiwarki
        traktują treść ukrytą jako mniej istotną lub pomijają ją zupełnie. Opinie, FAQ
        i specyfikacje schowane pod "pokaż więcej" tracą wtedy wartość dla widoczności
        w odpowiedziach generatywnych.
        """
        bloki: list[dict] = []
        ukryte_slowa = 0

        for element in soup.find_all(_HIDEABLE_BLOCK_TAGS):
            if not self._is_hidden_element(element):
                continue
            # Element zagnieżdżony w już zliczonym bloku liczyłby te same słowa drugi raz.
            if any(element in blok["element"].descendants for blok in bloki):
                continue
            tekst = element.get_text(" ", strip=True)
            liczba_slow = len(tekst.split())
            if liczba_slow < HIDDEN_BLOCK_MIN_WORDS:
                continue
            bloki.append({
                "element": element,
                "tag": element.name,
                "words": liczba_slow,
                "preview": tekst[:120],
                "class": " ".join(element.get("class") or [])[:80],
            })
            ukryte_slowa += liczba_slow

        wszystkie_slowa = len(soup.get_text(" ", strip=True).split())
        widoczne_slowa = max(wszystkie_slowa - ukryte_slowa, 0)
        laczne = ukryte_slowa + widoczne_slowa
        return {
            "blocks": [{k: v for k, v in b.items() if k != "element"} for b in bloki[:10]],
            "blocks_count": len(bloki),
            "hidden_words": ukryte_slowa,
            "visible_words": widoczne_slowa,
            "hidden_share": round(ukryte_slowa / laczne, 2) if laczne else 0.0,
        }

    def _analyze_heading_visibility(self, soup: BeautifulSoup) -> dict:
        """Nagłówki obecne w HTML, ale niewidoczne dla użytkownika.

        Taki nagłówek zaburza strukturę dokumentu dla robotów, choć w interfejsie nie
        istnieje - typowy przypadek to komunikat "Nie znaleziono produktów" ukryty pod
        prawidłowym H1, tworzący dla crawlera sztuczny poziom hierarchii.
        """
        ukryte = [
            {"tag": heading.name.upper(), "text": heading.get_text(strip=True)[:100]}
            for heading in soup.find_all(HEADING_TAGS)
            if heading.get_text(strip=True) and self._is_hidden_element(heading)
        ]
        return {"hidden_headings": ukryte, "hidden_count": len(ukryte)}

    def _analyze_outbound_links(self, soup: BeautifulSoup, page_url: str) -> dict:
        """Linki wychodzące poza domenę - dowód opierania treści na źródłach zewnętrznych."""
        host = urlparse(page_url).netloc.lower().replace("www.", "")
        wychodzace: list[dict] = []
        zaufane: list[dict] = []

        for link in soup.find_all("a", href=True):
            href = link["href"].strip()
            if not href.startswith(("http://", "https://")):
                continue
            target_host = urlparse(href).netloc.lower().replace("www.", "")
            if not target_host or target_host == host or target_host.endswith("." + host):
                continue

            rel = " ".join(link.get("rel") or []).lower()
            wpis = {
                "url": href[:200],
                "host": target_host,
                "nofollow": "nofollow" in rel,
                "anchor": link.get_text(strip=True)[:80],
            }
            wychodzace.append(wpis)
            if _TRUSTED_DOMAIN_RE.search(target_host):
                zaufane.append(wpis)

        return {
            "total": len(wychodzace),
            "trusted": len(zaufane),
            "followed_trusted": len([w for w in zaufane if not w["nofollow"]]),
            "trusted_hosts": sorted({w["host"] for w in zaufane})[:10],
            "samples": wychodzace[:10],
        }

    def _find_placeholder_text(self, soup: BeautifulSoup) -> list[dict]:
        """Znajduje teksty zastępcze w WIDOCZNEJ treści strony.

        Analiza musi objąć całą treść, a nie tylko nagłówki i meta tagi - "lorem ipsum"
        najczęściej zostaje właśnie w akapitach opisu, gdzie nikt go nie szuka.
        Skrypty i style pomijamy, bo biblioteki frontendowe bywają nimi naszpikowane.
        """
        for niechciany in soup(["script", "style", "noscript"]):
            niechciany.extract()

        tekst = soup.get_text(" ", strip=True)
        lowered = tekst.lower()
        trafienia: list[dict] = []
        for marker in PLACEHOLDER_TEXT_MARKERS:
            pozycja = lowered.find(marker)
            if pozycja >= 0:
                trafienia.append({
                    "marker": marker,
                    "context": tekst[max(pozycja - 30, 0):pozycja + 70].strip(),
                })
        return trafienia

    def _analyze_x_robots_tag(self) -> dict:
        """Dyrektywy indeksacji podane w nagłówku HTTP `X-Robots-Tag`.

        Nagłówek jest równoważny znacznikowi `<meta name="robots">`, ale bywa
        przeoczony, bo nie widać go w źródle strony - ustawia go serwer lub CDN.
        Obsługiwana jest składnia z nazwą bota ("X-Robots-Tag: googlebot: noindex"),
        dzięki czemu wykrywamy też blokady wymierzone w konkretne crawlery AI.

        Zwraca dyrektywy globalne (`directives`) oraz mapę bot -> dyrektywy
        (`per_bot`), z nazwami botów w oryginalnej pisowni.
        """
        raw_value = ""
        for name, value in (self._last_response_headers or {}).items():
            if name.lower() == "x-robots-tag":
                raw_value = value
                break

        directives: set[str] = set()
        per_bot: dict[str, set[str]] = {}

        for rule in raw_value.split(","):
            rule = rule.strip()
            if not rule:
                continue
            # "googlebot: noindex" -> reguła dla konkretnego bota; "noindex" -> globalna.
            if ":" in rule:
                bot, _, value = rule.partition(":")
                bot, value = bot.strip(), value.strip().lower()
                if value:
                    per_bot.setdefault(bot, set()).add(value)
                    continue
            directives.add(rule.lower())

        def blocks(values: set[str]) -> bool:
            return "noindex" in values or "none" in values

        return {
            "present": bool(raw_value),
            "raw": raw_value,
            "directives": sorted(directives),
            "per_bot": {bot: sorted(values) for bot, values in per_bot.items()},
            "noindex": blocks(directives),
            "nofollow": "nofollow" in directives or "none" in directives,
            "blocked_bots": sorted(bot for bot, values in per_bot.items() if blocks(values)),
        }

    def _analyze_answer_first(self, soup: BeautifulSoup) -> dict:
        """Sprawdza wzorzec "Answer-First" pod nagłówkami sekcji H2/H3.

        Modele językowe cytują fragmenty, które odpowiadają na pytanie od razu -
        zwięzły akapit ({ANSWER_FIRST_MIN_WORDS}-{ANSWER_FIRST_MAX_WORDS} słów)
        bezpośrednio pod nagłówkiem, przed rozbudowanym wyjaśnieniem. Sekcja, która
        zaczyna się od długiego wstępu, rzadko trafia do odpowiedzi AI w całości.

        Za "pierwszy akapit sekcji" uznajemy pierwszy element <p> z treścią następujący
        po nagłówku - listy i tabele pomijamy, bo opisuje je osobny test gęstości
        elementów ustrukturyzowanych.
        """
        sections: list[dict] = []

        for heading in soup.find_all(["h2", "h3"]):
            heading_text = heading.get_text(strip=True)
            if not heading_text:
                continue

            first_paragraph = ""
            for element in heading.find_all_next():
                # Kolejny nagłówek kończy sekcję - nie znaleziono akapitu wprowadzającego.
                if element.name in HEADING_TAGS:
                    break
                if element.name == "p":
                    text = element.get_text(" ", strip=True)
                    if text:
                        first_paragraph = text
                        break

            word_count = len(first_paragraph.split())
            sections.append({
                "heading": heading_text[:120],
                "word_count": word_count,
                "answer_first": ANSWER_FIRST_MIN_WORDS <= word_count <= ANSWER_FIRST_MAX_WORDS,
                "has_paragraph": bool(first_paragraph),
            })

        compliant = [s for s in sections if s["answer_first"]]
        return {
            "sections_total": len(sections),
            "sections_compliant": len(compliant),
            "ratio": round(len(compliant) / len(sections), 2) if sections else 0.0,
            "sections": sections[:20],
        }

    def _analyze_structured_content(self, soup: BeautifulSoup) -> dict:
        """Zlicza natywne elementy ustrukturyzowane: tabele i listy.

        Modele językowe wyciągają dane z tabel i list znacznie pewniej niż z prozy -
        `<table>`, `<ul>` i `<ol>` niosą jawną strukturę, której nie trzeba wnioskować
        z tekstu. Liczymy wyłącznie elementy Z TREŚCIĄ i pomijamy listy nawigacyjne
        (wewnątrz <nav>, <header>, <footer>), bo menu nie jest treścią merytoryczną.
        """
        def is_content_element(element) -> bool:
            if not element.get_text(strip=True):
                return False
            return not element.find_parent(["nav", "header", "footer"])

        tables = [t for t in soup.find_all("table") if is_content_element(t)]
        lists = [lst for lst in soup.find_all(["ul", "ol"]) if is_content_element(lst)]
        # Zagnieżdżone listy liczymy raz - podlista jest częścią tej samej struktury.
        top_level_lists = [lst for lst in lists if not lst.find_parent(["ul", "ol"])]
        # <dl> to w e-commerce typowy nośnik specyfikacji produktu (para cecha-wartość),
        # czyli dokładnie ten format, z którego LLM najłatwiej wyciąga dane.
        definition_lists = [dl for dl in soup.find_all("dl") if is_content_element(dl)]

        paragraphs = [p for p in soup.find_all("p") if p.get_text(strip=True)]
        structured_count = len(tables) + len(top_level_lists) + len(definition_lists)
        total_blocks = structured_count + len(paragraphs)

        return {
            "tables": len(tables),
            "lists": len(top_level_lists),
            "definition_lists": len(definition_lists),
            "definition_pairs": sum(len(dl.find_all("dt", recursive=False)) for dl in definition_lists),
            "list_items": sum(len(lst.find_all("li", recursive=False)) for lst in top_level_lists),
            "paragraphs": len(paragraphs),
            "structured_blocks": structured_count,
            "share": round(structured_count / total_blocks, 2) if total_blocks else 0.0,
        }

    def _extract_meta_robots(self, soup: BeautifulSoup) -> dict:
        """Odczytuje dyrektywy `<meta name="robots">` sterujące indeksacją strony.

        Uwzględnia warianty dla konkretnych botów (`googlebot`), bo `noindex` podany
        wyłącznie dla Googlebota wyklucza stronę z Google tak samo skutecznie jak
        dyrektywa ogólna. Wartości zbieramy ze WSZYSTKICH znaczników - strony bywają
        sklejane z kilku szablonów i dyrektywy potrafią się powtarzać.
        """
        directives: set[str] = set()
        raw_values: list[str] = []

        for tag in soup.find_all("meta"):
            name = (tag.get("name") or "").strip().lower()
            if name not in ("robots", "googlebot"):
                continue
            content = (tag.get("content") or "").strip()
            if not content:
                continue
            raw_values.append(f"{name}: {content}")
            directives |= {part.strip().lower() for part in content.split(",") if part.strip()}

        return {
            "present": bool(raw_values),
            "directives": sorted(directives),
            "raw": raw_values,
            "noindex": "noindex" in directives or "none" in directives,
            "nofollow": "nofollow" in directives or "none" in directives,
        }

    def _extract_twitter_card(self, soup: BeautifulSoup) -> dict:
        """Zbiera tagi Twitter Card (X) z `<meta name="twitter:...">`.

        Twitter/X czyta `name`, a nie `property` (w odróżnieniu od Open Graph), ale
        część CMS-ów wystawia je przez `property` - obsługujemy oba warianty, bo dla
        wyniku audytu liczy się obecność tagu, a nie użyty atrybut. Brakujące
        `twitter:title`/`twitter:description`/`twitter:image` nie są błędem, jeśli
        strona ma odpowiedniki Open Graph - X używa ich wtedy jako fallbacku (ocena
        tej zależności leży po stronie `AuditService._evaluate_twitter_cards`).
        """
        tags: dict[str, str] = {}
        for tag in soup.find_all("meta"):
            key = tag.get("name") or tag.get("property") or ""
            if key.lower().startswith("twitter:") and tag.get("content", "").strip():
                tags[key.lower()[len("twitter:"):]] = tag["content"].strip()

        return {
            "tags": tags,
            "card_type": tags.get("card"),
            "has_card": "card" in tags,
        }

    def _extract_favicon(self, soup: BeautifulSoup, base_url: str) -> dict:
        """Wykrywa ikonę witryny deklarowaną w `<head>`.

        Sprawdzane są wszystkie używane w praktyce warianty `rel`: "icon",
        "shortcut icon", "apple-touch-icon" oraz "mask-icon". Brak deklaracji w HTML
        nie przesądza jeszcze o braku ikony (przeglądarki pobierają domyślnie
        `/favicon.ico`), dlatego zwracamy też adres tego fallbacku - to `AuditService`
        decyduje, jaki status z tego wynika.
        """
        icon_rels = {"icon", "shortcut icon", "apple-touch-icon", "mask-icon"}
        declared: list[dict] = []
        for link in soup.find_all("link", href=True):
            rel_value = " ".join(link.get("rel") or []).lower()
            if rel_value in icon_rels or "icon" in rel_value.split():
                declared.append({
                    "rel": rel_value,
                    "href": urljoin(base_url, link["href"]),
                    "sizes": link.get("sizes", ""),
                })

        return {
            "declared": declared,
            "count": len(declared),
            "has_apple_touch_icon": any("apple-touch-icon" in item["rel"] for item in declared),
            "default_ico_url": self._build_absolute_url(base_url, "/favicon.ico"),
        }

    def _analyze_heading_quality(self, soup: BeautifulSoup) -> dict:
        """Ocenia poprawność hierarchii nagłówków H1-H6 w kolejności występowania.

        Wykrywa dwa problemy, których nie łapie `_analyze_heading_noise` (ten zajmuje
        się wyłącznie treścią nagłówków) ani test H1 (ten liczy same H1):
          * nagłówki puste (bez tekstu) - używane wyłącznie do celów wizualnych,
            rozmywają strukturę dokumentu dla robotów i czytników ekranu,
          * przeskoki poziomów (np. H1 -> H3 z pominięciem H2), które łamią logiczne
            zagnieżdżenie sekcji.
        """
        empty_headings: list[str] = []
        level_skips: list[dict] = []
        previous_level = 0

        for tag in soup.find_all(HEADING_TAGS):
            level = int(tag.name[1])
            text = tag.get_text(strip=True)

            if not text:
                empty_headings.append(tag.name.upper())
            # Pierwszy nagłówek na stronie nie ma z czym tworzyć przeskoku, a zejście
            # w górę hierarchii (H3 -> H2) jest normalnym początkiem nowej sekcji.
            elif previous_level and level > previous_level + 1:
                level_skips.append({
                    "from": f"H{previous_level}",
                    "to": tag.name.upper(),
                    "text": text[:80],
                })

            if text:
                previous_level = level

        return {
            "empty_headings": empty_headings,
            "empty_count": len(empty_headings),
            "level_skips": level_skips,
            "skip_count": len(level_skips),
        }

    def _get_meta_content(self, soup: BeautifulSoup, name: str) -> str | None:
        """Treść `<meta>` o danej nazwie - niewrażliwie na wielkość liter i atrybut.

        HTML nie rozróżnia wielkości liter w nazwach atrybutów ani w wartości `name`,
        a CMS-y wystawiają `Description`, `DESCRIPTION` czy `og:description` przez
        `property` zamiast `name`. Dosłowne `soup.find("meta", attrs={"name": name})`
        pomijało te warianty i audyt zgłaszał brak opisu, który na stronie był.

        Celowo NIE czytamy `itemprop`: `<meta itemprop="description">` wewnątrz bloku
        mikrodanych opisuje produkt albo artykuł, a nie całą stronę.
        """
        szukana = name.lower()
        for tag in soup.find_all("meta"):
            klucz = tag.get("name") or tag.get("property") or ""
            if klucz.strip().lower() != szukana:
                continue
            content = (tag.get("content") or "").strip()
            if content:
                return content
        return None

    # Zamienniki tytułu i opisu w kolejności wiarygodności. Pierwszy element to źródło
    # właściwe, kolejne - awaryjne: strony budowane po stronie klienta oraz część CMS-ów
    # wypełniają wyłącznie Open Graph, a wyszukiwarki i modele LLM czytają wtedy właśnie
    # te tagi. Kolejność jest istotna: og:* ma pierwszeństwo przed twitter:*, bo jest
    # szerzej wspierane.
    TITLE_FALLBACK_KEYS = ("og:title", "twitter:title")
    DESCRIPTION_FALLBACK_KEYS = ("og:description", "twitter:description")

    def _extract_title(self, soup: BeautifulSoup) -> tuple[str | None, str | None]:
        """Zwraca (tytuł, źródło) - źródło to "title", "og:title" albo "twitter:title".

        Źródło wędruje dalej do audytu, bo tytuł wzięty z Open Graph NIE jest tym samym
        co `<title>`: Google wyświetla w wynikach ten drugi. Zamiennik pozwala pokazać
        użytkownikowi realną treść strony, ale nie może zamieniać błędu w wynik "OK".
        """
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        if title:
            return title, "title"

        for klucz in self.TITLE_FALLBACK_KEYS:
            wartość = self._get_meta_content(soup, klucz)
            if wartość:
                return wartość, klucz
        return None, None

    def _extract_description(self, soup: BeautifulSoup) -> tuple[str | None, str | None]:
        """Zwraca (opis, źródło) - źródło to "description", "og:description" albo
        "twitter:description". Logika źródła jak w `_extract_title`."""
        opis = self._get_meta_content(soup, "description")
        if opis:
            return opis, "description"

        for klucz in self.DESCRIPTION_FALLBACK_KEYS:
            wartość = self._get_meta_content(soup, klucz)
            if wartość:
                return wartość, klucz
        return None, None

    # ------------------------------------------------------------------
    # Analiza obrazków: ALT, title, ASCII w src
    # ------------------------------------------------------------------
    def _analyze_images(self, images: list, page_url: str = "") -> dict:
        validatable = [img for img in images if self._is_validatable_image(img)]

        with_alt = [img for img in validatable if img.get("alt", "").strip()]
        without_alt = [img for img in validatable if not img.get("alt", "").strip()]
        with_title = [img for img in validatable if img.get("title", "").strip()]
        without_title = [img for img in validatable if not img.get("title", "").strip()]
        non_ascii_src = [
            img["src"] for img in validatable if img.get("src") and not img["src"].isascii()
        ]
        without_alt_src = [img["src"] for img in without_alt if img.get("src")]
        # Bezwzględne adresy próbki obrazków do sprawdzenia wagi pliku (patrz
        # `SEOScraper.check_image_sizes`) - ograniczone do IMAGE_SIZE_CHECK_LIMIT,
        # żeby audyt nie wysyłał dziesiątek żądań HEAD na stronach z wieloma zdjęciami.
        checkable_srcs = [
            urljoin(page_url, img["src"])
            for img in validatable[:IMAGE_SIZE_CHECK_LIMIT]
            if img.get("src")
        ]

        return {
            "total": len(validatable),
            "total_all_images": len(images),
            "skipped_non_raster": len(images) - len(validatable),
            "with_alt": len(with_alt),
            "without_alt": len(without_alt),
            "without_alt_examples": without_alt_src[:10],
            "with_title": len(with_title),
            "without_title": len(without_title),
            "non_ascii_src_count": len(non_ascii_src),
            "non_ascii_src_examples": non_ascii_src[:5],
            "checkable_srcs": checkable_srcs,
        }

    def _is_validatable_image(self, img) -> bool:
        """Czy obrazek podlega walidacji ALT/title - tylko standardowe pliki rastrowe
        (JPG/PNG/WEBP/GIF), z pominięciem SVG oraz dekoracyjnych ikon/elementów UI."""
        src = (img.get("src") or "").strip().lower()
        if not src:
            return False

        path = src.split("?", 1)[0].split("#", 1)[0]
        if not path.endswith(RASTER_IMAGE_EXTENSIONS):
            return False

        if img.get("role") == "presentation" or img.get("aria-hidden") == "true":
            return False

        class_attr = img.get("class") or []
        if isinstance(class_attr, str):
            class_attr = class_attr.split()
        if _DECORATIVE_HINT_RE.search(" ".join(class_attr)):
            return False
        if _DECORATIVE_HINT_RE.search(img.get("id") or ""):
            return False

        return True

    # ------------------------------------------------------------------
    # Dane strukturalne Schema.org (JSON-LD + Microdata)
    # ------------------------------------------------------------------
    def _extract_schema(self, soup: BeautifulSoup) -> dict:
        scripts = soup.find_all("script", type="application/ld+json")
        json_ld_types: set[str] = set()
        parse_errors = 0
        # Surowe encje są potrzebne do walidacji grafu (@id, relacje, ceny, czystość
        # danych) - sam zbiór typów nie pozwala sprawdzić, jak encje się ze sobą łączą.
        entities: list[dict] = []

        for script in scripts:
            raw = script.string or script.get_text()
            if not raw or not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                parse_errors += 1
                continue
            json_ld_types |= self._collect_schema_types(payload)
            entities.extend(self._flatten_schema_entities(payload))

        microdata_types = self._extract_microdata_types(soup)
        types_found = json_ld_types | microdata_types

        return {
            "blocks_found": len(scripts),
            "types_found": sorted(types_found),
            "json_ld_types": sorted(json_ld_types),
            "microdata_types": sorted(microdata_types),
            "parse_errors": parse_errors,
            "entities": entities,
        }

    def _flatten_schema_entities(self, node, depth: int = 0) -> list[dict]:
        """Spłaszcza dokument JSON-LD do listy encji (słowników z kluczem @type).

        Rozwija `@graph` oraz encje zagnieżdżone w właściwościach, bo walidacja
        powiązań musi widzieć WSZYSTKIE encje dokumentu niezależnie od tego, czy autor
        użył płaskiego grafu, czy zagnieżdżenia. `depth` chroni przed zapętleniem na
        wyjątkowo głęboko zagnieżdżonych (lub złośliwych) dokumentach.
        """
        if depth > SCHEMA_MAX_DEPTH:
            return []

        entities: list[dict] = []
        if isinstance(node, list):
            for item in node:
                entities.extend(self._flatten_schema_entities(item, depth + 1))
            return entities

        if not isinstance(node, dict):
            return entities

        if "@graph" in node:
            entities.extend(self._flatten_schema_entities(node["@graph"], depth + 1))

        if node.get("@type"):
            entities.append(node)

        for key, value in node.items():
            if key in ("@graph", "@context", "@type"):
                continue
            if isinstance(value, (dict, list)):
                entities.extend(self._flatten_schema_entities(value, depth + 1))

        return entities

    def _collect_schema_types(self, node) -> set[str]:
        types: set[str] = set()
        if isinstance(node, list):
            for item in node:
                types |= self._collect_schema_types(item)
        elif isinstance(node, dict):
            type_value = node.get("@type")
            if isinstance(type_value, str):
                types.add(type_value)
            elif isinstance(type_value, list):
                types.update(t for t in type_value if isinstance(t, str))
            if isinstance(node.get("@graph"), list):
                types |= self._collect_schema_types(node["@graph"])
        return types

    def _extract_microdata_types(self, soup: BeautifulSoup) -> set[str]:
        """Wyciąga typy Schema.org zapisane jako Microdata (itemscope + itemtype),
        np. <div itemscope itemtype="https://schema.org/Product">."""
        types: set[str] = set()
        for tag in soup.find_all(attrs={"itemscope": True}):
            itemtype = tag.get("itemtype", "")
            if not itemtype:
                continue
            for token in itemtype.split():
                type_name = token.rstrip("/").rsplit("/", 1)[-1]
                if type_name:
                    types.add(type_name)
        return types

    # ------------------------------------------------------------------
    # Klasyfikacja typu podstrony (Strona główna / Produkt / Artykuł / Kategoria / Ogólna)
    # ------------------------------------------------------------------
    def _detect_page_type(self, url: str, soup: BeautifulSoup) -> str:
        path = (urlparse(url).path or "/").lower()

        if path in ("", "/"):
            return PAGE_TYPE_HOMEPAGE

        if any(segment in path for segment in PRODUCT_PATH_SEGMENTS) or self._has_product_signals(soup):
            return PAGE_TYPE_PRODUCT

        if any(segment in path for segment in ARTICLE_PATH_SEGMENTS) or self._has_article_signals(soup):
            return PAGE_TYPE_ARTICLE

        if any(segment in path for segment in CATEGORY_PATH_SEGMENTS) or self._has_category_signals(soup):
            return PAGE_TYPE_CATEGORY

        return PAGE_TYPE_GENERIC

    def _detect_page_intent(self, url: str, soup: BeautifulSoup, schema: dict, page_type: str) -> str:
        """Klasyfikuje podstronę jako redakcyjną albo ofertową.

        Kolejność rozstrzygania odzwierciedla wiarygodność sygnałów: deklaracja
        wydawcy w Schema.org jest mocniejsza niż ścieżka adresu, a ta - niż
        heurystyki na treści. Domyślnie strona jest KOMERCYJNA: w razie wątpliwości
        lepiej nie żądać imiennego autora, bo fałszywy alarm na stronie ofertowej
        kosztuje więcej niż pominięte ostrzeżenie na nietypowym artykule.
        """
        types = {str(t).lower() for t in (schema or {}).get("types") or []}

        if types & EDITORIAL_SCHEMA_TYPES:
            return PAGE_INTENT_EDITORIAL
        if types & COMMERCIAL_SCHEMA_TYPES:
            return PAGE_INTENT_COMMERCIAL

        path = (urlparse(url).path or "/").lower()
        if any(segment in path for segment in EDITORIAL_PATH_SEGMENTS):
            return PAGE_INTENT_EDITORIAL
        if any(segment in path for segment in COMMERCIAL_PATH_SEGMENTS):
            return PAGE_INTENT_COMMERCIAL

        # Bez rozstrzygnięcia ze Schema i adresu zostaje heurystyka treści - ta sama,
        # z której korzysta `page_type`.
        if page_type == PAGE_TYPE_ARTICLE:
            return PAGE_INTENT_EDITORIAL
        return PAGE_INTENT_COMMERCIAL

    def _has_product_signals(self, soup: BeautifulSoup) -> bool:
        # Przyciski "dodaj do koszyka" lub elementy z klasą ceny.
        return bool(soup.find(attrs={"class": _CART_OR_PRICE_CLASS_RE}))

    def _has_article_signals(self, soup: BeautifulSoup) -> bool:
        """Czy strona wygląda na tekst redakcyjny.

        Znacznik <article> sam w sobie NIE wystarcza. CMS-y komponentowe (AEM, Drupal)
        używają go jako kontenera kafelka, więc strona ofertowa potrafi mieć ich
        kilkanaście - tak było na orlen.pl/pl/dla-biznesu/karty-i-uslugi-flotowe
        (8 znaczników), co wpychało ją do typu "article" i włączało wymóg autora.
        Liczy się więc POJEDYNCZY <article> (strona zbudowana wokół jednego tekstu)
        albo metadane redakcyjne, które kafelek layoutu nigdy nie niesie.
        """
        author_meta = soup.find("meta", attrs={"name": "author"}) or soup.find(
            "meta", property="article:author"
        )
        date_meta = soup.find("meta", property="article:published_time") or soup.find(
            "meta", property="article:modified_time"
        )
        if author_meta or date_meta:
            return True
        return len(soup.find_all("article")) == 1

    def _has_category_signals(self, soup: BeautifulSoup) -> bool:
        # Kilka powtarzalnych elementów listy produktów sugeruje stronę kategorii/sklepu.
        return len(soup.find_all(attrs={"class": _PRODUCT_LIST_CLASS_RE})) >= 2

    # ------------------------------------------------------------------
    # Dynamiczna detekcja sekcji FAQ
    # ------------------------------------------------------------------
    def _detect_faq_section(self, soup: BeautifulSoup) -> bool:
        for tag in soup.find_all(HEADING_TAGS):
            text = tag.get_text(strip=True).lower()
            if any(keyword in text for keyword in FAQ_HEADING_KEYWORDS):
                return True

        # Wiele elementów <details> to typowy wzorzec akordeonu z pytaniami/odpowiedziami.
        if len(soup.find_all("details")) >= 2:
            return True

        if soup.find(attrs={"class": _FAQ_CONTAINER_CLASS_RE}):
            return True
        if soup.find(attrs={"id": _FAQ_CONTAINER_ID_RE}):
            return True

        # Microdata Question/Answer (wzorzec Q&A niezależny od bloku JSON-LD FAQPage).
        if soup.find(attrs={"itemtype": _SCHEMA_QUESTION_TYPE_RE}):
            return True

        return False

    # ------------------------------------------------------------------
    # Szum nagłówkowy (Hx Noise) i kolejność nagłówków względem H1
    # ------------------------------------------------------------------
    def _analyze_heading_noise(self, soup: BeautifulSoup) -> dict:
        ordered_headings = soup.find_all(HEADING_TAGS)
        first_h1_index = next(
            (i for i, tag in enumerate(ordered_headings) if tag.name == "h1"), None
        )

        headings_before_h1 = []
        if first_h1_index is not None:
            headings_before_h1 = [
                {"tag": tag.name, "text": tag.get_text(strip=True)}
                for tag in ordered_headings[:first_h1_index]
                if tag.name in ("h2", "h3")
            ]

        noisy_headings = []
        for tag in soup.find_all(("h3", "h4")):
            text = tag.get_text(strip=True)
            if any(keyword in text.lower() for keyword in HX_NOISE_KEYWORDS):
                noisy_headings.append({"tag": tag.name, "text": text})

        return {
            "headings_before_h1": headings_before_h1,
            "noisy_headings": noisy_headings,
        }

    # ------------------------------------------------------------------
    # Sygnały E-E-A-T: autorstwo i aktualność treści
    # ------------------------------------------------------------------
    def _analyze_eeat_signals(self, soup: BeautifulSoup) -> dict:
        rel_author = soup.find_all(rel="author")
        itemprop_author = soup.find_all(attrs={"itemprop": "author"})
        class_author = soup.find_all(class_=re.compile(r"author", re.I))
        has_author_signal = bool(rel_author or itemprop_author or class_author)

        modified_tag = soup.find("meta", property="article:modified_time")
        published_tag = soup.find("meta", property="article:published_time")

        return {
            "has_author_signal": has_author_signal,
            "modified_time": modified_tag.get("content") if modified_tag else None,
            "published_time": published_tag.get("content") if published_tag else None,
        }

    # ------------------------------------------------------------------
    # Linkowanie wewnętrzne
    # ------------------------------------------------------------------
    def _count_internal_links(self, soup: BeautifulSoup, url: str) -> int:
        """Liczy odnośniki <a href> prowadzące do tej samej domeny (lub adresy
        względne) - pomija kotwice (#), mailto:, tel: i javascript:."""
        domain = urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[len("www."):]

        count = 0
        for tag in soup.find_all("a", href=True):
            if self._is_internal_link(tag["href"], domain):
                count += 1
        return count

    def _is_internal_link(self, href: str, domain: str) -> bool:
        href = (href or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            return False

        netloc = urlparse(href).netloc.lower()
        if not netloc:
            return True  # adres względny -> ta sama domena

        if netloc.startswith("www."):
            netloc = netloc[len("www."):]
        return netloc == domain

    # ------------------------------------------------------------------
    # Renderowanie JavaScript (heurystyka SSR vs CSR)
    # ------------------------------------------------------------------
    def _analyze_js_rendering(self, soup: BeautifulSoup) -> dict:
        """Heurystyka SSR vs CSR: liczy widoczny tekst strony (bez kodu <script>/
        <style>) i zestawia go z liczbą znaczników <script>. Bardzo mało tekstu przy
        wielu skryptach (typowy wzorzec pustego <div id="root">/"app"> wypełnianego
        dopiero przez JS w przeglądarce) sugeruje renderowanie wyłącznie po stronie
        klienta (CSR) - taka treść jest niewidoczna dla części robotów wyszukiwarek
        i modeli LLM, które nie wykonują JavaScriptu."""
        script_count = len(soup.find_all("script"))

        # Kopia niezależna od `soup` używanego przez resztę parse() - decompose()
        # nieodwracalnie usuwa węzły, więc operujemy na osobnym drzewie.
        text_only = copy.deepcopy(soup)
        for tag in text_only(["script", "style", "noscript"]):
            tag.decompose()
        visible_text = text_only.get_text(separator=" ", strip=True)
        word_count = len(visible_text.split())

        likely_csr = word_count < JS_CSR_WORD_COUNT_THRESHOLD and script_count >= JS_CSR_MIN_SCRIPT_COUNT
        return {
            "word_count": word_count,
            "script_count": script_count,
            "likely_csr": likely_csr,
        }

    # ------------------------------------------------------------------
    # Dodatkowe, w pełni opcjonalne sprawdzenia sieciowe - każde jest wywoływane
    # osobno (patrz AuditService.run_audit) i niezależnie zabezpieczone: błąd
    # pojedynczego sprawdzenia (timeout, 404, brak nagłówka) nigdy nie przerywa
    # audytu ani nie wpływa na pozostałe sprawdzenia.
    # ------------------------------------------------------------------
    def check_robots_txt(self, base_url: str) -> dict:
        """Sprawdza obecność i podstawową treść pliku /robots.txt pod audytowaną
        domeną - osobne, krótkie zapytanie GET."""
        robots_url = self._build_absolute_url(base_url, "/robots.txt")
        try:
            response = httpx.get(
                validate_public_url(robots_url), headers=self.headers, timeout=min(self.timeout, 10.0)
            )
        except (httpx.HTTPError, UnsafeUrlError):
            return {"checked": True, "exists": False, "disallows_all": False}

        if response.status_code != 200:
            return {"checked": True, "exists": False, "disallows_all": False, "blocked_ai_bots": []}

        return {
            "checked": True,
            "exists": True,
            "disallows_all": self._robots_disallows_everything(response.text),
            "blocked_ai_bots": self._robots_blocked_ai_bots(response.text),
        }

    def _robots_blocked_ai_bots(self, content: str) -> list[str]:
        """Zwraca listę botów LLM, którym robots.txt blokuje dostęp do całej witryny.

        Istotne dla widoczności w odpowiedziach generowanych przez AI (GEO): jeśli
        `GPTBot`, `ClaudeBot`, `PerplexityBot` czy `Bytespider` dostaną "Disallow: /",
        treść witryny nie trafi do modeli i nie pojawi się w ich odpowiedziach.

        Blokadę zliczamy tylko wtedy, gdy `Disallow: /` obejmuje CAŁĄ witrynę -
        blokada pojedynczego katalogu (np. "Disallow: /admin/") jest normalną
        konfiguracją, a nie problemem. Uwzględniamy też grupę "User-agent: *",
        która obowiązuje boty bez własnej, dedykowanej sekcji.
        """
        # Mapowanie: nazwa user-agenta (lowercase) -> lista reguł Disallow w jego grupie.
        rules_by_agent: dict[str, list[str]] = {}
        current_agents: list[str] = []
        expecting_agents = False

        for raw_line in content.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field, value = field.strip().lower(), value.strip()

            if field == "user-agent":
                # Kolejne linie User-agent bez Disallow pomiędzy tworzą jedną grupę.
                if not expecting_agents:
                    current_agents = []
                    expecting_agents = True
                current_agents.append(value.lower())
                rules_by_agent.setdefault(value.lower(), [])
            elif field in ("disallow", "allow"):
                expecting_agents = False
                for agent in current_agents:
                    rules_by_agent.setdefault(agent, []).append(f"{field}:{value}")

        def blocks_everything(agent: str) -> bool:
            rules = rules_by_agent.get(agent)
            if rules is None:
                return False
            # "Allow: /" po "Disallow: /" znosi blokadę całej witryny.
            return "disallow:/" in rules and "allow:/" not in rules

        blocked = []
        for bot in AI_BOT_USER_AGENTS:
            agent = bot.lower()
            if blocks_everything(agent) or (agent not in rules_by_agent and blocks_everything("*")):
                blocked.append(bot)
        return blocked

    def _robots_disallows_everything(self, content: str) -> bool:
        """Czy robots.txt blokuje CAŁĄ witrynę dla wszystkich robotów
        ("User-agent: *" + "Disallow: /") - typowy, poważny błąd konfiguracji."""
        wildcard_user_agent = False
        for raw_line in content.splitlines():
            line = raw_line.strip().lower()
            if line.startswith("user-agent:"):
                wildcard_user_agent = line.split(":", 1)[1].strip() == "*"
            elif wildcard_user_agent and line.startswith("disallow:"):
                if line.split(":", 1)[1].strip() == "/":
                    return True
        return False

    def check_custom_404_page(self, base_url: str) -> dict:
        """Odpytuje jawnie nieistniejący adres pod audytowaną domeną, żeby sprawdzić,
        czy serwer poprawnie zwraca kod 404 (a nie "miękkie 404" - status 200 z
        generyczną stroną, mylące dla robotów indeksujących)."""
        probe_path = f"/seo-auditor-404-check-{uuid4().hex[:10]}"
        probe_url = self._build_absolute_url(base_url, probe_path)
        try:
            response = httpx.get(
                validate_public_url(probe_url),
                headers=self.headers,
                timeout=min(self.timeout, 10.0),
                follow_redirects=True,
            )
        except (httpx.HTTPError, UnsafeUrlError):
            return {"checked": False, "returns_404": False, "status_code": None}

        return {
            "checked": True,
            "returns_404": response.status_code == 404,
            "status_code": response.status_code,
        }

    def check_image_sizes(self, image_urls: list[str]) -> dict:
        """Sprawdza wagę (KB) próbki obrazków przez żądania HEAD (bez pobierania
        całej zawartości pliku). Obrazki bez nagłówka Content-Length lub z
        nieudanym żądaniem są pomijane - nie liczą się ani jako "OK", ani jako
        "zbyt ciężkie". Błąd pojedynczego obrazka nigdy nie przerywa sprawdzenia
        pozostałych."""
        oversized = []
        checked_count = 0
        for image_url in image_urls:
            try:
                # Adresy obrazków pochodzą z audytowanej (obcej) strony, więc są danymi
                # niezaufanymi - <img src="http://127.0.0.1:8000/..."> to najprostsza
                # droga do SSRF, jeśli nie sprawdzić ich tak samo jak adresu audytu.
                response = httpx.head(
                    validate_public_url(image_url),
                    headers=self.headers,
                    timeout=min(self.timeout, 8.0),
                    follow_redirects=True,
                )
                content_length = response.headers.get("content-length")
                if content_length is None:
                    continue
                size_kb = int(content_length) / 1024
            except (httpx.HTTPError, UnsafeUrlError, ValueError):
                continue

            checked_count += 1
            if size_kb > IMAGE_SIZE_LIMIT_KB:
                oversized.append({"src": image_url, "size_kb": round(size_kb)})

        return {"checked_count": checked_count, "oversized": oversized}

    def _build_absolute_url(self, base_url: str, path: str) -> str:
        parsed = urlparse(base_url)
        return f"{parsed.scheme}://{parsed.netloc}{path}"
