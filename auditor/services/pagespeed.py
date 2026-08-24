from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
STRATEGIES = ("mobile", "desktop")

# PSI potrafi odpowiadać bardzo wolno dla ciężkich stron - dajemy jej więcej czasu
# niż standardowe zapytania scrapera, zachowując krótszy limit na samo połączenie.
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
RETRY_BACKOFF_SECONDS = 1.0

# Kody błędów, dla których warto ponowić żądanie - PSI bywa niestabilne po stronie
# Google (400 potrafi wystąpić przejściowo obok "prawdziwego" błędu wejścia).
RETRYABLE_STATUS_CODES = {400, 404, 500, 502, 503, 504}

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


class PageSpeedService:
    """Klient Google PageSpeed Insights API (v5) - wynik wydajności i Core Web Vitals."""

    def __init__(self, timeout: httpx.Timeout | float = DEFAULT_TIMEOUT, max_retries: int = 1):
        self.timeout = timeout
        self.max_retries = max_retries
        self.api_key = getattr(settings, "PAGESPEED_API_KEY", "") or None

    def analyze_all(self, url: str) -> dict:
        """Odpytuje PageSpeed Insights równolegle dla strategii mobile i desktop.

        Każda strategia korzysta z własnej, niezależnej instancji ``httpx.Client``
        (utworzonej wewnątrz ``analyze``), dzięki czemu wolna/zawieszona odpowiedź
        dla jednej strategii nie wpływa na drugą.
        """
        with ThreadPoolExecutor(max_workers=len(STRATEGIES)) as executor:
            futures = {
                strategy: executor.submit(self.analyze, url, strategy) for strategy in STRATEGIES
            }
            return {strategy: future.result() for strategy, future in futures.items()}

    def analyze(self, url: str, strategy: str = "mobile") -> dict:
        """Odpytuje PageSpeed Insights i zwraca ustandaryzowany słownik wyników."""
        normalized_url = self._normalize_url(url)
        params = {"url": normalized_url, "strategy": strategy, "category": "performance"}
        if self.api_key:
            params["key"] = self.api_key

        attempts = self.max_retries + 1
        last_message = f"Nie udało się połączyć z PageSpeed Insights (strategia: {strategy})."

        for attempt in range(1, attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(API_URL, params=params)
                    response.raise_for_status()
                    data = response.json()
                return self._parse(data)
            except httpx.ReadTimeout:
                logger.warning(
                    "Przekroczono limit czasu odpowiedzi PageSpeed Insights "
                    "(próba %s/%s, strategia: %s) url=%s",
                    attempt, attempts, strategy, url,
                )
                last_message = (
                    f"PageSpeed Insights nie odpowiedziało w wyznaczonym czasie (strategia: {strategy}). "
                    "Spróbuj ponowić audyt za chwilę."
                )
                if attempt < attempts:
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                return self._fallback(last_message)
            except httpx.HTTPStatusError as exc:
                # Uwaga: nie wolno logować/zapisywać pełnego wyjątku ani URL-a żądania -
                # PSI przyjmuje klucz API jako parametr query, więc trafiłby on do bazy,
                # widoku HTML oraz promptu RAG.
                status_code = exc.response.status_code
                message = f"PageSpeed Insights zwróciło błąd HTTP {status_code} (strategia: {strategy})."
                logger.warning(
                    "%s próba=%s/%s url=%s", message, attempt, attempts, url
                )
                last_message = message
                if status_code in RETRYABLE_STATUS_CODES and attempt < attempts:
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                return self._fallback(message)
            except httpx.HTTPError as exc:
                message = f"Nie udało się połączyć z PageSpeed Insights (strategia: {strategy}): {type(exc).__name__}."
                logger.warning("%s url=%s", message, url)
                return self._fallback(message)
            except ValueError:
                message = f"Otrzymano nieprawidłową odpowiedź z PageSpeed Insights (strategia: {strategy})."
                logger.warning("%s url=%s", message, url)
                return self._fallback(message)

        # Nieosiągalne w praktyce - zabezpieczenie na wypadek wyczerpania pętli bez zwrotu.
        # Brak wyniku dla jednej strategii (np. desktop) nie może wywalić całego audytu -
        # zwracamy bezpieczny słownik, druga strategia i pozostałe metryki nadal się wyświetlą.
        return self._fallback(last_message)

    def _normalize_url(self, url: str) -> str:
        """PSI odrzuca (HTTP 400) adresy bez schematu lub z otaczającymi białymi znakami -
        np. "example.com" albo " https://example.com " - normalizujemy je przed wysyłką."""
        normalized = (url or "").strip()
        if normalized and not _SCHEME_RE.match(normalized):
            normalized = f"https://{normalized}"
        return normalized

    def _parse(self, data: dict) -> dict:
        try:
            lighthouse = data["lighthouseResult"]
            audits = lighthouse["audits"]
            performance_score = round(lighthouse["categories"]["performance"]["score"] * 100)
        except (KeyError, TypeError) as exc:
            logger.warning("Nie udało się sparsować odpowiedzi PageSpeed Insights: %s", exc)
            return self._fallback("Odpowiedź PageSpeed Insights nie zawierała oczekiwanych danych.")

        return {
            "available": True,
            "error": None,
            "performance_score": performance_score,
            "lcp": self._audit_value(audits, "largest-contentful-paint", divide_by=1000),
            "cls": self._audit_value(audits, "cumulative-layout-shift"),
            "fcp": self._audit_value(audits, "first-contentful-paint", divide_by=1000),
            "inp": self._extract_inp(data, audits),
        }

    def _audit_value(self, audits: dict, key: str, divide_by: float = 1) -> float | None:
        value = audits.get(key, {}).get("numericValue")
        return value / divide_by if value is not None else None

    def _extract_inp(self, data: dict, audits: dict) -> float | None:
        # Preferuj dane terenowe CrUX (loadingExperience) - to one zasilają realny ranking Core Web Vitals.
        field_metrics = (data.get("loadingExperience") or {}).get("metrics", {})
        inp_field = field_metrics.get("INTERACTION_TO_NEXT_PAINT")
        if inp_field and inp_field.get("percentile") is not None:
            return inp_field["percentile"]

        # Fallback: laboratoryjny audyt Lighthouse (dostępny tylko w nowszych wersjach).
        return self._audit_value(audits, "interaction-to-next-paint")

    def _fallback(self, error_message: str) -> dict:
        return {
            "available": False,
            "error": error_message,
            "performance_score": None,
            "lcp": None,
            "cls": None,
            "fcp": None,
            "inp": None,
        }
