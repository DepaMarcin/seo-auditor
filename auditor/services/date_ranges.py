from __future__ import annotations

import calendar
from datetime import date


def last_n_full_months_range(months: int) -> tuple[date, date]:
    """Zwraca (start, end) obejmujący `months` ostatnich PEŁNYCH miesięcy
    kalendarzowych - bieżący, trwający miesiąc jest pomijany celowo (jego niepełne
    dane zaburzałyby porównania rok-do-roku/miesiąc-do-miesiąca, np. 2 dni września
    potraktowane jak cały miesiąc). Używane przez GA4OAuthService i GSCService do
    spójnego wyznaczania okresów porównawczych 3M R/R."""
    today = date.today()
    year, month = today.year, today.month
    month -= 1
    if month == 0:
        month, year = 12, year - 1
    end_year, end_month = year, month

    for _ in range(months - 1):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    start_year, start_month = year, month

    start = date(start_year, start_month, 1)
    _, last_day = calendar.monthrange(end_year, end_month)
    end = date(end_year, end_month, last_day)
    return start, end


def same_months_last_year(start: date, end: date) -> tuple[date, date]:
    """Zwraca te same miesiące kalendarzowe co (start, end), przesunięte dokładnie o
    rok wstecz - granice liczone niezależnie dla start/end (nie przez odjęcie 365
    dni), żeby uniknąć problemów z latami przestępnymi (29 lutego)."""
    start_b = date(start.year - 1, start.month, 1)
    _, last_day = calendar.monthrange(end.year - 1, end.month)
    end_b = date(end.year - 1, end.month, last_day)
    return start_b, end_b


def expected_year_months(count: int) -> list[str]:
    """Zwraca `count` kolejnych kluczy "YYYYMM" (chronologicznie), kończących się na
    ostatnim PEŁNYM miesiącu - gwarantuje pełną, wyrównaną oś N miesięcy nawet gdy w
    którymś miesiącu API nie zwróciło żadnego wiersza (brak sesji/kliknięć/zdarzeń)."""
    today = date.today()
    year, month = today.year, today.month
    month -= 1
    if month == 0:
        month, year = 12, year - 1

    keys = []
    for _ in range(count):
        keys.append(f"{year:04d}{month:02d}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return list(reversed(keys))
