from __future__ import annotations

ORGANIC_CHANNEL = "Organic Search"
PAID_CHANNEL = "Paid Search"
DIRECT_CHANNEL = "Direct"

# Próg wzrostu ruchu płatnego (%) rok-do-roku, powyżej którego - przy jednoczesnym
# spadku ruchu organicznego - zgłaszamy ostrzeżenie o potencjalnym przesunięciu
# budżetu z SEO na płatne kampanie (kanibalizacja płatna).
PAID_CANNIBALIZATION_THRESHOLD_PCT = 10.0


def _pct_change(previous: float, current: float) -> float | None:
    """Procentowa zmiana `current` względem `previous`, zaokrąglona do 1 miejsca po
    przecinku. Zwraca None, gdy `previous` wynosi 0 - nie ma wtedy sensownej bazy
    procentowej (uniknięcie dzielenia przez zero / sztucznie ogromnych wartości)."""
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def _change_sentence(
    subject: str, change: float, rose_word: str, fell_word: str, suffix: str, flat_word: str = "nie zmienił się"
) -> str:
    """Buduje naturalnie brzmiące zdanie o zmianie - dla zmiany zerowej pomija
    powtórzenie "o 0.0%", żeby nie brzmiało nienaturalnie ("nie zmieniła się o 0.0%")."""
    if change > 0:
        return f"{subject} {rose_word} o {change}% {suffix}."
    if change < 0:
        return f"{subject} {fell_word} o {abs(change)}% {suffix}."
    return f"{subject} {flat_word} {suffix}."


def analyze_channel_trends(
    channel_totals_3m: dict, lead_history: dict | None = None, lead_totals_3m: dict | None = None
) -> dict:
    """Analizuje porównanie ROK-DO-ROKU (ostatnie 3 pełne miesiące vs analogiczne 3
    miesiące rok temu) i wylicza gotowe "Automatyczne Wnioski SEO", plus - opcjonalnie
    - osobną analizę trendu leadów.

    `channel_totals_3m` to wynik `GA4OAuthService.fetch_3m_yoy_summary()["channels"]`:
    {"current": {"Organic Search": int, "Paid Search": int, "Direct": int, ...}, "previous": {...}}
    - sumy sesji per kanał dla obu 3-miesięcznych okresów (NIE 12-miesięczne dane do
    wykresu - te są osobno w `Audit.ga4_channels_history`).

    `lead_history` to opcjonalnie `{"months": [...], "events": [...]}` (wynik
    `GA4OAuthService.fetch_event_conversions()["history"]`) - surowa seria miesięczna
    do narysowania wykresu leadów. `lead_totals_3m` to opcjonalnie
    `{"current": int, "previous": int}` (z `fetch_3m_yoy_summary()["leads"]`) - sumy
    zdarzenia dla obu okresów, do wyliczenia trendu R/R leadów. Analiza leadów jest
    CELOWO odseparowana od `summary_points` kanałów - w `detail.html` trafia do
    własnego, niezależnego boksu pod osobnym wykresem leadów.

    Zwraca słownik zapisywany bezpośrednio w `Audit.ga4_insights`:
    {
        "has_data": bool,
        "organic_change_pct": float | None,       # 3M R/R
        "channel_yoy_changes": {"Organic Search": float, "Paid Search": float, "Direct": float},
        "cannibalization_warning": bool,
        "summary_points": [str, ...],              # wnioski o kanałach ruchu (3M R/R)
        "lead_insights": {                         # None, gdy nie wybrano zdarzenia leadowego
            "has_data": bool,
            "last_month_label": str,
            "last_month_count": int,
            "change_pct": float | None,             # 3M R/R
            "trend": "up" | "down" | "flat" | None,
            "summary_points": [str, ...],
            "history": {"months": [...], "events": [...]},
        } | None,
    }
    """
    current = channel_totals_3m.get("current") or {}
    previous = channel_totals_3m.get("previous") or {}

    summary_points: list[str] = []
    insights: dict = {
        "has_data": bool(current) or bool(previous),
        "organic_change_pct": None,
        "channel_yoy_changes": {},
        "cannibalization_warning": False,
        "summary_points": summary_points,
        "lead_insights": _analyze_lead_trend(lead_history, lead_totals_3m),
    }

    if not insights["has_data"]:
        summary_points.append("Za mało danych GA4, żeby wyliczyć wnioski o trendzie ruchu organicznego (3M R/R).")
        return insights

    # --- 1. Nagłówkowa zmiana ruchu organicznego: ostatnie 3 pełne miesiące vs
    #     analogiczne 3 miesiące rok temu. ---
    organic_change = _pct_change(previous.get(ORGANIC_CHANNEL, 0), current.get(ORGANIC_CHANNEL, 0))
    insights["organic_change_pct"] = organic_change
    if organic_change is not None:
        summary_points.append(
            _change_sentence("Ruch organiczny", organic_change, "wzrósł", "spadł", "rok do roku (ostatnie 3 miesiące)")
        )

    # --- 2. Dynamika rok-do-roku Organic/Paid/Direct - kontekst do wykrycia
    #     przesunięcia budżetu między kanałami płatnymi i organicznymi. ---
    yoy_changes: dict[str, float] = {}
    for name in (ORGANIC_CHANNEL, PAID_CHANNEL, DIRECT_CHANNEL):
        change = _pct_change(previous.get(name, 0), current.get(name, 0))
        if change is not None:
            yoy_changes[name] = change
    insights["channel_yoy_changes"] = yoy_changes

    organic_yoy = yoy_changes.get(ORGANIC_CHANNEL)
    paid_yoy = yoy_changes.get(PAID_CHANNEL)
    if organic_yoy is not None and paid_yoy is not None:
        if organic_yoy < 0 and paid_yoy > PAID_CANNIBALIZATION_THRESHOLD_PCT:
            insights["cannibalization_warning"] = True
            summary_points.append(
                f"Ruch organiczny spada rok do roku ({organic_yoy}%), a jednocześnie ruch płatny (Paid Search) "
                f"rośnie o {paid_yoy}% R/R - to może wskazywać na przesunięcie budżetu z SEO na płatne "
                "kampanie (kanibalizacja płatna) zamiast realnego wzrostu widoczności organicznej."
            )
        elif organic_yoy > 0 and paid_yoy < 0:
            summary_points.append(
                f"Ruch organiczny rośnie rok do roku (+{organic_yoy}%), mimo spadku ruchu płatnego "
                f"({paid_yoy}% R/R) - inwestycja w SEO efektywnie kompensuje ograniczenie budżetu na "
                "płatne kampanie."
            )

    direct_yoy = yoy_changes.get(DIRECT_CHANNEL)
    if direct_yoy is not None:
        summary_points.append(
            _change_sentence("Ruch bezpośredni (Direct)", direct_yoy, "wzrósł", "spadł", "rok do roku")
        )

    if not summary_points:
        summary_points.append("Brak istotnych zmian trendu rok do roku w analizowanym okresie.")

    return insights


def _analyze_lead_trend(lead_history: dict | None, lead_totals_3m: dict | None) -> dict | None:
    """Osobna analiza trendu leadów/konwersji (jeśli użytkownik wybrał zdarzenie do
    śledzenia) - wynik trafia do własnego boksu pod dedykowanym wykresem leadów w
    `detail.html`, niezależnie od wniosków o kanałach ruchu. Trend R/R liczony jest
    z `lead_totals_3m` (sumy 3-miesięczne), a `lead_history` dostarcza wyłącznie
    surową serię miesięczną do wykresu."""
    if not lead_history or not lead_history.get("months"):
        return None

    months = lead_history["months"]
    events = lead_history.get("events", [])
    last_month_label = months[-1] if months else ""
    last_month_count = events[-1] if events else 0

    summary_points = [f"Liczba leadów w ostatnim miesiącu ({last_month_label}) wyniosła {last_month_count}."]
    change = None
    trend = None
    if lead_totals_3m:
        change = _pct_change(lead_totals_3m.get("previous", 0), lead_totals_3m.get("current", 0))
        if change is not None:
            trend = "up" if change > 0 else "down" if change < 0 else "flat"
            summary_points.append(
                _change_sentence("Trend leadów", change, "wzrósł", "spadł", "rok do roku (ostatnie 3 miesiące)")
            )
        else:
            summary_points.append("Za mało danych, żeby wyliczyć trend leadów rok do roku.")

    return {
        "has_data": True,
        "last_month_label": last_month_label,
        "last_month_count": last_month_count,
        "change_pct": change,
        "trend": trend,
        "summary_points": summary_points,
        # Surowa seria miesięczna - potrzebna w detail.html do narysowania osobnego
        # wykresu leadów (json_script + Chart.js), niezależnego od wykresu kanałów.
        "history": {"months": months, "events": events},
    }
