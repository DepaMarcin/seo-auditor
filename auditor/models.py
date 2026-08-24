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
