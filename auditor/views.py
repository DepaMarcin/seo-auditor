import re

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import Audit, AuditMetric
from .services.audit_service import SCORE_WEIGHTS, AuditService


def index(request):
    if request.method == "POST":
        url = request.POST.get("url", "").strip()
        if not url:
            messages.error(request, "Podaj adres URL do audytu.")
            return redirect("auditor:index")

        audit = Audit.objects.create(url=url)
        AuditService().run_audit(audit)
        return redirect("auditor:detail", pk=audit.pk)

    audits = Audit.objects.all()
    return render(request, "auditor/index.html", {"audits": audits})


CORE_WEB_VITALS_KEYS = {"pagespeed_score", "lcp", "cls", "fcp", "inp"}
STRATEGY_LABELS = {"mobile": "📱 ", "desktop": "🖥️ "}

# Czytelne etykiety kategorii - używane w tabeli "Problemy i rekomendacje" oraz w PDF.
CATEGORY_LABELS = {
    "seo": "Meta Tagi",
    "technical": "SEO Techniczne",
    "performance": "Szybkość strony",
    "structure": "Schema.org / GEO",
}

# Etykiety i kolejność kategorii na paskach postępu w panelu "Przegląd".
OVERVIEW_CATEGORY_ORDER = [
    ("seo", "SEO On-Page"),
    ("technical", "SEO Techniczne"),
    ("performance", "Szybkość i Wydajność"),
    ("structure", "Dane Strukturalne & LLM"),
]

# Wyodrębnia bloki kodu ```...``` (opcjonalnie z nazwą języka) z tekstu rekomendacji AI.
_CODE_FENCE_RE = re.compile(r"```[a-zA-Z]*\n?(.*?)```", re.DOTALL)


def _split_recommendation_segments(recommendation):
    """Dzieli tekst rekomendacji AI na segmenty tekstowe i bloki kodu (```...```),
    żeby móc wyrenderować kod w podświetlanej ramce z przyciskiem "Kopiuj"."""
    if not recommendation:
        return []

    segments = []
    last_end = 0
    for match in _CODE_FENCE_RE.finditer(recommendation):
        if match.start() > last_end:
            text = recommendation[last_end:match.start()].strip()
            if text:
                segments.append({"type": "text", "content": text})
        code = match.group(1).strip()
        if code:
            segments.append({"type": "code", "content": code})
        last_end = match.end()

    remaining = recommendation[last_end:].strip()
    if remaining:
        segments.append({"type": "text", "content": remaining})

    if not segments and recommendation.strip():
        segments.append({"type": "text", "content": recommendation.strip()})

    return segments


def _annotate_metric_labels(metrics):
    """Dolicza do każdej metryki `short_key` (klucz bez przedrostka strategii),
    `display_key` (czytelna etykieta), `category_label` oraz `recommendation_segments`
    (rekomendacja AI podzielona na tekst/bloki kodu do prezentacji w raporcie)."""
    for metric in metrics:
        short_key = metric.key
        strategy_emoji = ""
        for strategy, emoji in STRATEGY_LABELS.items():
            prefix = f"{strategy}_"
            if metric.key.startswith(prefix):
                short_key = metric.key[len(prefix):]
                strategy_emoji = emoji
                break
        metric.short_key = short_key
        label = metric.value.get("label") if isinstance(metric.value, dict) else None
        metric.display_key = f"{strategy_emoji}{label or short_key.replace('_', ' ')}"
        metric.category_label = CATEGORY_LABELS.get(metric.category, metric.category)

        recommendation = metric.value.get("recommendation") if isinstance(metric.value, dict) else None
        metric.recommendation_segments = _split_recommendation_segments(recommendation)
    return metrics


# Metryki score per urządzenie zastąpione są w podsumowaniu (Priorytety/Ostrzeżenia)
# jedną zbiorczą metryką "pagespeed_score" - nie pokazujemy ich tam osobno.
MERGED_PAGESPEED_SCORE_KEYS = {"mobile_pagespeed_score", "desktop_pagespeed_score"}


def _split_performance_metrics(metrics):
    """Rozdziela metryki performance na mobile/desktop (wg przedrostka) i pozostałe.

    Zbiorcza metryka "pagespeed_score" (bez przedrostka) jest pomijana tutaj celowo -
    jest prezentowana wyłącznie w sekcjach Priorytety/Ostrzeżenia/Zdane testy, żeby nie
    powielać jej obok pełnych kafelków Mobile/Desktop w sekcji szczegółowej.
    """
    mobile, desktop, other = [], [], []
    for metric in metrics:
        if metric.key == "pagespeed_score":
            continue
        if metric.key.startswith("mobile_"):
            mobile.append(metric)
        elif metric.key.startswith("desktop_"):
            desktop.append(metric)
        else:
            other.append(metric)
    return mobile, desktop, other


def _compute_category_scores(audit):
    """Liczy % wyniku dla każdej z 4 kategorii (do pasków postępu w panelu "Przegląd"),
    tą samą metodą co ogólny wynik audytu (średnia ważona statusów ok/warning/error)."""
    results = []
    for category, label in OVERVIEW_CATEGORY_ORDER:
        category_metrics = list(audit.metrics.filter(category=category))
        if category_metrics:
            total = sum(SCORE_WEIGHTS[m.status] for m in category_metrics)
            score = round(total / len(category_metrics))
        else:
            score = 0
        results.append({"label": label, "score": score})
    return results


def _score_bucket(score):
    if score >= 80:
        return "ok"
    if score >= 50:
        return "warning"
    return "error"


def audit_detail(request, pk):
    audit = get_object_or_404(Audit, pk=pk)

    all_metrics = _annotate_metric_labels(list(audit.metrics.all()))

    metrics_by_category = {}
    for metric in all_metrics:
        metrics_by_category.setdefault(metric.category, []).append(metric)

    performance_metrics = metrics_by_category.pop("performance", [])
    structure_metrics = metrics_by_category.pop("structure", [])
    mobile_metrics, desktop_metrics, other_performance_metrics = _split_performance_metrics(
        performance_metrics
    )

    mobile_score = next(
        (m.value.get("value") for m in mobile_metrics if m.short_key == "pagespeed_score"), None
    )
    desktop_score = next(
        (m.value.get("value") for m in desktop_metrics if m.short_key == "pagespeed_score"), None
    )

    # W podsumowaniu (Priorytety/Ostrzeżenia/Zdane testy) pomijamy osobne metryki
    # score per urządzenie - reprezentuje je tam jedna zbiorcza metryka "pagespeed_score".
    summary_metrics = [m for m in all_metrics if m.key not in MERGED_PAGESPEED_SCORE_KEYS]

    critical_errors = [m for m in summary_metrics if m.status == AuditMetric.MetricStatus.ERROR]
    warnings = [m for m in summary_metrics if m.status == AuditMetric.MetricStatus.WARNING]
    passed_tests = [m for m in summary_metrics if m.status == AuditMetric.MetricStatus.OK]

    stats = {
        "errors_count": len(critical_errors),
        "warnings_count": len(warnings),
        "passed_count": len(passed_tests),
        "total_count": len(summary_metrics),
    }

    return render(
        request,
        "auditor/detail.html",
        {
            "audit": audit,
            "metrics_by_category": metrics_by_category,
            "structure_metrics": structure_metrics,
            "mobile_metrics": mobile_metrics,
            "desktop_metrics": desktop_metrics,
            "other_performance_metrics": other_performance_metrics,
            "mobile_score": mobile_score,
            "desktop_score": desktop_score,
            "cwv_keys": CORE_WEB_VITALS_KEYS,
            "critical_errors": critical_errors,
            "warnings": warnings,
            "passed_tests": passed_tests,
            "stats": stats,
            "category_scores": _compute_category_scores(audit) if audit.status == "completed" else [],
            "score_bucket": _score_bucket(audit.score),
        },
    )


# ----------------------------------------------------------------------
# Generator drukowalnego raportu PDF (auditor:download_pdf_report)
# ----------------------------------------------------------------------

# Zespół odpowiedzialny za wdrożenie poprawki, wg kategorii metryki.
TEAM_BY_CATEGORY = {
    "seo": "SEO",
    "technical": "IT",
    "performance": "IT",
    "structure": "IT",
}

# Bazowy priorytet (1-10) wg statusu oraz dodatkowy "boost" wg kategorii -
# wydajność i dane strukturalne mają największy wpływ na widoczność w Google/LLM,
# więc przy tym samym statusie trafiają wyżej na liście wdrożeniowej.
PRIORITY_BASE_BY_STATUS = {
    AuditMetric.MetricStatus.ERROR: 7,
    AuditMetric.MetricStatus.WARNING: 4,
}
PRIORITY_BOOST_BY_CATEGORY = {"performance": 3, "structure": 2, "seo": 1, "technical": 1}


def _priority_for_metric(metric):
    base = PRIORITY_BASE_BY_STATUS.get(metric.status, 3)
    boost = PRIORITY_BOOST_BY_CATEGORY.get(metric.category, 0)
    return min(10, base + boost)


def download_pdf_report(request, audit_id):
    """Generuje drukowalny (HTML -> Zapisz jako PDF w przeglądarce) raport z audytu,
    w formalnym układzie agencyjnym: okładka, spis treści, tabela priorytetów
    wdrożeniowych oraz rozdziały tematyczne."""
    audit = get_object_or_404(Audit, pk=audit_id)

    if audit.status != Audit.Status.COMPLETED:
        messages.error(request, "Raport PDF jest dostępny wyłącznie dla zakończonych audytów.")
        return redirect("auditor:detail", pk=audit.pk)

    all_metrics = _annotate_metric_labels(list(audit.metrics.all()))
    summary_metrics = [m for m in all_metrics if m.key not in MERGED_PAGESPEED_SCORE_KEYS]

    priority_findings = [
        m for m in summary_metrics
        if m.status in (AuditMetric.MetricStatus.ERROR, AuditMetric.MetricStatus.WARNING)
    ]
    for metric in priority_findings:
        metric.priority = _priority_for_metric(metric)
        metric.team = TEAM_BY_CATEGORY.get(metric.category, "IT")
    priority_findings.sort(key=lambda m: m.priority, reverse=True)

    stats = {
        "errors_count": len([m for m in summary_metrics if m.status == AuditMetric.MetricStatus.ERROR]),
        "warnings_count": len([m for m in summary_metrics if m.status == AuditMetric.MetricStatus.WARNING]),
        "passed_count": len([m for m in summary_metrics if m.status == AuditMetric.MetricStatus.OK]),
    }

    return render(
        request,
        "auditor/report_pdf.html",
        {
            "audit": audit,
            "generated_at": timezone.now(),
            "category_scores": _compute_category_scores(audit),
            "score_bucket": _score_bucket(audit.score),
            "stats": stats,
            "priority_findings": priority_findings,
            # Rozdział 1: Analiza Techniczna (technical + performance).
            "technical_metrics": [m for m in all_metrics if m.category in ("technical", "performance")],
            # Rozdział 2: Meta Tagi.
            "meta_tag_metrics": [m for m in all_metrics if m.category == "seo"],
            # Rozdział 3: Dane Strukturalne Schema.org.
            "schema_metrics": [m for m in all_metrics if m.key.startswith("schema_")],
            # Rozdział 4: Widoczność w AI Overviews (GEO) - pozostałe metryki structure.
            "geo_metrics": [
                m for m in all_metrics if m.category == "structure" and not m.key.startswith("schema_")
            ],
        },
    )
