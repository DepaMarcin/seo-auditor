from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.senuto.com/api/v2"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Id kraju wymagany przez API Senuto dla wszystkich zapytań o widoczność - 208 to
# rynek polski (PL).
COUNTRY_ID_PL = 208

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


class SenutoService:
    """Klient API Senuto - statystyki widoczności domeny (liczba fraz w TOP3/TOP10/TOP50)
    oraz historia widoczności w czasie.

    Zgodnie z założeniem, że dane widoczności są dodatkiem do audytu, a nie jego
    krytycznym elementem: brak klucza API, brak domeny w bazie Senuto, limit zapytań
    czy jakikolwiek inny błąd komunikacji nigdy nie przerywa audytu - w każdym takim
    wypadku zwracany jest bezpieczny słownik z zerowymi wartościami (patrz `_fallback`).
    """

    def __init__(self, timeout: httpx.Timeout | float = DEFAULT_TIMEOUT):
        self.timeout = timeout
        # `settings.SENUTO_API_KEY` (zasilane przez python-dotenv z .env) jest źródłem
        # podstawowym; `os.getenv` to dodatkowy fallback na wypadek uruchomienia kodu
        # poza kontekstem aplikacji Django (np. w samodzielnym skrypcie/powłoce).
        self.api_key = getattr(settings, "SENUTO_API_KEY", "") or os.getenv("SENUTO_API_KEY", "")
        if not self.api_key:
            logger.error("Brak klucza SENUTO_API_KEY w konfiguracji .env")

    def get_visibility_stats(self, url_or_domain: str) -> dict:
        """Zwraca liczbę fraz w TOP3/TOP10/TOP50 oraz historię widoczności dla domeny
        wyodrębnionej z `url_or_domain`. W razie braku klucza API lub błędu API zwraca
        bezpieczny fallback (zerowe wartości), aby nie blokować generowania audytu."""
        domain = self._normalize_domain(url_or_domain)
        if not self.api_key:
            logger.error("Pomijam zapytanie do Senuto dla domeny %s - brak klucza SENUTO_API_KEY.", domain)
            return self._fallback()
        if not domain:
            logger.warning("Pomijam zapytanie do Senuto - nie udało się wyodrębnić domeny z %r.", url_or_domain)
            return self._fallback()

        logger.info("Senuto: rozpoczynam pobieranie statystyk widoczności dla domeny %s.", domain)

        try:
            with httpx.Client(timeout=self.timeout, headers=self._headers()) as client:
                summary = self._fetch_visibility_summary(client, domain)
                history = self._fetch_visibility_history(client, domain)
            return {
                "available": True,
                "error": None,
                "top3": summary["top3"],
                "top10": summary["top10"],
                "top50": summary["top50"],
                "history": history,
            }
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            raw_body = self._safe_response_text(exc.response)
            if status_code == 401:
                logger.error(
                    "Senuto: błąd autoryzacji (401) dla domeny %s - sprawdź poprawność SENUTO_API_KEY. "
                    "Odpowiedź serwera: %s",
                    domain, raw_body,
                )
            elif status_code == 400:
                logger.error(
                    "Senuto: błędne zapytanie (400) dla domeny %s - sprawdź format domeny. Odpowiedź serwera: %s",
                    domain, raw_body,
                )
            elif status_code == 404:
                logger.info("Senuto: domena %s nie została odnaleziona w bazie Senuto. Odpowiedź serwera: %s",
                            domain, raw_body)
            else:
                logger.error(
                    "Senuto zwróciło błąd HTTP %s dla domeny %s. Odpowiedź serwera: %s",
                    status_code, domain, raw_body,
                )
            return self._fallback()
        except httpx.HTTPError:
            logger.warning("Nie udało się połączyć z API Senuto dla domeny %s.", domain, exc_info=True)
            return self._fallback()
        except (KeyError, TypeError, ValueError):
            logger.warning("Nie udało się sparsować odpowiedzi API Senuto dla domeny %s.", domain, exc_info=True)
            return self._fallback()
        except Exception:
            # Ostatnia linia obrony - żaden nieprzewidziany błąd integracji z Senuto
            # nie może uniemożliwić wygenerowania reszty audytu SEO.
            logger.warning("Nieoczekiwany błąd podczas pobierania danych Senuto dla %s.", domain, exc_info=True)
            return self._fallback()

    def _headers(self) -> dict:
        # Dokumentacja Senuto posługuje się nagłówkiem "Api-Token", część wdrożeń API v2
        # akceptuje też standardowy "Authorization: Bearer" - wysyłamy oba, żeby
        # integracja działała niezależnie od tego, którego wariantu wymaga konkretne konto.
        return {
            "Api-Token": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
        }

    def _fetch_visibility_summary(self, client: httpx.Client, domain: str) -> dict:
        endpoint = f"{API_BASE_URL}/visibility_analysis/domain_visibility"
        response = client.get(endpoint, params={"domain": domain, "country_id": COUNTRY_ID_PL})
        self._log_response(domain, response)
        response.raise_for_status()
        data = response.json()
        return {
            "top3": data.get("top3_count") or 0,
            "top10": data.get("top10_count") or 0,
            "top50": data.get("top50_count") or 0,
        }

    def _fetch_visibility_history(self, client: httpx.Client, domain: str) -> list[dict]:
        endpoint = f"{API_BASE_URL}/visibility_analysis/domain_history"
        response = client.get(endpoint, params={"domain": domain, "country_id": COUNTRY_ID_PL})
        self._log_response(domain, response)
        response.raise_for_status()
        data = response.json()
        return [
            {"date": point.get("date"), "visibility": point.get("visibility") or 0}
            for point in data.get("history", [])
        ]

    def _log_response(self, clean_domain: str, response: httpx.Response) -> None:
        """Logowanie diagnostyczne każdego zapytania do Senuto - domena, kod statusu
        i surowa odpowiedź JSON - żeby błąd auth (401), błąd domeny (400) czy brak
        danych był od razu widoczny w konsoli/logach."""
        logger.info(f"Senuto Request Domain: {clean_domain}")
        logger.info(f"Senuto Response Status: {response.status_code}")
        try:
            logger.info(f"Senuto Response JSON: {response.json()}")
        except ValueError:
            logger.info(f"Senuto Response Body (nie-JSON): {self._safe_response_text(response)}")

    def _safe_response_text(self, response: httpx.Response) -> str:
        """Zwraca surową treść odpowiedzi do logów diagnostycznych, obcinając ją do
        rozsądnej długości. Odpowiedzi Senuto nie zawierają klucza API (przesyłanego
        wyłącznie w nagłówkach żądania), więc bezpiecznie je logować."""
        try:
            text = response.text
        except Exception:
            return "(nie udało się odczytać treści odpowiedzi)"
        return text[:500]

    def _normalize_domain(self, value: str) -> str:
        """Wyodrębnia czystą domenę z dowolnego adresu URL: usuwa schemat (http/https),
        prefiks www., ścieżki, parametry zapytania oraz końcowe ukośniki -
        np. "https://www.harbingers.io/blog/?x=1" -> "harbingers.io"."""
        value = (value or "").strip()
        if not value:
            return ""
        if not _SCHEME_RE.match(value):
            value = f"https://{value}"

        domain = urlparse(value).netloc.lower()
        if domain.startswith("www."):
            domain = domain[len("www."):]
        return domain.rstrip("/")

    def _fallback(self) -> dict:
        return {
            "available": False,
            "error": "Brak danych widoczności Senuto dla tej domeny.",
            "top3": 0,
            "top10": 0,
            "top50": 0,
            "history": [],
        }
