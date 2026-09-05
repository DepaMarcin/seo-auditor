"""Zadania w tle: wykonanie audytu poza cyklem request/response.

Pojedynczy audyt to kilkanaście-kilkadziesiąt sekwencyjnych wywołań sieciowych
(scraping, HEAD po obrazkach, 2x PageSpeed z limitem 60 s, Senuto, po jednym
wywołaniu LLM na każdy wykryty problem). Trzymanie tego w wątku HTTP oznaczało
timeouty proxy i zajęcie workera na kilka minut.

`enqueue_audit()` ma dwie ścieżki:

* **Celery** - właściwa, produkcyjna: zadanie trafia do brokera i wykonuje je worker.
* **wątek tła** - awaryjna dla developmentu (typowo Windows bez uruchomionego Redisa).
  Wybór ścieżki jest jawnie logowany, żeby nie było wątpliwości, co się wydarzyło.
"""
from __future__ import annotations

import logging
import threading

from celery import shared_task
from django.conf import settings
from django.db import close_old_connections

logger = logging.getLogger(__name__)


@shared_task(
    name="auditor.run_audit",
    bind=True,
    max_retries=0,
    soft_time_limit=getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 600),
)
def run_audit_task(self, audit_id: int) -> None:
    """Wykonuje pełny audyt SEO dla wskazanego rekordu `Audit`."""
    _run_audit_now(audit_id)


def _run_audit_now(audit_id: int) -> None:
    """Wspólne ciało zadania - używane zarówno przez Celery, jak i przez wątek awaryjny."""
    from auditor.models import Audit
    from auditor.services.audit_service import AuditService

    try:
        audit = Audit.objects.get(pk=audit_id)
    except Audit.DoesNotExist:
        logger.warning("Audyt %s nie istnieje - pomijam zadanie.", audit_id)
        return

    try:
        AuditService().run_audit(audit)
    except Exception:
        # `run_audit` sam ustawia status FAILED w bloku finally - tutaj wyłącznie logujemy,
        # żeby wyjątek w wątku tła nie zniknął bez śladu.
        logger.exception("Audyt %s zakończył się nieoczekiwanym błędem.", audit_id)
    finally:
        # Wątek tła ma własne połączenie z bazą - bez tego zostaje otwarte na zawsze.
        close_old_connections()


def _run_in_thread(audit_id: int) -> None:
    thread = threading.Thread(
        target=_run_audit_now, args=(audit_id,), name=f"audit-{audit_id}", daemon=True
    )
    thread.start()


def enqueue_audit(audit_id: int) -> str:
    """Zleca wykonanie audytu w tle. Zwraca użytą ścieżkę: "celery" albo "thread"."""
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        # Tryb testowy: wykonanie synchroniczne, bez brokera i bez wątków.
        _run_audit_now(audit_id)
        return "eager"

    try:
        run_audit_task.delay(audit_id)
        return "celery"
    except Exception as exc:
        # Najczęstsza przyczyna: broker (Redis) nie działa. To nie powód, żeby
        # użytkownik nie mógł uruchomić audytu - schodzimy na wątek tła.
        logger.warning(
            "Nie udało się zlecić audytu %s do Celery (%s) - wykonuję w wątku tła. "
            "Na produkcji uruchom brokera i workera: celery -A config worker -l info",
            audit_id, type(exc).__name__,
        )
        _run_in_thread(audit_id)
        return "thread"
