"""Skrypt diagnostyczny integracji z API Senuto.

Wykonuje bezpośrednie zapytania HTTP do oficjalnego API Senuto (z pominięciem
`auditor.services.senuto.SenutoService`), żeby zweryfikować krok po kroku:
klucz API, wysyłaną domenę, pełny URL i nagłówki żądania, kod statusu HTTP,
surową odpowiedź serwera oraz dokładne klucze zwracane w JSON-ie.

Użycie:
    python test_senuto.py [domena]

Domyślna domena: harbingers.io
"""

from __future__ import annotations

import sys

import httpx
from dotenv import load_dotenv
import os
from datetime import date, timedelta

load_dotenv()

# Zweryfikowane na podstawie oficjalnej kolekcji Postman API Senuto
# (https://docs-api.senuto.com/) - baza URL bez segmentu "/v2", country_id Polski = 1.
API_BASE_URL = "https://api.senuto.com/api"
COUNTRY_ID_PL = 1
FETCH_MODE = "topLevelDomain"
HISTORY_DAYS = 90
DEFAULT_DOMAIN = "harbingers.io"

_DATE_MAX = date.today()
_DATE_MIN = _DATE_MAX - timedelta(days=HISTORY_DAYS)

ENDPOINTS = {
    "Statystyki widoczności (TOP3 / TOP10 / TOP50)": (
        "/visibility_analysis/reports/dashboard/getDomainStatistics",
        {"fetch_mode": FETCH_MODE, "country_id": COUNTRY_ID_PL},
    ),
    "Historia widoczności": (
        "/visibility_analysis/reports/domain_positions/getPositionsHistoryChartDataForAllTypes",
        {"fetch_mode": FETCH_MODE, "date_min": _DATE_MIN.isoformat(), "date_max": _DATE_MAX.isoformat()},
    ),
}


def mask_secret(value: str) -> str:
    """Zwraca zanonimizowaną postać sekretu, np. "abc1...xyz9", do bezpiecznego
    wypisania w konsoli bez ujawniania pełnej wartości klucza API."""
    if not value:
        return "(brak)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def clean_domain(raw: str) -> str:
    """Wyciąga czystą domenę z dowolnego adresu URL - usuwa schemat, "www.",
    ścieżki i końcowe ukośniki, np. "https://www.harbingers.io/" -> "harbingers.io"."""
    raw = raw.strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]
    if raw.startswith("www."):
        raw = raw[len("www."):]
    return raw.lower()


def describe_json_shape(data) -> str:
    """Opisuje strukturę odpowiedzi JSON (jakie klucze zwraca serwer), żeby łatwo
    zweryfikować, czy parser w SenutoService czyta właściwe pola."""
    if isinstance(data, dict):
        return f"obiekt (dict) - klucze najwyższego poziomu: {list(data.keys())}"
    if isinstance(data, list):
        if not data:
            return "lista (list) - pusta"
        first = data[0]
        if isinstance(first, dict):
            return f"lista (list) obiektów - klucze pierwszego elementu: {list(first.keys())}"
        return f"lista (list) elementów typu {type(first).__name__}"
    return f"wartość typu {type(data).__name__}"


def main() -> None:
    api_key = os.getenv("SENUTO_API_KEY", "")
    domain = clean_domain(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DOMAIN)

    print("=" * 78)
    print("DIAGNOSTYKA API SENUTO")
    print("=" * 78)
    print(f"Klucz SENUTO_API_KEY znaleziony w .env: {'TAK' if api_key else 'NIE'}")
    print(f"Klucz (zanonimizowany): {mask_secret(api_key)}")
    print(f"Odpytywana domena: {domain}")
    print()

    if not api_key:
        print("BRAK KLUCZA API - przerywam. Uzupełnij SENUTO_API_KEY w pliku .env.")
        return

    headers = {"Authorization": f"Bearer {api_key}"}
    masked_headers = {"Authorization": f"Bearer {mask_secret(api_key)}"}

    for label, (path, extra_params) in ENDPOINTS.items():
        url = f"{API_BASE_URL}{path}"
        params = {"domain": domain, **extra_params}

        print("-" * 78)
        print(f"[{label}]")
        print(f"1. Pełny URL żądania: {url}")
        print(f"   Parametry zapytania: {params}")
        print(f"   Nagłówki (Headers, klucz zanonimizowany): {masked_headers}")
        print()

        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                response = client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            print(f"BŁĄD POŁĄCZENIA ({type(exc).__name__}): {exc}")
            print()
            continue

        print(f"2. Kod statusu HTTP: {response.status_code} {response.reason_phrase}")
        print()

        print("3. Surowa odpowiedź serwera:")
        try:
            data = response.json()
            print(data)
        except ValueError:
            data = None
            print("(odpowiedź nie jest poprawnym JSON-em - surowy tekst poniżej)")
            print(response.text[:2000])
        print()

        print("4. Struktura pól JSON (do weryfikacji parsera SenutoService):")
        if data is not None:
            print(f"   {describe_json_shape(data)}")
            if isinstance(data, dict) and data.get("success") is False:
                print(f"   UWAGA: serwer zwrócił success=false mimo HTTP {response.status_code} "
                      f"- komunikat: {data.get('message')!r}")
        else:
            print("   (brak - odpowiedź nie była JSON-em)")
        print()

    print("=" * 78)
    print("Koniec diagnostyki.")


if __name__ == "__main__":
    main()
