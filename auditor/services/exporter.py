"""Budowa struktury raportu eksportowego - wspólna dla XLSX, CSV i Google Sheets.

Moduł NIE zna docelowego formatu: zwraca czyste dane (nagłówki + wiersze), a formatem
zajmują się warstwy wyżej (`auditor.views.export_report`, `auditor.services.sheets`).
Dzięki temu trzy sposoby eksportu opisują dokładnie ten sam raport i nie rozjeżdżają
się przy każdej zmianie w metrykach.

Raport ma trzy zakładki:
  1. "Ruch i Widoczność" - dane Search Console i GA4 (kliknięcia, wyświetlenia, CTR, pozycja),
  2. "Matryca Techniczna Szablonów" - wyniki wszystkich testów per szablon podstrony,
  3. "Priorytetowy Backlog Zadań" - błędy i ostrzeżenia posortowane wg wagi problemu.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from auditor.models import Audit, AuditedPage, AuditMetric
from auditor.presentation import (
    CATEGORY_LABELS,
    OFFICIAL_TEST_NAMES,
    TEAM_BY_CATEGORY,
    annotate_metric_labels,
    priority_for_metric,
)

# Wartość wstawiana tam, gdzie dane nie są dostępne (np. audyt sprzed rozszerzenia
# metryk GSC o CTR) - pusta komórka sugerowałaby zero, a to nieprawda.
NO_DATA = "—"

STATUS_LABELS = {
    "error": "BŁĄD",
    "warning": "OSTRZEŻENIE",
    "ok": "OK",
    "info": "OPCJONALNE",
}


@dataclass
class Sheet:
    """Pojedyncza zakładka raportu: tytuł, nagłówki kolumn i wiersze danych."""

    title: str
    headers: list[str]
    rows: list[list[Any]] = field(default_factory=list)

    def as_table(self) -> list[list[Any]]:
        """Zakładka jako jedna tabela (nagłówek + dane) - do zapisu w Sheets/CSV."""
        return [self.headers, *self.rows]


def build_report(audit: Audit) -> list[Sheet]:
    """Buduje komplet zakładek raportu dla wskazanego audytu."""
    metrics = annotate_metric_labels(list(audit.metrics.all()))
    pages = list(audit.pages.all())
    return [
        build_traffic_sheet(audit),
        build_template_matrix_sheet(audit, pages, metrics),
        build_backlog_sheet(metrics),
    ]


# ----------------------------------------------------------------------
# Zakładka 1: Ruch i Widoczność
# ----------------------------------------------------------------------

def build_traffic_sheet(audit: Audit) -> Sheet:
    """Dane Search Console i GA4: podsumowanie witryny plus rozbicie na frazy i podstrony.

    Wyświetlenia, CTR i średnia pozycja pochodzą z wierszy Search Console zapisanych przy
    audycie. Audyty wykonane przed rozszerzeniem tych danych mają w tych kolumnach
    `NO_DATA` - wtedy wystarczy ponownie odświeżyć dane GSC.
    """
    sheet = Sheet(
        title="Ruch i Widoczność",
        headers=["Sekcja", "Pozycja", "Kliknięcia (teraz)", "Kliknięcia (rok temu)",
                 "Zmiana", "Wyświetlenia", "CTR %", "Śr. pozycja"],
    )

    sheet.rows.append([
        "Podsumowanie witryny", audit.url,
        audit.gsc_total_clicks_current, audit.gsc_total_clicks_previous,
        f"{audit.gsc_yoy_change_percent:+.1f}% R/R", NO_DATA, NO_DATA, NO_DATA,
    ])
    sheet.rows.append([
        "Ruch organiczny GA4", "Sesje organiczne (zapisany okres)",
        audit.ga4_organic_sessions, NO_DATA, NO_DATA, NO_DATA, NO_DATA, NO_DATA,
    ])
    sheet.rows.append([
        "Widoczność Senuto", "Frazy w TOP 3 / TOP 10 / TOP 50",
        f"{audit.senuto_top3} / {audit.senuto_top10} / {audit.senuto_top50}",
        NO_DATA, NO_DATA, NO_DATA, NO_DATA, NO_DATA,
    ])

    for section, rows, key in (
        ("Frazy - wzrosty", audit.gsc_top_gainers, "query"),
        ("Frazy - spadki", audit.gsc_top_losers, "query"),
        ("Podstrony - wzrosty", audit.gsc_top_page_gainers, "page"),
        ("Podstrony - spadki", audit.gsc_top_page_losers, "page"),
    ):
        for row in rows or []:
            sheet.rows.append(_traffic_row(section, row, key))

    return sheet


def _traffic_row(section: str, row: dict, key_name: str) -> list[Any]:
    delta = row.get("delta", 0)
    return [
        section,
        row.get(key_name, NO_DATA),
        row.get("clicks_current", 0),
        row.get("clicks_previous", 0),
        f"{delta:+d}",
        row.get("impressions_current", NO_DATA),
        row.get("ctr_current", NO_DATA),
        row.get("position_current", NO_DATA),
    ]


# ----------------------------------------------------------------------
# Zakładka 2: Matryca Techniczna Szablonów
# ----------------------------------------------------------------------

def build_template_matrix_sheet(
    audit: Audit, pages: list[AuditedPage], metrics: list[AuditMetric]
) -> Sheet:
    """Pełna tabela wyników wszystkich testów w rozbiciu na szablony podstron.

    Kolumna "Rekomendacja AI" dla podstron innych niż główna korzysta z rekomendacji
    wygenerowanej dla tego samego klucza metryki w audycie głównym - skan szablonów
    celowo nie odpytuje modelu osobno dla każdej podstrony (patrz
    `AuditService._scan_single_page`), a problemy szablonowe i tak się powtarzają.
    """
    sheet = Sheet(
        title="Matryca Techniczna Szablonów",
        headers=["Typ Szablonu", "Adres URL", "Kategoria", "Metryka", "Status",
                 "Diagnoza", "Rekomendacja AI"],
    )

    recommendations = _recommendations_by_key(metrics)

    if not pages:
        # Audyty sprzed wprowadzenia wielu szablonów nie mają rekordów AuditedPage -
        # raportujemy wtedy metryki adresu głównego, żeby eksport nie był pusty.
        for metric in metrics:
            sheet.rows.append(_matrix_row(
                AuditedPage.PageType.HOMEPAGE.label, audit.url,
                metric.category, metric.short_key, metric.status,
                (metric.value or {}).get("note", ""),
                (metric.value or {}).get("recommendation", ""),
            ))
        return sheet

    for page in pages:
        if page.status == AuditedPage.Status.FAILED:
            sheet.rows.append([
                page.get_page_type_display(), page.url, NO_DATA, NO_DATA,
                STATUS_LABELS["error"], page.error_message or "Nie udało się przeskanować podstrony.", "",
            ])
            continue

        for metric in page.metrics_data or []:
            key = _short_key(metric.get("key", ""))
            value = metric.get("value") or {}
            sheet.rows.append(_matrix_row(
                page.get_page_type_display(), page.url,
                metric.get("category", ""), key, metric.get("status", ""),
                value.get("note", ""),
                value.get("recommendation") or recommendations.get(key, ""),
            ))

    return sheet


def _matrix_row(
    page_type: str, url: str, category: str, key: str, status: str,
    note: str, recommendation: str,
) -> list[Any]:
    return [
        page_type,
        url,
        CATEGORY_LABELS.get(category, category),
        OFFICIAL_TEST_NAMES.get(key, key.replace("_", " ")),
        STATUS_LABELS.get(status, status),
        note,
        recommendation,
    ]


def _recommendations_by_key(metrics: list[AuditMetric]) -> dict[str, str]:
    """Mapa: klucz metryki -> rekomendacja AI z audytu głównego."""
    return {
        metric.short_key: (metric.value or {}).get("recommendation", "")
        for metric in metrics
        if (metric.value or {}).get("recommendation")
    }


def _short_key(key: str) -> str:
    """Klucz metryki bez przedrostka strategii PageSpeed ("mobile_lcp" -> "lcp")."""
    for prefix in ("mobile_", "desktop_"):
        if key.startswith(prefix):
            return key[len(prefix):]
    return key


# ----------------------------------------------------------------------
# Zakładka 3: Priorytetowy Backlog Zadań
# ----------------------------------------------------------------------

def build_backlog_sheet(metrics: list[AuditMetric]) -> Sheet:
    """Błędy i ostrzeżenia posortowane wg wagi problemu (`presentation.priority_for_metric`).

    Kolejność i skala priorytetu (1-10) są te same, co w drukowanym raporcie PDF -
    backlog w arkuszu i w PDF muszą wskazywać identyczną kolejność wdrożenia.
    """
    sheet = Sheet(
        title="Priorytetowy Backlog Zadań",
        headers=["Priorytet", "Zespół", "Kategoria", "Metryka", "Status", "Diagnoza", "Rekomendacja AI"],
    )

    findings = [m for m in metrics if m.status in ("error", "warning")]
    findings.sort(key=lambda m: (-priority_for_metric(m), m.status != "error", m.short_key))

    for metric in findings:
        value = metric.value or {}
        sheet.rows.append([
            priority_for_metric(metric),
            TEAM_BY_CATEGORY.get(metric.category, "IT"),
            CATEGORY_LABELS.get(metric.category, metric.category),
            OFFICIAL_TEST_NAMES.get(metric.short_key, metric.display_key),
            STATUS_LABELS.get(metric.status, metric.status),
            value.get("note", ""),
            value.get("recommendation", ""),
        ])

    if not sheet.rows:
        sheet.rows.append(["—", "—", "—", "Brak wykrytych błędów i ostrzeżeń", "OK", "", ""])

    return sheet


def report_filename(audit: Audit, extension: str) -> str:
    """Nazwa pliku raportu: domena audytu + data, bez znaków problematycznych w systemie plików."""
    from urllib.parse import urlparse

    domain = (urlparse(audit.url).netloc or "audyt").replace("www.", "").replace(":", "-")
    safe_domain = "".join(ch if ch.isalnum() or ch in "-." else "-" for ch in domain)
    return f"audyt-seo-{safe_domain}-{audit.created_at:%Y-%m-%d}.{extension}"
