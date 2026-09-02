from django.db import models


class Audit(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Oczekujący"
        PROCESSING = "processing", "W trakcie"
        COMPLETED = "completed", "Zakończony"
        FAILED = "failed", "Błąd"

    url = models.URLField(max_length=2048)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    score = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    # Statystyki widoczności domeny z API Senuto (auditor.services.senuto.SenutoService).
    senuto_top3 = models.IntegerField(default=0)
    senuto_top10 = models.IntegerField(default=0)
    senuto_top50 = models.IntegerField(default=0)
    # Ustrukturyzowana historia widoczności do przełączanego wykresu Chart.js:
    # {"dates": [...], "top3": [...], "top10": [...], "top50": [...]}.
    senuto_history = models.JSONField(default=dict, blank=True)

    # Integracja Google Analytics 4 przez OAuth 2.0 (auditor.services.ga4_service.GA4OAuthService).
    ga4_property_id = models.CharField(max_length=50, blank=True, null=True)
    ga4_refresh_token = models.TextField(blank=True, null=True)
    ga4_organic_sessions = models.IntegerField(default=0)
    ga4_history = models.JSONField(default=dict)

    # Analiza trendów wielokanałowych i leadów/konwersji (auditor.services.ga4_insights).
    ga4_selected_lead_event = models.CharField(max_length=100, blank=True, null=True)
    ga4_channels_history = models.JSONField(default=dict)  # Dane 12-miesięczne dla wszystkich kanałów
    ga4_insights = models.JSONField(default=dict)  # Wyliczone wnioski i algorytmy trendu

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.url} ({self.status})"


class AuditMetric(models.Model):
    class MetricStatus(models.TextChoices):
        OK = "ok", "OK"
        WARNING = "warning", "Ostrzeżenie"
        ERROR = "error", "Błąd"

    audit = models.ForeignKey(Audit, on_delete=models.CASCADE, related_name="metrics")
    category = models.CharField(max_length=50)
    key = models.CharField(max_length=100)
    value = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=MetricStatus.choices, default=MetricStatus.OK)
    current_value = models.TextField(
        blank=True,
        default="",
        help_text="Zastany fragment/wartość ze strony powiązana z tą metryką (np. treść <title>, lista URL-i obrazków bez ALT).",
    )

    class Meta:
        ordering = ["category", "key"]

    def __str__(self):
        return f"{self.audit_id} - {self.category}.{self.key} ({self.status})"


class KnowledgeDocument(models.Model):
    title = models.CharField(max_length=255)
    content = models.TextField()
    category = models.CharField(max_length=50)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.title
