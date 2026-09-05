"""Prosty limiter uruchamiania audytów oparty o cache Django.

Jeden audyt to kilkanaście wywołań PageSpeed/Senuto i po jednym wywołaniu LLM na każdy
wykryty problem - bez limitu pojedynczy skrypt w pętli wyczerpuje budżet OpenAI i dzienną
quotę PSI. Świadomie nie wprowadzamy zależności od `django-ratelimit`: potrzebny jest
jeden licznik w oknie czasowym, a cache aplikacji już jest skonfigurowany.

Uwaga: przy `LocMemCache` licznik jest per-proces. Na produkcji ustaw `REDIS_CACHE_URL`
(patrz `config.settings`), żeby limit obowiązywał wspólnie dla wszystkich workerów.
"""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest


def _client_ip(request: HttpRequest) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _bucket_key(request: HttpRequest, scope: str) -> str:
    """Klucz licznika: per użytkownik (zalogowany) albo per adres IP."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return f"ratelimit:{scope}:user:{user.pk}"
    return f"ratelimit:{scope}:ip:{_client_ip(request)}"


def is_rate_limited(request: HttpRequest, scope: str = "audit") -> bool:
    """Rejestruje próbę i zwraca True, gdy limit został przekroczony.

    Licznik działa w oknie kroczącym o stałym początku: pierwsze żądanie zakłada klucz z
    TTL równym długości okna, kolejne go inkrementują. Po wygaśnięciu klucza limit
    zaczyna się od nowa - to celowe uproszczenie, w zupełności wystarczające do ochrony
    budżetu API (nie jest to mechanizm bezpieczeństwa przed atakiem rozproszonym).
    """
    limit = getattr(settings, "AUDIT_RATE_LIMIT_COUNT", 10)
    window = getattr(settings, "AUDIT_RATE_LIMIT_WINDOW_SECONDS", 3600)
    if limit <= 0:
        return False

    key = _bucket_key(request, scope)
    # add() zakłada klucz tylko wtedy, gdy jeszcze nie istnieje - to on ustala TTL okna.
    if cache.add(key, 1, window):
        return False

    try:
        current = cache.incr(key)
    except ValueError:
        # Klucz wygasł pomiędzy add() a incr() - traktujemy jako początek nowego okna.
        cache.set(key, 1, window)
        return False

    return current > limit
