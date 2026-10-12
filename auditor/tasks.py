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

from celery import chord, group, shared_task
from celery.exceptions import MaxRetriesExceededError
from django.conf import settings
from django.db import OperationalError, close_old_connections
from django.utils import timezone

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


# ----------------------------------------------------------------------
# Badanie widoczności w wyszukiwarkach AI (GEO)
# ----------------------------------------------------------------------
@shared_task(name="auditor.run_geo_study")
def run_geo_study_task(study_id: int) -> None:
    _run_geo_study_now(study_id)


def _run_geo_study_now(study_id: int) -> None:
    """Wykonuje badanie GEO. Każde wyjście musi zamknąć rekord statusem końcowym -
    inaczej pasek postępu w interfejsie kręciłby się w nieskończoność."""
    from auditor.models import GeoStudy
    from auditor.services.geo import run_study

    try:
        study = GeoStudy.objects.get(pk=study_id)
    except GeoStudy.DoesNotExist:
        logger.warning("Badanie GEO %s nie istnieje - pomijam.", study_id)
        return

    try:
        run_study(study)
    except Exception:
        logger.exception("Badanie GEO %s zakończyło się wyjątkiem.", study_id)
        GeoStudy.objects.filter(pk=study_id, status=GeoStudy.Status.PROCESSING).update(
            status=GeoStudy.Status.FAILED
        )


def enqueue_geo_study(study_id: int) -> str:
    """Zleca badanie GEO w tle - tą samą ścieżką co audyt (Celery, wątek, eager)."""
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        _run_geo_study_now(study_id)
        return "eager"

    try:
        run_geo_study_task.delay(study_id)
        return "celery"
    except Exception as exc:
        logger.warning(
            "Nie udało się zlecić badania GEO %s do Celery (%s) - wykonuję w wątku tła.",
            study_id, type(exc).__name__,
        )
        threading.Thread(target=_run_geo_study_now, args=(study_id,), daemon=True).start()
        return "thread"


# ----------------------------------------------------------------------
# Badanie multi-agentowe (SEO Investigator) - trzech agentów równolegle
#
# Agenci dziedzinowi nie zależą od siebie: każdy czyta własne źródło i zapisuje
# własne wnioski. Sekwencyjnie badanie trwa tyle, ile suma trzech odczytów plus
# wywołanie modelu; równolegle - tyle, ile najdłuższy odczyt plus to wywołanie.
#
# Przepływ to `chord`: `group` trzech zadań zbierających i jedno zadanie syntezy
# w roli callbacku. Workerzy nie dzielą pamięci, więc każdy dostaje CZYSTY
# `SEOInvestigatorState` i oddaje go zserializowanym (`as_dict()`); scalenie robi
# `merge_states` po stronie callbacku.
#
# UWAGA: `chord` wymaga skonfigurowanego backendu wyników (CELERY_RESULT_BACKEND).
# Bez niego Celery nie ma gdzie odłożyć wyników grupy i odmawia uruchomienia -
# dlatego `enqueue_investigation` sprawdza to wprost i w razie braku schodzi
# na ścieżkę sekwencyjną, zamiast wywalać się na oczach użytkownika.
# ----------------------------------------------------------------------

# Ile razy ponawiamy zadanie zbierające. Ponowienie ma sens tylko dla awarii
# przemijających - patrz RETRYABLE_ERRORS.
INVESTIGATION_MAX_RETRIES = 3
INVESTIGATION_RETRY_DELAY_SECONDS = 5

# Wyjątki, przy których ponowienie ma sens. `OperationalError` jest tu
# najważniejszy: przy równoległych workerach i SQLite odczyt bądź zapis potrafi
# zwrócić "database is locked", co po chwili samo mija. Awarie samych integracji
# (GA4, Search Console, scraper) NIE trafiają tutaj - agenci łapią je u siebie
# i zapisują w `state.errors`, bo badanie z dwóch źródeł jest użyteczne,
# a czekanie na trzecie opóźniałoby raport bez pewności, że pomoże.
RETRYABLE_ERRORS = (OperationalError, ConnectionError, TimeoutError)


def _resolve_owner(owner_id: int | None):
    """Właściciel badania - albo None, gdy zadanie dostało badanie bez konta.

    Izolacja danych opiera się na tym obiekcie: agenci filtrują po właścicielu, więc
    nieistniejące konto musi dać None (brak danych), a nie wyjątek.
    """
    if owner_id is None:
        return None

    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=owner_id).first()


def _record_progress(run_id: int | None, sources: dict) -> None:
    """Dokłada źródła tego agenta do postępu badania.

    Trzy zadania zapisują tu równolegle, więc odczyt-modyfikacja-zapis musi iść pod
    blokadą wiersza - bez niej ostatni zapis skasowałby wpisy pozostałych. Zapis
    postępu NIE MOŻE wywrócić zadania: to informacja dla paska postępu, a nie wynik
    badania, więc jego awarię tylko logujemy.
    """
    if not run_id or not sources:
        return

    from django.db import transaction

    from auditor.models import InvestigationRun

    try:
        with transaction.atomic():
            run = InvestigationRun.objects.select_for_update().filter(pk=run_id).first()
            if run is None:
                return
            postep = dict(run.progress or {})
            postep.update(sources)
            InvestigationRun.objects.filter(pk=run_id).update(progress=postep)
    except Exception:
        logger.warning("Nie udało się zapisać postępu badania %s.", run_id, exc_info=True)


def _collect_state(
    task, agent, domain: str, owner_id: int | None, run_id: int | None = None
) -> dict:
    """Uruchamia jednego agenta na czystym stanie i oddaje stan zserializowany.

    Zadanie NIE MOŻE zakończyć się porażką: w akordzie porażka członka grupy
    pomija callback, czyli awaria jednego źródła kasowałaby cały raport. Dlatego
    każda ścieżka wyjścia zwraca słownik - z wnioskami albo z zapisanym błędem.
    """
    from auditor.agents.state import SEOInvestigatorState

    state = SEOInvestigatorState(domain=domain)
    try:
        agent.run(state, owner=_resolve_owner(owner_id))
    except RETRYABLE_ERRORS as exc:
        # Ponawiamy ręcznie, a nie przez `autoretry_for`: tamten mechanizm po
        # wyczerpaniu prób kończy zadanie błędem, czego akord nie wybacza.
        try:
            raise task.retry(
                exc=exc,
                countdown=INVESTIGATION_RETRY_DELAY_SECONDS * (task.request.retries + 1),
            )
        except MaxRetriesExceededError:
            logger.warning(
                "Agent %s dla %s nie wstał po %s próbach - raport powstanie bez niego.",
                getattr(agent, "name", agent), domain, INVESTIGATION_MAX_RETRIES,
            )
            state.record_error(getattr(agent, "name", "Agent"), exc)
    except Exception as exc:  # noqa: BLE001 - awaria jednego źródła nie kasuje raportu
        logger.exception("Agent %s padł dla %s.", getattr(agent, "name", agent), domain)
        state.record_error(getattr(agent, "name", "Agent"), exc)
    finally:
        _record_progress(run_id, state.sources)
        # Worker i wątek tła mają własne połączenia - bez tego zostają otwarte.
        close_old_connections()

    return state.as_dict()


@shared_task(
    name="auditor.investigation.technical",
    bind=True,
    max_retries=INVESTIGATION_MAX_RETRIES,
    soft_time_limit=getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 600),
)
def run_technical_task(
    self, domain: str, owner_id: int | None = None, run_id: int | None = None
) -> dict:
    """Metryki techniczne domeny - z ostatniego audytu albo z lekkiego skanu."""
    from auditor.agents.specialists import TechnicalAgent

    return _collect_state(self, TechnicalAgent(), domain, owner_id, run_id)


@shared_task(
    name="auditor.investigation.analytics",
    bind=True,
    max_retries=INVESTIGATION_MAX_RETRIES,
    soft_time_limit=getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 600),
)
def run_analytics_task(
    self, domain: str, owner_id: int | None = None, run_id: int | None = None
) -> dict:
    """Ruch z Analytics 4 i Search Console - z bazy, a w jej braku z API."""
    from auditor.agents.specialists import AnalyticsAgent

    return _collect_state(self, AnalyticsAgent(), domain, owner_id, run_id)


@shared_task(
    name="auditor.investigation.geo",
    bind=True,
    max_retries=INVESTIGATION_MAX_RETRIES,
    soft_time_limit=getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 600),
)
def run_geo_task(
    self, domain: str, owner_id: int | None = None, run_id: int | None = None
) -> dict:
    """Widoczność marki w wyszukiwarkach AI - z ostatniego badania GEO."""
    from auditor.agents.specialists import GeoAgent

    return _collect_state(self, GeoAgent(), domain, owner_id, run_id)


@shared_task(
    name="auditor.investigation.synthesize",
    bind=True,
    max_retries=0,
    soft_time_limit=getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 600),
)
def synthesize_report_task(self, states_data: list[dict], run_id: int) -> int | None:
    """Callback akordu: scala cząstki, zleca syntezę i zapisuje wynik.

    `max_retries=0` jest celowe: synteza to płatne wywołanie modelu, a przy jego
    awarii agent syntezy sam składa raport z surowych ustaleń. Ponawianie
    płaciłoby drugi raz za to samo.
    """
    from auditor.agents.specialists import ReporterAgent
    from auditor.agents.state import merge_states
    from auditor.models import InvestigationRun

    try:
        state = merge_states(states_data)
        ReporterAgent().run(state)
        return _store_investigation(run_id, state)
    except Exception:
        logger.exception("Synteza badania %s zakończyła się wyjątkiem.", run_id)
        InvestigationRun.objects.filter(pk=run_id).update(
            status=InvestigationRun.Status.FAILED, finished_at=timezone.now()
        )
        return None
    finally:
        close_old_connections()


def _store_investigation(run_id: int, state) -> int | None:
    """Zapisuje raport i pełny stan na rekordzie badania."""
    from auditor.models import InvestigationRun

    zaktualizowane = InvestigationRun.objects.filter(pk=run_id).update(
        status=InvestigationRun.Status.COMPLETED,
        report=state.final_synthesis_report,
        state=state.as_dict(),
        finished_at=timezone.now(),
    )
    if not zaktualizowane:
        logger.warning("Badanie %s nie istnieje - wynik nie ma gdzie trafić.", run_id)
        return None
    return run_id


def _run_investigation_now(domain: str, owner_id: int | None, run_id: int) -> None:
    """Sekwencyjne wykonanie całego badania - ścieżka awaryjna bez brokera.

    Ten sam przepływ co w akordzie, tylko po kolei i w jednym procesie. Korzysta
    z `run_seo_investigation`, żeby obie ścieżki dawały identyczny wynik.
    """
    from auditor.agents.orchestrator import run_seo_investigation
    from auditor.models import InvestigationRun

    try:
        state = run_seo_investigation(domain, owner=_resolve_owner(owner_id))
        _store_investigation(run_id, state)
    except Exception:
        logger.exception("Badanie %s zakończyło się wyjątkiem.", run_id)
        InvestigationRun.objects.filter(pk=run_id).update(
            status=InvestigationRun.Status.FAILED, finished_at=timezone.now()
        )
    finally:
        close_old_connections()


def _chord_available() -> bool:
    """Czy akord ma gdzie odkładać wyniki grupy.

    `chord` bez backendu wyników kończy się wyjątkiem w momencie zlecenia, więc
    sprawdzamy to zanim obiecamy użytkownikowi równoległe badanie.
    """
    return bool(getattr(settings, "CELERY_RESULT_BACKEND", ""))


def enqueue_investigation(
    domain: str, owner_id: int | None = None, audit_id: int | None = None
) -> tuple[int, str]:
    """Zleca badanie domeny. Zwraca `(id rekordu, użyta ścieżka)`.

    Ścieżki, w kolejności preferencji - te same trzy co przy audycie:
    "chord" (równolegle w Celery), "eager" (synchronicznie, w testach),
    "thread" (sekwencyjnie w wątku tła, gdy brokera lub backendu nie ma).

    `audit_id` zapisuje się tylko jako informacja o pochodzeniu zlecenia - agenci
    i tak szukają danych po domenie.
    """
    from auditor.models import InvestigationRun

    run = InvestigationRun.objects.create(
        domain=domain,
        owner_id=owner_id,
        audit_id=audit_id,
        status=InvestigationRun.Status.PROCESSING,
    )

    # Tryb eager wykonuje akord w miejscu, bez brokera i bez backendu wyników -
    # dlatego testy idą tą samą drogą co produkcja, a nie obok niej.
    eager = bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False))

    if not eager and not _chord_available():
        logger.warning(
            "CELERY_RESULT_BACKEND jest pusty - akord nie ma gdzie odłożyć wyników "
            "grupy. Badanie %s wykonuję sekwencyjnie w wątku tła. Aby zrównoleglić, "
            "ustaw CELERY_RESULT_BACKEND (np. redis://127.0.0.1:6379/1).",
            run.pk,
        )
        _run_in_background(domain, owner_id, run.pk)
        return run.pk, "thread"

    try:
        chord(
            group(
                run_technical_task.s(domain, owner_id, run.pk),
                run_analytics_task.s(domain, owner_id, run.pk),
                run_geo_task.s(domain, owner_id, run.pk),
            )
        )(synthesize_report_task.s(run.pk))
        return run.pk, "eager" if eager else "chord"
    except Exception as exc:
        # Najczęstsza przyczyna: broker nie działa. To nie powód, żeby użytkownik
        # nie dostał raportu - schodzimy na wykonanie sekwencyjne.
        logger.warning(
            "Nie udało się zlecić badania %s do Celery (%s) - wykonuję w wątku tła.",
            run.pk, type(exc).__name__,
        )
        _run_in_background(domain, owner_id, run.pk)
        return run.pk, "thread"


def _run_in_background(domain: str, owner_id: int | None, run_id: int) -> None:
    threading.Thread(
        target=_run_investigation_now,
        args=(domain, owner_id, run_id),
        name=f"investigation-{run_id}",
        daemon=True,
    ).start()
