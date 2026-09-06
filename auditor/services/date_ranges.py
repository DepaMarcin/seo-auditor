from __future__ import annotations

import calendar
from datetime import date, timedelta

# ----------------------------------------------------------------------
# Dynamiczny zakres dat dla sekcji GA4/GSC (patrz auditor.views.analytics_data).
# ----------------------------------------------------------------------

# Najdłuższe dopuszczalne okno danych. Search Console przechowuje ok. 16 miesięcy
# historii, a domyślna retencja GA4 to 14 miesięcy - dłuższy zakres i tak zwróciłby
# z API puste okresy, więc odrzucamy go już na wejściu z czytelnym komunikatem.
MAX_RANGE_DAYS = 480

# Najkrótszy sensowny zakres - jeden dzień (start == end) jest dozwolony.
MIN_RANGE_DAYS = 1

# Powyżej tylu dni seria dzienna staje się nieczytelna na wykresie (i niepotrzebnie
# duża w odpowiedzi JSON), więc dane agregujemy miesięcznie - GA4 robi to serwerowo
# wymiarem "yearMonth", bez sumowania po stronie Pythona.
MONTHLY_GRANULARITY_THRESHOLD_DAYS = 92

# Zakresy przycisków szybkiego wyboru w interfejsie (etykieta -> liczba dni wstecz).
QUICK_RANGES = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "12m": 365,
}

_POLISH_MONTH_ABBR = {
    1: "STY", 2: "LUT", 3: "MAR", 4: "KWI", 5: "MAJ", 6: "CZE",
    7: "LIP", 8: "SIE", 9: "WRZ", 10: "PAŹ", 11: "LIS", 12: "GRU",
}


class DateRangeError(ValueError):
    """Podany zakres dat jest nieprawidłowy (zła kolejność, format lub zbyt szeroki)."""


def parse_iso_date(value: str, field_name: str) -> date:
    """Parsuje datę "YYYY-MM-DD" albo podnosi `DateRangeError` z czytelnym komunikatem."""
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise DateRangeError(f"Nieprawidłowy format pola {field_name} - oczekiwano YYYY-MM-DD.") from exc


def validate_range(start: date, end: date, today: date | None = None) -> tuple[date, date]:
    """Sprawdza poprawność zakresu i zwraca go (ewentualnie przycięty do dzisiaj).

    Reguły:
      * data początkowa nie może być późniejsza niż końcowa,
      * zakres nie może przekraczać `MAX_RANGE_DAYS` (okno retencji GA4/GSC),
      * data końcowa z przyszłości jest przycinana do dzisiaj (API i tak nie ma
        takich danych) - to nie błąd użytkownika, tylko naturalna konsekwencja
        wybrania "dzisiaj" w kalendarzu w innej strefie czasowej.
    """
    today = today or date.today()

    if start > end:
        raise DateRangeError("Data początkowa nie może być późniejsza niż data końcowa.")

    if end > today:
        end = today
    if start > end:
        raise DateRangeError("Data początkowa nie może być późniejsza niż dzisiejsza data.")

    span_days = (end - start).days + 1
    if span_days > MAX_RANGE_DAYS:
        raise DateRangeError(
            f"Zakres jest zbyt szeroki ({span_days} dni). Maksymalne okno danych GA4/GSC "
            f"to {MAX_RANGE_DAYS} dni."
        )

    return start, end


def quick_range(key: str, today: date | None = None) -> tuple[date, date]:
    """Zamienia klucz przycisku szybkiego wyboru ("7d"/"30d"/"90d"/"12m") na zakres dat."""
    if key not in QUICK_RANGES:
        raise DateRangeError(f"Nieznany zakres skrótowy: {key!r}.")
    today = today or date.today()
    return today - timedelta(days=QUICK_RANGES[key] - 1), today


def default_range(days: int = 30, today: date | None = None) -> tuple[date, date]:
    """Domyślny zakres pokazywany przy pierwszym wejściu na stronę audytu."""
    today = today or date.today()
    return today - timedelta(days=days - 1), today


def use_monthly_granularity(start: date, end: date) -> bool:
    """Czy dla tego zakresu agregować dane miesięcznie zamiast dziennie."""
    return (end - start).days + 1 > MONTHLY_GRANULARITY_THRESHOLD_DAYS


def previous_year_range(start: date, end: date) -> tuple[date, date]:
    """Ten sam zakres przesunięty dokładnie o rok wstecz - okres porównawczy R/R.

    Uogólnienie `same_months_last_year` na dowolny (niekoniecznie pełnomiesięczny)
    zakres. 29 lutego przesuwamy na 28 lutego, żeby nie wywrócić się na roku
    nieprzestępnym.
    """
    return _shift_year(start), _shift_year(end)


def _shift_year(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        # 29 lutego w roku nieprzestępnym.
        return value.replace(year=value.year - 1, day=28)


def expected_days(start: date, end: date) -> list[str]:
    """Kolejne klucze dzienne "YYYYMMDD" dla całego zakresu (wyrównana oś wykresu).

    Dzięki temu dni, dla których API nie zwróciło wiersza (zero sesji/zdarzeń),
    dostają na wykresie zero zamiast znikać z osi.
    """
    days = []
    current = start
    while current <= end:
        days.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return days


def expected_months_between(start: date, end: date) -> list[str]:
    """Kolejne klucze miesięczne "YYYYMM" obejmujące zakres (wyrównana oś wykresu)."""
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}{month:02d}")
        month += 1
        if month == 13:
            month, year = 1, year + 1
    return months


def format_day_label(raw_day: str) -> str:
    """Klucz GA4 "YYYYMMDD" -> etykieta ISO "YYYY-MM-DD" na osi wykresu."""
    return f"{raw_day[0:4]}-{raw_day[4:6]}-{raw_day[6:8]}"


def format_month_label(raw_year_month: str) -> str:
    """Klucz GA4 "YYYYMM" -> czytelna polska etykieta, np. "202509" -> "WRZ 2025"."""
    year, month = raw_year_month[:4], int(raw_year_month[4:6])
    return f"{_POLISH_MONTH_ABBR.get(month, raw_year_month[4:6])} {year}"


def format_range_label(start: date, end: date) -> str:
    """Opis zakresu wstawiany do treści wniosków, np. "01.06.2026 - 30.06.2026 (30 dni)"."""
    span = (end - start).days + 1
    return f"{start.strftime('%d.%m.%Y')} - {end.strftime('%d.%m.%Y')} ({span} dni)"


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
