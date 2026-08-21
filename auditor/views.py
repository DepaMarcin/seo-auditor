from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from .models import Audit, AuditMetric
from .services.audit_service import AuditService


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


def _annotate_metric_labels(metrics):
    """Dolicza do każdej metryki `short_key` (klucz bez przedrostka strategii)
    oraz `display_key` (czytelna etykieta do prezentacji w raporcie)."""
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
    return metrics


def _split_performance_metrics(metrics):
    """Rozdziela metryki performance na mobile/desktop (wg przedrostka) i pozostałe."""
    mobile, desktop, other = [], [], []
    for metric in metrics:
        if metric.key.startswith("mobile_"):
            mobile.append(metric)
        elif metric.key.startswith("desktop_"):
            desktop.append(metric)
        else:
            other.append(metric)
    return mobile, desktop, other


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

    critical_errors = [m for m in all_metrics if m.status == AuditMetric.MetricStatus.ERROR]
    warnings = [m for m in all_metrics if m.status == AuditMetric.MetricStatus.WARNING]
    passed_tests = [m for m in all_metrics if m.status == AuditMetric.MetricStatus.OK]

    stats = {
        "errors_count": len(critical_errors),
        "warnings_count": len(warnings),
        "passed_count": len(passed_tests),
        "total_count": len(all_metrics),
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
        },
    )
