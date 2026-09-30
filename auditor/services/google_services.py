"""Przypisanie usług Google do audytu i odświeżenie jego danych.

Jedno miejsce dla obu sytuacji: pierwszego wyboru zaraz po autoryzacji OAuth i
późniejszej zmiany z panelu analityki. Wcześniej były to dwie ścieżki w `views.py`,
które robiły prawie to samo - "prawie", bo tylko jedna czyściła dane poprzedniej
usługi. Wynik zależał więc od tego, którędy użytkownik przyszedł, a raport potrafił
pokazać ruch jednej witryny pod adresem drugiej.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.core.cache import cache

logger = logging.getLogger(__name__)

# Pola wypełniane danymi POBRANYMI z konkretnej usługi GA4. Po zmianie usługi opisują
# już inną witrynę, więc muszą zniknąć razem z nią.
GA4_DERIVED_FIELDS = {
    "ga4_organic_sessions": 0,
    "ga4_history": dict,
    "ga4_channels_history": dict,
    "ga4_insights": dict,
    # Nazwa zdarzenia konwersji pochodzi z konfiguracji poprzedniej usługi i w nowej
    # najczęściej nie istnieje.
    "ga4_selected_lead_event": None,
}

# To samo dla Search Console: komplet liczb i komentarzy dotyczy jednej witryny.
GSC_DERIVED_FIELDS = {
    "gsc_total_clicks_current": 0,
    "gsc_total_clicks_previous": 0,
    "gsc_yoy_change_percent": 0.0,
    "gsc_top_gainers": list,
    "gsc_top_losers": list,
    "gsc_top_page_gainers": list,
    "gsc_top_page_losers": list,
    "gsc_query_commentary": "",
    "gsc_page_commentary": "",
}


@dataclass
class AssignmentResult:
    """Co się wydarzyło przy przypisaniu - widok buduje z tego komunikat."""

    ga4_changed: bool = False
    gsc_changed: bool = False
    fetched: bool = False

    @property
    def changed(self) -> bool:
        return self.ga4_changed or self.gsc_changed


def reset_derived_fields(audit, fields: dict) -> list[str]:
    """Czyści dane pobrane z poprzedniej usługi. Zwraca nazwy zmienionych pól."""
    for nazwa, wartosc in fields.items():
        setattr(audit, nazwa, wartosc() if callable(wartosc) else wartosc)
    return list(fields)


def apply_google_services(audit, property_id: str, site_url: str) -> AssignmentResult:
    """Zapisuje wybór, unieważnia dane poprzedniej usługi i pobiera nowe.

    Kolejność jest istotna: najpierw czyścimy, potem pobieramy. Gdy pobranie się nie
    uda, audyt zostaje z pustymi liczbami - to lepsze niż liczby cudzej witryny pod
    nazwą bieżącej, bo użytkownik nie ma jak tej podmiany zauważyć.
    """
    property_id = (property_id or "").strip()
    site_url = (site_url or "").strip()

    poprzednia_usluga = audit.ga4_property_id or ""
    poprzednia_witryna = audit.gsc_site_url or ""

    wynik = AssignmentResult(
        ga4_changed=property_id != poprzednia_usluga,
        gsc_changed=site_url != poprzednia_witryna,
    )

    # Puste pole to świadomy wybór "brak / dopasuj automatycznie", a nie brak danych.
    audit.ga4_property_id = property_id or None
    audit.gsc_site_url = site_url
    zmienione = ["ga4_property_id", "gsc_site_url"]

    if wynik.ga4_changed:
        zmienione += reset_derived_fields(audit, GA4_DERIVED_FIELDS)
        _invalidate_ga4_cache(audit, poprzednia_usluga)
    if wynik.gsc_changed:
        zmienione += reset_derived_fields(audit, GSC_DERIVED_FIELDS)

    audit.save(update_fields=zmienione)

    if not wynik.changed:
        return wynik

    wynik.fetched = refresh_google_data(audit, property_id)
    return wynik


def _invalidate_ga4_cache(audit, poprzednia_usluga: str) -> None:
    """Usuwa zbuforowane odpowiedzi związane z poprzednią usługą.

    Lista usług jest buforowana pod numerem audytu, a lista zdarzeń pod numerem
    usługi - po zmianie obie opisują już nieistniejący wybór.
    """
    cache.delete(f"ga4_properties:{audit.pk}")
    if poprzednia_usluga:
        cache.delete(f"ga4_events:{poprzednia_usluga}")


def refresh_google_data(audit, property_id: str) -> bool:
    """Pobiera dane GA4 i GSC dla świeżo przypisanych usług.

    Zwraca True, gdy cokolwiek udało się pobrać. Brak tokenu albo awaria Google nie
    jest błędem krytycznym - użytkownik zobaczy ostrzeżenie, a nie stronę błędu.
    """
    from auditor.services.audit_service import AuditService

    if not audit.ga4_refresh_token:
        return False

    try:
        credentials = build_credentials_from_refresh_token(audit)
    except Exception:  # noqa: BLE001 - brak konfiguracji OAuth nie może wywrócić zapisu
        logger.exception("Nie udało się odtworzyć poświadczeń Google dla audytu %s.", audit.pk)
        return False

    service = AuditService()
    pobrano = False

    if property_id:
        try:
            service.sync_ga4_data(audit, credentials, property_id)
            pobrano = True
        except Exception:  # noqa: BLE001
            logger.exception("Nie udało się pobrać danych GA4 dla audytu %s.", audit.pk)

    try:
        service.sync_gsc_data(audit, credentials)
        pobrano = True
    except Exception:  # noqa: BLE001
        logger.exception("Nie udało się pobrać danych Search Console dla audytu %s.", audit.pk)

    return pobrano


def load_google_client_config() -> tuple[str, str]:
    """Odczytuje `client_id`/`client_secret` z pliku `client_secret.json`.

    Bez budowania pełnego obiektu `Flow` - potrzebne wyłącznie do odtworzenia
    `Credentials` z zapisanego wcześniej `refresh_token`.
    """
    import json

    from django.conf import settings

    with open(settings.GA4_CLIENT_SECRETS_FILE, encoding="utf-8") as fh:
        raw_config = json.load(fh)
    config = raw_config.get("web") or raw_config.get("installed") or {}
    return config["client_id"], config["client_secret"]


def build_credentials_from_refresh_token(audit):
    """Odtwarza `Credentials` z tokenu audytu - bez ekranu zgody Google."""
    from auditor.services.ga4_service import GA4OAuthService

    client_id, client_secret = load_google_client_config()
    return GA4OAuthService().build_credentials_from_refresh_token(
        refresh_token=audit.ga4_refresh_token,
        client_id=client_id,
        client_secret=client_secret,
    )
