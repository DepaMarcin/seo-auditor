from django.conf import settings
from django.db import models

from .services.crypto import decrypt_secret, encrypt_secret


class Audit(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Oczekujący"
        PROCESSING = "processing", "W trakcie"
        COMPLETED = "completed", "Zakończony"
        FAILED = "failed", "Błąd"

    # Właściciel audytu - audyt zawiera dane analityczne firmy (GA4, GSC), więc nie
    # może być dostępny dla każdego, kto zna jego identyfikator. `null=True` istnieje
    # wyłącznie ze względu na rekordy sprzed wdrożenia autoryzacji (patrz polecenie
    # `manage.py claim_audits`); nowe audyty zawsze mają właściciela.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="audits",
        null=True,
        blank=True,
    )
    url = models.URLField(max_length=2048)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    score = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Statystyki widoczności domeny z API Senuto (auditor.services.senuto.SenutoService).
    senuto_top3 = models.IntegerField(default=0)
    senuto_top10 = models.IntegerField(default=0)
    senuto_top50 = models.IntegerField(default=0)
    # Ustrukturyzowana historia widoczności do przełączanego wykresu Chart.js:
    # {"dates": [...], "top3": [...], "top10": [...], "top50": [...]}.
    senuto_history = models.JSONField(default=dict, blank=True)

    # Integracja Google Analytics 4 przez OAuth 2.0 (auditor.services.ga4_service.GA4OAuthService).
    ga4_property_id = models.CharField(max_length=50, blank=True, null=True)
    # Token trzymany jest zaszyfrowany (auditor.services.crypto); kod aplikacji nigdy
    # nie sięga do tego pola bezpośrednio - korzysta z właściwości `ga4_refresh_token`.
    ga4_refresh_token_encrypted = models.TextField(blank=True, null=True)
    ga4_organic_sessions = models.IntegerField(default=0)
    ga4_history = models.JSONField(default=dict)

    # Analiza trendów wielokanałowych i leadów/konwersji (auditor.services.ga4_insights).
    ga4_selected_lead_event = models.CharField(max_length=100, blank=True, null=True)
    ga4_channels_history = models.JSONField(default=dict)  # Dane 12-miesięczne dla wszystkich kanałów
    ga4_insights = models.JSONField(default=dict)  # Wyliczone wnioski i algorytmy trendu

    # Analiza fraz kluczowych Google Search Console - 3 miesiące teraz vs 3 miesiące
    # rok temu (auditor.services.gsc_service.GSCService).
    gsc_total_clicks_current = models.IntegerField(default=0)
    gsc_total_clicks_previous = models.IntegerField(default=0)
    gsc_yoy_change_percent = models.FloatField(default=0.0)
    gsc_top_gainers = models.JSONField(default=list)
    gsc_top_losers = models.JSONField(default=list)
    gsc_top_page_gainers = models.JSONField(default=list)
    gsc_top_page_losers = models.JSONField(default=list)
    # Automatyczne komentarze tekstowe PL generowane przez auditor.services.gsc_insights.
    gsc_query_commentary = models.TextField(blank=True, default="")
    gsc_page_commentary = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.url} ({self.status})"

    @property
    def ga4_refresh_token(self) -> str | None:
        """Odszyfrowany token odświeżania Google (albo None, gdy brak/nie da się odczytać)."""
        return decrypt_secret(self.ga4_refresh_token_encrypted)

    @ga4_refresh_token.setter
    def ga4_refresh_token(self, value: str | None) -> None:
        self.ga4_refresh_token_encrypted = encrypt_secret(value)


class AuditMetric(models.Model):
    class MetricStatus(models.TextChoices):
        OK = "ok", "OK"
        WARNING = "warning", "Ostrzeżenie"
        ERROR = "error", "Błąd"
        # Test formalnie "zdany" (nie liczy się jako problem), ale opcjonalny w danym
        # kontekście strony - np. EEAT+ (autor, data aktualizacji) na stronach
        # ofertowych/głównych, gdzie wymóg dotyczy przede wszystkim treści blogowych.
        INFO = "info", "Informacyjne (Opcjonalne)"

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
