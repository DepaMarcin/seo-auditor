"""Klient Internet Archive (Wayback Machine) - wiek i historia domeny w archiwum.

Wiek domeny jest przybliżonym sygnałem autorytetu: witryna archiwizowana od lat ma za
sobą historię, której świeżo zarejestrowana domena nie zbuduje z dnia na dzień. Archiwum
NIE jest rejestrem domen - data pierwszej migawki mówi tylko, od kiedy Internet Archive
zna adres, więc traktujemy ją jako oszacowanie "od dołu" (domena może być starsza).

Zgodnie z konwencją pozostałych integracji zewnętrznych w tym projekcie (SenutoService,
PageSpeedService, GA4OAuthService): błąd komunikacji nigdy nie podnosi wyjątku na
zewnątrz - zwracany jest bezpieczny słownik z `available=False`, żeby niedostępność
archiwum nie przerywała audytu.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from urllib.parse import urlparse

import httpx
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Publiczne API Internet Archive - bez klucza i bez uwierzytelniania.
AVAILABILITY_API_URL = "http://archive.org/wayback/available"

# CDX API pozwala pobrać NAJSTARSZĄ migawkę (sortowanie rosnące + limit 1); endpoint
# /wayback/available zwraca migawkę najbliższą podanej dacie, nie pierwszą w historii.
CDX_API_URL = "http://web.archive.org/cdx/search/cdx"

# CDX potrafi odpowiadać wolno przy pierwszym zapytaniu o daną domenę (buduje wtedy
# indeks), a audyt i tak wykonuje się w tle - dajemy mu więcej czasu niż scraperowi.
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Data, od której pytamy CDX o najstarszą migawkę - Internet Archive działa od 1996 r.
ARCHIVE_EPOCH = "1996"


class WaybackService:
    """Pobiera datę pierwszej migawki domeny w Wayback Machine."""

    def __init__(self, timeout: httpx.Timeout | float = DEFAULT_TIMEOUT):
        self.timeout = timeout

    def fetch_domain_history(self, url: str) -> dict:
        """Zwraca informacje o obecności domeny w archiwum:

        {
            "available": bool,        # czy udało się odpytać archiwum
            "archived": bool,         # czy domena ma w ogóle jakąkolwiek migawkę
            "first_snapshot": "YYYY-MM-DD" | None,
            "age_years": float | None,
            "age_days": int | None,
            "snapshot_url": str | None,
            "error": str | None,
        }
        """
        domain = self._extract_domain(url)
        if not domain:
            return self._fallback("Nie udało się wyodrębnić domeny z adresu audytu.")

        # Archiwum zmienia się w skali miesięcy, a nie minut - dzienny cache w zupełności
        # wystarcza i oszczędza publiczne, nielimitowane API Internet Archive.
        cache_key = f"wayback:{domain}"
        cached = cache.get(cache_key)
        if cached is not None:
            logger.info("Wayback: historia domeny %s pobrana z cache.", domain)
            return cached

        result = self._fetch_oldest_snapshot(domain)
        # Udaną odpowiedź trzymamy dobę (archiwum zmienia się w skali miesięcy).
        # Nieudaną - tylko kilkanaście minut: zapytanie do CDX potrafi trwać ~20 s, więc
        # ponawianie go przy każdym audycie po chwilowym limicie 429 byłoby kosztowne,
        # ale błąd musi mieć szansę "wygasnąć" bez czekania całej doby.
        ttl = (
            getattr(settings, "CACHE_TTL_WAYBACK", 24 * 3600)
            if result["available"]
            else getattr(settings, "CACHE_TTL_WAYBACK_FAILURE", 15 * 60)
        )
        cache.set(cache_key, result, ttl)
        return result

    def _fetch_oldest_snapshot(self, domain: str) -> dict:
        """Odpytuje CDX API o najstarszą migawkę, z awaryjnym zejściem na /wayback/available."""
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(
                    CDX_API_URL,
                    params={
                        "url": domain,
                        "output": "json",
                        "fl": "timestamp,original",
                        "filter": "statuscode:200",
                        "collapse": "timestamp:4",
                        "limit": 1,
                        "from": ARCHIVE_EPOCH,
                    },
                )
                response.raise_for_status()
                rows = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Nie udało się połączyć z CDX API Wayback Machine dla %s: %s", domain, type(exc).__name__)
            return self._fetch_from_availability_api(domain)
        except ValueError:
            logger.warning("Nieprawidłowa odpowiedź CDX API Wayback Machine dla %s.", domain)
            return self._fetch_from_availability_api(domain)

        # Pierwszy wiersz odpowiedzi CDX to nagłówek kolumn - dane zaczynają się od drugiego.
        if not isinstance(rows, list) or len(rows) < 2:
            return self._archived_none()

        timestamp = rows[1][0] if rows[1] else None
        snapshot_date = self._parse_timestamp(timestamp)
        if snapshot_date is None:
            return self._archived_none()

        return self._build_result(snapshot_date, f"http://web.archive.org/web/{timestamp}/{domain}")

    def _fetch_from_availability_api(self, domain: str) -> dict:
        """Zapasowe źródło: `/wayback/available`.

        Zwraca migawkę najbliższą podanej dacie, więc pytamy o rok 1996 - archiwum
        odpowie wtedy najwcześniejszą migawką, jaką ma dla tego adresu.
        """
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(
                    AVAILABILITY_API_URL, params={"url": domain, "timestamp": ARCHIVE_EPOCH}
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            # 429 to limit zapytań po stronie Internet Archive, a nie problem z domeną -
            # rozróżniamy to w komunikacie, żeby nie sugerować, że domena jest nowa.
            if exc.response.status_code == 429:
                logger.info("Internet Archive ograniczyło liczbę zapytań (429) dla %s.", domain)
                return self._fallback("Archiwum Internet Archive chwilowo ogranicza liczbę zapytań.")
            logger.warning(
                "Archiwum Internet Archive zwróciło błąd HTTP %s dla %s.", exc.response.status_code, domain
            )
            return self._fallback("Nie udało się połączyć z archiwum Internet Archive.")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "Nie udało się pobrać historii Wayback Machine dla %s: %s", domain, type(exc).__name__
            )
            return self._fallback("Nie udało się połączyć z archiwum Internet Archive.")

        snapshot = (payload.get("archived_snapshots") or {}).get("closest") or {}
        if not snapshot.get("available"):
            return self._archived_none()

        snapshot_date = self._parse_timestamp(snapshot.get("timestamp"))
        if snapshot_date is None:
            return self._archived_none()

        return self._build_result(snapshot_date, snapshot.get("url"))

    def _build_result(self, snapshot_date: date, snapshot_url: str | None) -> dict:
        age_days = (date.today() - snapshot_date).days
        return {
            "available": True,
            "archived": True,
            "first_snapshot": snapshot_date.isoformat(),
            "age_days": age_days,
            "age_years": round(age_days / 365.25, 1),
            "snapshot_url": snapshot_url,
            "error": None,
        }

    def _parse_timestamp(self, timestamp: str | None) -> date | None:
        """Znacznik czasu Wayback ("YYYYMMDDhhmmss") na datę."""
        if not timestamp or len(timestamp) < 8:
            return None
        try:
            return datetime.strptime(timestamp[:8], "%Y%m%d").date()
        except ValueError:
            return None

    def _extract_domain(self, url: str) -> str:
        normalized = url if "://" in url else f"https://{url}"
        domain = urlparse(normalized).netloc.lower().split(":")[0]
        return domain[len("www."):] if domain.startswith("www.") else domain

    def _archived_none(self) -> dict:
        """Archiwum odpowiedziało, ale nie zna tej domeny."""
        return {
            "available": True,
            "archived": False,
            "first_snapshot": None,
            "age_days": None,
            "age_years": None,
            "snapshot_url": None,
            "error": None,
        }

    def _fallback(self, error_message: str) -> dict:
        return {
            "available": False,
            "archived": False,
            "first_snapshot": None,
            "age_days": None,
            "age_years": None,
            "snapshot_url": None,
            "error": error_message,
        }
