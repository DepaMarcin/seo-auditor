from __future__ import annotations

ORGANIC_CHANNEL = "Organic Search"
PAID_CHANNEL = "Paid Search"
DIRECT_CHANNEL = "Direct"

# Próg wzrostu ruchu płatnego (%), powyżej którego - przy jednoczesnym spadku ruchu
# organicznego - zgłaszamy ostrzeżenie o potencjalnej kanibalizacji płatnej.
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


def analyze_channel_trends(channels_history: dict, lead_history: dict | None = None) -> dict:
    """Analizuje 12-miesięczne dane wielokanałowe GA4 (`Audit.ga4_channels_history`,
    wynik `GA4OAuthService.fetch_yearly_channel_data` - jeden punkt na miesiąc) i
    wylicza gotowe wnioski SEO, plus - opcjonalnie - osobną analizę trendu leadów.

    `lead_history` to opcjonalnie `{"months": [...], "events": [...]}` - wynik
    `GA4OAuthService.fetch_event_conversions()["history"]` dla wybranego przez
    użytkownika zdarzenia lead/konwersja (`Audit.ga4_selected_lead_event`). Analiza
    leadów jest CELOWO odseparowana od `summary_points` kanałów - w `detail.html`
    trafia do własnego, niezależnego boksu pod osobnym wykresem leadów.

    Zwraca słownik zapisywany bezpośrednio w `Audit.ga4_insights`:
    {
        "has_data": bool,
        "organic_change_pct": float | None,      # ostatni pełny miesiąc vs poprzedni (m/m)
        "channel_mom_changes": {"Organic Search": float, "Paid Search": float, "Direct": float},
        "cannibalization_warning": bool,
        "summary_points": [str, ...],             # wnioski o kanałach ruchu
        "lead_insights": {                        # None, gdy nie wybrano zdarzenia leadowego
            "has_data": bool,
            "last_month_label": str,
            "last_month_count": int,
            "change_pct": float | None,
            "trend": "up" | "down" | "flat" | None,
            "summary_points": [str, ...],
        } | None,
    }
    """
    months = channels_history.get("months") or []
    channels = channels_history.get("channels") or {}
    organic = channels.get(ORGANIC_CHANNEL, [])
    paid = channels.get(PAID_CHANNEL, [])
    direct = channels.get(DIRECT_CHANNEL, [])

    summary_points: list[str] = []
    insights: dict = {
        "has_data": bool(months) and bool(organic),
        "organic_change_pct": None,
        "channel_mom_changes": {},
        "cannibalization_warning": False,
        "summary_points": summary_points,
        "lead_insights": _analyze_lead_trend(lead_history),
    }

    if not insights["has_data"]:
        summary_points.append("Za mało danych GA4, żeby wyliczyć wnioski o trendzie ruchu organicznego.")
        return insights

    # --- 1. Nagłówkowa zmiana ruchu organicznego: ostatni pełny miesiąc vs poprzedni. ---
    organic_change = _pct_change(_second_last(organic), _last(organic))
    insights["organic_change_pct"] = organic_change
    if organic_change is not None:
        summary_points.append(_change_sentence("Ruch organiczny", organic_change, "wzrósł", "spadł", "m/m"))

    # --- 2. Dynamika miesiąc-do-miesiąca Organic/Paid/Direct - kontekst dla wykrycia
    #     kanibalizacji płatnej. ---
    mom_changes: dict[str, float] = {}
    for name, series in ((ORGANIC_CHANNEL, organic), (PAID_CHANNEL, paid), (DIRECT_CHANNEL, direct)):
        change = _pct_change(_second_last(series), _last(series))
        if change is not None:
            mom_changes[name] = change
    insights["channel_mom_changes"] = mom_changes

    organic_mom = mom_changes.get(ORGANIC_CHANNEL)
    paid_mom = mom_changes.get(PAID_CHANNEL)
    if organic_mom is not None and paid_mom is not None:
        if organic_mom < 0 and paid_mom > PAID_CANNIBALIZATION_THRESHOLD_PCT:
            insights["cannibalization_warning"] = True
            summary_points.append(
                f"Ruch organiczny spada ({organic_mom}% m/m), a jednocześnie ruch płatny (Paid Search) "
                f"rośnie o {paid_mom}% m/m - to może wskazywać na kanibalizację płatną (kampanie płatne "
                "przejmują ruch, który wcześniej trafiał organicznie, zamiast realnie zwiększać zasięg)."
            )

    direct_mom = mom_changes.get(DIRECT_CHANNEL)
    if direct_mom is not None:
        summary_points.append(_change_sentence("Ruch bezpośredni (Direct)", direct_mom, "wzrósł", "spadł", "m/m"))

    if not summary_points:
        summary_points.append("Brak istotnych zmian trendu w analizowanym okresie.")

    return insights


def _analyze_lead_trend(lead_history: dict | None) -> dict | None:
    """Osobna analiza trendu leadów/konwersji (jeśli użytkownik wybrał zdarzenie do
    śledzenia) - wynik trafia do własnego boksu pod dedykowanym wykresem leadów w
    `detail.html`, niezależnie od wniosków o kanałach ruchu."""
    if not lead_history or not lead_history.get("months"):
        return None

    months = lead_history["months"]
    events = lead_history.get("events", [])
    last_month_label = months[-1] if months else ""
    last_month_count = _last(events)
    change = _pct_change(_second_last(events), _last(events))

    summary_points = [f"Liczba leadów w ostatnim miesiącu ({last_month_label}) wyniosła {last_month_count}."]
    trend = None
    if change is not None:
        trend = "up" if change > 0 else "down" if change < 0 else "flat"
        summary_points.append(_change_sentence("Trend leadów", change, "wzrósł", "spadł", "m/m"))
    else:
        summary_points.append("Za mało danych, żeby wyliczyć trend leadów m/m.")

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


def _last(series: list[float]) -> float:
    return series[-1] if series else 0


def _second_last(series: list[float]) -> float:
    return series[-2] if len(series) >= 2 else 0
