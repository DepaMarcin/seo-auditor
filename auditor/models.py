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
        # Testu NIE DAŁO SIĘ przeprowadzić, bo surowy HTML nie zawierał treści
        # (CSR albo blokada WAF - patrz auditor.services.accessibility). Status
        # celowo odrębny od "error": brak H1 w pustym szkielecie aplikacji nie jest
        # błędem strony, a oznaczanie go jako błąd generuje raport pełen nieprawdy.
        # Metryki w tym stanie są wyłączone z punktacji (patrz SCORED_STATUSES).
        SKIPPED = "skipped", "Nie do zbadania"

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


class AuditedPage(models.Model):
    """Pojedyncza podstrona (szablon) przebadana w ramach jednego audytu.

    Audyt ocenia witrynę na podstawie kilku reprezentatywnych szablonów - strona główna
    rządzi się innymi prawami niż karta produktu czy wpis blogowy, więc jeden adres nie
    opisuje stanu całego serwisu.

    Podział odpowiedzialności względem `AuditMetric`:
      * `AuditMetric` trzyma metryki adresu GŁÓWNEGO i to one składają się na ogólną
        ocenę witryny (`Audit.score`) oraz zasilają cały dotychczasowy interfejs,
      * `AuditedPage.metrics_data` trzyma rozbicie per szablon - listę metryk w tym samym
        formacie co `AuditMetric` (category/key/value/status/current_value), ale jako
        JSON, bo służy do zestawień i eksportu, a nie do zapytań po pojedynczej metryce.
    """

    class PageType(models.TextChoices):
        HOMEPAGE = "homepage", "Strona główna"
        CATEGORY = "category", "Strona kategorii"
        PRODUCT = "product", "Strona produktu"
        BLOG = "blog", "Wpis na blogu"
        OFFER = "offer", "Strona ofertowa"
        OTHER = "other", "Inna podstrona"

    class Status(models.TextChoices):
        PENDING = "pending", "Oczekujący"
        PROCESSING = "processing", "W trakcie"
        COMPLETED = "completed", "Zakończony"
        FAILED = "failed", "Błąd"

    audit = models.ForeignKey(Audit, on_delete=models.CASCADE, related_name="pages")
    url = models.URLField(max_length=2048)
    page_type = models.CharField(max_length=20, choices=PageType.choices, default=PageType.OTHER)
    metrics_data = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    score = models.IntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            # Ten sam adres w jednym audycie nie ma sensu - dublowałby skan i zestawienia.
            models.UniqueConstraint(fields=["audit", "url"], name="unique_page_url_per_audit"),
        ]

    def __str__(self):
        return f"{self.get_page_type_display()}: {self.url}"

    @property
    def status_counts(self) -> dict[str, int]:
        """Zliczenie metryk wg statusu - do kolumn zestawienia szablonów."""
        counts = {"error": 0, "warning": 0, "ok": 0, "info": 0}
        for metric in self.metrics_data or []:
            status = metric.get("status")
            if status in counts:
                counts[status] += 1
        return counts


class KnowledgeDocument(models.Model):
    title = models.CharField(max_length=255)
    content = models.TextField()
    category = models.CharField(max_length=50)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.title


class GeoStudy(models.Model):
    """Pojedyncze badanie widoczności marki w odpowiedziach modeli językowych.

    Badanie zadaje kilka pytań intencyjnych po kilka razy każde i liczy, jak często
    w przypisach odpowiedzi pojawia się domena klienta. Powtórzenia są istotne: modele
    są niedeterministyczne, więc jednorazowe trafienie nie odróżnia stabilnej obecności
    od przypadku - i dopiero rozkład wyników pozwala powiedzieć, czy marka jest
    cytowana regularnie, czy losowo.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Oczekujące"
        PROCESSING = "processing", "W trakcie"
        COMPLETED = "completed", "Zakończone"
        FAILED = "failed", "Błąd"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="geo_studies",
        null=True,
        blank=True,
    )
    # Badanie może powstać w oderwaniu od audytu (sama domena), ale gdy powstaje
    # z poziomu raportu, wiążemy je z nim - dzięki temu historia badań jest widoczna
    # przy audycie, a nie tylko na osobnej liście.
    audit = models.ForeignKey(
        "Audit",
        on_delete=models.SET_NULL,
        related_name="geo_studies",
        null=True,
        blank=True,
    )
    domain = models.CharField(max_length=253)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Średnia częstość cytowań ze wszystkich pytań, 0-100.
    overall_score = models.IntegerField(default=0)
    repetitions = models.PositiveSmallIntegerField(default=5)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Badanie GEO"
        verbose_name_plural = "Badania GEO"

    def __str__(self):
        return f"GEO {self.domain} ({self.status})"

    @property
    def score_bucket(self) -> str:
        """Kolor wskaźnika zbiorczego. Progi jak przy stabilności pojedynczego
        pytania, żeby karta główna i tabela mówiły tym samym językiem."""
        if self.overall_score >= 80:
            return "stable"
        if self.overall_score > 0:
            return "volatile"
        return "absent"

    @property
    def total_runs(self) -> int:
        """Ile zapytań obejmuje całe badanie (pytania x powtórzenia)."""
        return self.queries.count() * self.repetitions

    @property
    def completed_runs(self) -> int:
        from auditor.models import GeoRun

        return GeoRun.objects.filter(query__study=self).count()

    @property
    def progress_percent(self) -> int:
        total = self.total_runs
        return round(self.completed_runs / total * 100) if total else 0


class GeoQuery(models.Model):
    """Jedno pytanie intencyjne wraz z podsumowaniem wyników jego powtórzeń."""

    class Stability(models.TextChoices):
        STABLE = "stable", "STABLE"
        VOLATILE = "volatile", "VOLATILE"
        ABSENT = "absent", "ABSENT"

    study = models.ForeignKey(GeoStudy, on_delete=models.CASCADE, related_name="queries")
    text = models.TextField()
    position = models.PositiveSmallIntegerField(default=1)

    # Odsetek powtórzeń, w których domena klienta pojawiła się w przypisach (0-100).
    citation_rate = models.IntegerField(default=0)
    stability = models.CharField(
        max_length=20, choices=Stability.choices, default=Stability.ABSENT
    )
    # Pozycje domeny klienta na liście przypisów w kolejnych powtórzeniach, np. [1, 3, 2].
    cited_positions = models.JSONField(default=list, blank=True)
    # Domeny konkurencji cytowane, gdy zabrakło domeny klienta: [{"domain": ..., "count": ...}].
    competitors = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["position", "pk"]
        verbose_name = "Pytanie GEO"
        verbose_name_plural = "Pytania GEO"

    def __str__(self):
        return self.text[:80]

    @property
    def cited_runs(self) -> int:
        return sum(1 for run in self.runs.all() if run.brand_cited)

    @property
    def most_common_position(self) -> str:
        """Najczęstsza pozycja w przypisach - kolumna "Pozycje w źródłach"."""
        if not self.cited_positions:
            return "—"
        from collections import Counter

        position, count = Counter(self.cited_positions).most_common(1)[0]
        return f"#{position}" + (f" ({count}x)" if count > 1 else "")


class GeoRun(models.Model):
    """Pojedyncze wywołanie modelu - jedno powtórzenie jednego pytania."""

    query = models.ForeignKey(GeoQuery, on_delete=models.CASCADE, related_name="runs")
    attempt = models.PositiveSmallIntegerField(default=1)
    answer = models.TextField(blank=True, default="")
    # Pełna lista przypisów: [{"url": ..., "domain": ..., "title": ..., "position": n}].
    citations = models.JSONField(default=list, blank=True)
    brand_cited = models.BooleanField(default=False)
    brand_position = models.PositiveSmallIntegerField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["attempt", "pk"]
        verbose_name = "Powtórzenie GEO"
        verbose_name_plural = "Powtórzenia GEO"

    def __str__(self):
        return f"{self.query_id} / próba {self.attempt}"

