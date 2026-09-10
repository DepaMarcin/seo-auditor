"""Parser `sitemap.xml` - podpowiada po jednym reprezentatywnym adresie na typ szablonu.

Audyt wielu szablonów wymaga od użytkownika wskazania przykładowej kategorii, produktu,
wpisu blogowego i strony ofertowej. Ręczne wyklikiwanie ich w serwisie jest żmudne,
więc czytamy mapę witryny i klasyfikujemy adresy po wzorcach w ścieżce URL.

Klasyfikacja jest heurystyczna i celowo zachowawcza: podpowiedź trafia do formularza,
gdzie użytkownik ją widzi i może poprawić przed uruchomieniem audytu.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from django.conf import settings
from django.core.cache import cache

from .url_guard import UnsafeUrlError, validate_public_url

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=8.0)

# Typowe lokalizacje mapy witryny sprawdzane po kolei, gdy robots.txt nic nie wskaże.
COMMON_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/wp-sitemap.xml")

# Ile adresów maksymalnie analizujemy - mapy dużych sklepów mają dziesiątki tysięcy
# wpisów, a do wskazania jednego przykładu na szablon wystarczy pierwsza porcja.
MAX_URLS_TO_SCAN = 2000

# Ile map podrzędnych pobieramy z indeksu (sitemap index) - każda to osobne żądanie.
MAX_CHILD_SITEMAPS = 5

# Wzorce ścieżek rozpoznające typ szablonu. Kolejność ma znaczenie: pierwszy trafiony
# wzorzec wygrywa, więc bardziej specyficzne typy (produkt, blog) idą przed ogólnymi.
PAGE_TYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("product", re.compile(r"/(produkt|produkty|product|products|p|item|sklep/[^/]+/[^/]+)/", re.I)),
    ("blog", re.compile(r"/(blog|artykul|artykuly|article|articles|news|aktualnosci|poradnik|poradniki)/", re.I)),
    ("offer", re.compile(r"/(oferta|oferty|uslugi|usluga|offer|services|service|cennik|pricing)/", re.I)),
    ("category", re.compile(r"/(kategoria|kategorie|category|categories|c|kolekcja|collection|sklep)/", re.I)),
]


class SitemapService:
    """Pobiera mapę witryny i klasyfikuje znalezione adresy wg typu szablonu."""

    def __init__(self, timeout: httpx.Timeout | float = DEFAULT_TIMEOUT):
        self.timeout = timeout

    def suggest_pages(self, url: str) -> dict:
        """Zwraca po jednym przykładowym adresie na typ szablonu.

        {
            "available": bool,
            "sitemap_url": str | None,
            "suggestions": {"category": "https://...", "product": "https://...", ...},
            "scanned_urls": int,
            "error": str | None,
        }

        Nigdy nie podnosi wyjątku - brak mapy witryny to nie błąd, tylko brak podpowiedzi
        (użytkownik wpisze adresy ręcznie).
        """
        try:
            safe_url = validate_public_url(url)
        except UnsafeUrlError as exc:
            return self._empty(str(exc))

        origin = self._origin(safe_url)
        cache_key = f"sitemap:{origin}"
        cached = cache.get(cache_key)
        if cached is not None:
            logger.info("Sitemap: podpowiedzi dla %s pobrane z cache.", origin)
            return cached

        result = self._discover_and_classify(origin)
        ttl = (
            getattr(settings, "CACHE_TTL_SITEMAP", 6 * 3600)
            if result["available"]
            else getattr(settings, "CACHE_TTL_SITEMAP_FAILURE", 15 * 60)
        )
        cache.set(cache_key, result, ttl)
        return result

    def _discover_and_classify(self, origin: str) -> dict:
        sitemap_url, urls = self._collect_urls(origin)
        if not urls:
            return self._empty("Nie znaleziono mapy witryny (sitemap.xml) dla tej domeny.")

        suggestions = self._classify(urls, origin)
        return {
            "available": True,
            "sitemap_url": sitemap_url,
            "suggestions": suggestions,
            "scanned_urls": len(urls),
            "error": None,
        }

    def _collect_urls(self, origin: str) -> tuple[str | None, list[str]]:
        """Znajduje mapę witryny i zwraca listę adresów (rozwijając indeks map)."""
        for candidate in self._sitemap_candidates(origin):
            body = self._fetch(candidate)
            if not body:
                continue

            child_maps, page_urls = self._parse_sitemap(body)
            if page_urls:
                return candidate, page_urls[:MAX_URLS_TO_SCAN]

            # Indeks map - schodzimy o poziom niżej, do pierwszych kilku map podrzędnych.
            collected: list[str] = []
            for child in child_maps[:MAX_CHILD_SITEMAPS]:
                child_body = self._fetch(child)
                if not child_body:
                    continue
                _, child_urls = self._parse_sitemap(child_body)
                collected.extend(child_urls)
                if len(collected) >= MAX_URLS_TO_SCAN:
                    break
            if collected:
                return candidate, collected[:MAX_URLS_TO_SCAN]

        return None, []

    def _sitemap_candidates(self, origin: str) -> list[str]:
        """Adresy map do sprawdzenia: najpierw te wskazane w robots.txt, potem typowe."""
        candidates: list[str] = []

        robots_body = self._fetch(urljoin(origin, "/robots.txt"))
        if robots_body:
            for line in robots_body.splitlines():
                field, _, value = line.partition(":")
                if field.strip().lower() == "sitemap" and value.strip():
                    candidates.append(value.strip())

        candidates.extend(urljoin(origin, path) for path in COMMON_SITEMAP_PATHS)
        # Zachowujemy kolejność, usuwając duplikaty.
        return list(dict.fromkeys(candidates))

    def _fetch(self, url: str) -> str | None:
        """Pobiera zasób, zwracając None przy dowolnym problemie (także niedozwolonym adresie)."""
        try:
            safe_url = validate_public_url(url)
        except UnsafeUrlError:
            return None

        try:
            response = httpx.get(safe_url, timeout=self.timeout, follow_redirects=True)
            if response.status_code != 200:
                return None
            return response.text
        except httpx.HTTPError:
            return None

    def _parse_sitemap(self, body: str) -> tuple[list[str], list[str]]:
        """Zwraca (adresy map podrzędnych, adresy stron) z dokumentu sitemap."""
        try:
            root = ElementTree.fromstring(body.encode("utf-8", errors="ignore"))
        except ElementTree.ParseError:
            return [], []

        # Mapę rodziców budujemy JEDEN raz - szukanie rodzica osobno dla każdego <loc>
        # dawałoby złożoność kwadratową, a mapy dużych sklepów mają tysiące wpisów.
        parent_tags = {
            id(child): parent.tag.rsplit("}", 1)[-1].lower()
            for parent in root.iter()
            for child in parent
        }

        child_maps: list[str] = []
        page_urls: list[str] = []
        for element in root.iter():
            # Nazwy znaczników niosą przestrzeń nazw ("{...}loc") - porównujemy sufiks.
            if not element.tag.endswith("}loc") and element.tag != "loc":
                continue
            value = (element.text or "").strip()
            if not value:
                continue
            is_child_sitemap = parent_tags.get(id(element), "") == "sitemap"
            (child_maps if is_child_sitemap else page_urls).append(value)

        return child_maps, page_urls

    def _classify(self, urls: list[str], origin: str) -> dict[str, str]:
        """Przypisuje pierwszy pasujący adres do każdego typu szablonu."""
        suggestions: dict[str, str] = {}
        origin_host = urlparse(origin).netloc.lower()

        for url in urls:
            parsed = urlparse(url)
            # Mapy witryn często zawierają adresy z innych subdomen (np. demo.*) -
            # podpowiadanie ich byłoby mylące, bo audyt dotyczy wskazanej domeny.
            if parsed.netloc.lower() != origin_host:
                continue

            path = parsed.path or "/"
            if path in ("", "/"):
                suggestions.setdefault("homepage", url)
                continue

            for page_type, pattern in PAGE_TYPE_PATTERNS:
                if page_type in suggestions:
                    continue
                # Dokładamy końcowy ukośnik, żeby wzorzec łapał też ostatni segment ścieżki.
                if pattern.search(path if path.endswith("/") else path + "/"):
                    suggestions[page_type] = url
                    break

            if len(suggestions) >= len(PAGE_TYPE_PATTERNS) + 1:
                break

        return suggestions

    def _origin(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _empty(self, error: str) -> dict:
        return {
            "available": False,
            "sitemap_url": None,
            "suggestions": {},
            "scanned_urls": 0,
            "error": error,
        }
