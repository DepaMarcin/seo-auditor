"""Szyfrowanie sekretów przechowywanych w bazie (tokeny odświeżania OAuth Google).

`refresh_token` Google jest długoterminowym poświadczeniem dającym dostęp do Analytics
i Search Console użytkownika - trzymany jawnym tekstem trafiałby do każdej kopii
zapasowej bazy, a przy DEBUG=True także do tracebacków. Szyfrujemy go symetrycznie
(Fernet/AES-128-CBC + HMAC) kluczem z `settings.TOKEN_ENCRYPTION_KEY`.

W środowisku deweloperskim klucz może być pusty - wtedy wartość zapisywana jest jawnie,
z jednorazowym ostrzeżeniem w logach. Na produkcji brak klucza przerywa start aplikacji
(patrz `config.settings`), więc ta ścieżka nigdy tam nie wystąpi.
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger(__name__)

# Prefiks odróżniający wartość zaszyfrowaną od zapisanej jawnie (dane sprzed wdrożenia
# szyfrowania oraz tryb deweloperski bez klucza).
ENCRYPTED_PREFIX = "enc:v1:"

_warned_about_missing_key = False


def _get_cipher() -> Fernet | None:
    key = getattr(settings, "TOKEN_ENCRYPTION_KEY", "") or ""
    if not key:
        global _warned_about_missing_key
        if not _warned_about_missing_key:
            logger.warning(
                "Brak TOKEN_ENCRYPTION_KEY - tokeny OAuth są zapisywane w bazie jawnym "
                "tekstem. Dopuszczalne wyłącznie lokalnie (DEBUG=True)."
            )
            _warned_about_missing_key = True
        return None
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(value: str | None) -> str | None:
    """Szyfruje wartość do postaci "enc:v1:<token>". `None`/pusty string zwraca bez zmian."""
    if not value:
        return value

    cipher = _get_cipher()
    if cipher is None:
        return value

    return ENCRYPTED_PREFIX + cipher.encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str | None:
    """Odszyfrowuje wartość zapisaną przez `encrypt_secret`.

    Wartości bez prefiksu `enc:v1:` (zapisane przed wdrożeniem szyfrowania albo w trybie
    deweloperskim bez klucza) zwracane są bez zmian - dzięki temu wdrożenie nie unieważnia
    istniejących połączeń z Google.
    """
    if not value or not value.startswith(ENCRYPTED_PREFIX):
        return value

    cipher = _get_cipher()
    if cipher is None:
        logger.error("Token jest zaszyfrowany, ale brak TOKEN_ENCRYPTION_KEY - nie da się go odczytać.")
        return None

    try:
        return cipher.decrypt(value[len(ENCRYPTED_PREFIX):].encode()).decode()
    except InvalidToken:
        # Najczęstsza przyczyna: zmieniony/utracony TOKEN_ENCRYPTION_KEY. Zwracamy None,
        # a nie wyjątek - użytkownik zobaczy wtedy prośbę o ponowne połączenie konta.
        logger.error("Nie udało się odszyfrować tokenu OAuth - klucz szyfrujący mógł się zmienić.")
        return None
