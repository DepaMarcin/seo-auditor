"""Walidacja adresów URL przyjmowanych od użytkownika i napotykanych w przekierowaniach.

Jeden moduł zamyka dwie różne podatności o wspólnej przyczynie (brak walidacji wejścia):

* **SSRF** - `SEOScraper`/`PageSpeedService` odpytują dowolny podany adres, więc bez
  kontroli hosta użytkownik mógłby przez audyt skanować sieć wewnętrzną albo pobrać
  metadane chmury (169.254.169.254). Sam `django.core.validators.URLValidator` tego NIE
  blokuje - przepuszcza `127.0.0.1`, `localhost`, `10.0.0.0/8` i `[::1]`.
* **Stored XSS** - `Audit.objects.create()` nie uruchamia walidatorów pola, więc adres
  `javascript:alert(1)` trafiał do bazy, a potem do `<iframe src>` i `<a href>` w
  szablonie. Autoescape Django nie chroni przed schematem URL - dopiero ograniczenie
  schematów do http/https zamyka ten wektor.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

ALLOWED_SCHEMES = ("http", "https")

_url_validator = URLValidator(schemes=list(ALLOWED_SCHEMES))

# Maksymalna liczba przekierowań, którą scraper podąża "ręcznie" - każdy skok jest
# walidowany osobno, bo publiczny adres może przekierować w głąb sieci lokalnej.
MAX_REDIRECT_HOPS = 5


class UnsafeUrlError(ValueError):
    """Adres nie nadaje się do audytu (zły schemat albo host w sieci prywatnej)."""


def _is_blocked_ip(address: str) -> bool:
    """Czy adres IP należy do puli, której nigdy nie wolno odpytać z serwera."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        # Nierozpoznany format adresu traktujemy zachowawczo jako niebezpieczny.
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_public_url(raw_url: str) -> str:
    """Zwraca znormalizowany, bezpieczny do odpytania adres albo podnosi `UnsafeUrlError`.

    Normalizacja dokłada brakujący schemat ("example.com" -> "https://example.com"),
    zgodnie z zachowaniem `SEOScraper._normalize_url`/`PageSpeedService._normalize_url`,
    żeby walidacja nie odrzucała adresów, które audyt i tak by przyjął.
    """
    url = (raw_url or "").strip()
    if not url:
        raise UnsafeUrlError("Podaj adres URL do audytu.")
    if "://" not in url:
        url = f"https://{url}"

    try:
        _url_validator(url)
    except ValidationError as exc:
        raise UnsafeUrlError("Podaj poprawny adres zaczynający się od http:// lub https://.") from exc

    hostname = urlparse(url).hostname
    if not hostname:
        raise UnsafeUrlError("Podaj poprawny adres zaczynający się od http:// lub https://.")

    # Adres podany wprost jako IP sprawdzamy bez odpytywania DNS.
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if _is_blocked_ip(hostname):
            raise UnsafeUrlError("Adresy w sieci lokalnej nie podlegają audytowi.")
        return url

    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise UnsafeUrlError("Nie udało się rozwiązać nazwy domeny - sprawdź poprawność adresu.") from exc

    # Domena może wskazywać na wiele adresów (A/AAAA) - blokujemy, gdy KTÓRYKOLWIEK
    # z nich jest prywatny (klasyczny "DNS rebinding" na jeden z rekordów).
    if any(_is_blocked_ip(address) for address in resolved):
        raise UnsafeUrlError("Adresy w sieci lokalnej nie podlegają audytowi.")

    return url


def is_public_url(raw_url: str) -> bool:
    """Wygodny predykat dla miejsc, które nie potrzebują komunikatu błędu."""
    try:
        validate_public_url(raw_url)
    except UnsafeUrlError:
        return False
    return True
