from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from .forms import EmailUserChangeForm, EmailUserCreationForm
from .models import Audit, AuditMetric, GeoQuery, GeoRun, GeoStudy, KnowledgeDocument

User = get_user_model()

# Konta logują się adresem e-mail, więc panel zakłada je adresem, a nie nazwą.
admin.site.unregister(User)


@admin.register(User)
class EmailUserAdmin(UserAdmin):
    """Zarządzanie kontami: adres e-mail jest loginem."""

    form = EmailUserChangeForm
    add_form = EmailUserCreationForm

    list_display = ("email", "is_active", "is_staff", "is_superuser", "last_login")
    list_filter = ("is_active", "is_staff", "is_superuser")
    search_fields = ("email", "username", "first_name", "last_name")
    ordering = ("email",)

    # Przy zakładaniu konta pytamy wyłącznie o adres i hasło - nazwa użytkownika
    # powstaje z adresu, więc osobne pole byłoby tylko okazją do rozbieżności.
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "usable_password", "password1", "password2"),
        }),
    )

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Dane osobowe", {"fields": ("email", "first_name", "last_name")}),
        ("Uprawnienia", {
            "fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
        }),
        ("Ważne daty", {"fields": ("last_login", "date_joined")}),
    )
    readonly_fields = ("last_login", "date_joined")


class AuditMetricInline(admin.TabularInline):
    model = AuditMetric
    extra = 0
    readonly_fields = ("category", "key", "value", "status")
    can_delete = False


@admin.register(Audit)
class AuditAdmin(admin.ModelAdmin):
    # Właściciel na liście i w filtrach: przy wielu kontach to pierwsze pytanie,
    # jakie administrator zadaje patrząc na raport.
    list_display = ("url", "owner", "status", "score", "created_at")
    list_filter = ("status", "owner")
    search_fields = ("url", "owner__username")
    readonly_fields = ("created_at", "ga4_token_status")
    # Lista użytkowników rozrasta się z czasem - pole wyszukiwania zamiast rozwijanej.
    raw_id_fields = ("owner",)
    # Zaszyfrowany token nie ma po co być edytowalny: ręczna zmiana jednego znaku
    # unieważnia połączenie z Google, a sama wartość nic administratorowi nie mówi.
    exclude = ("ga4_refresh_token_encrypted",)
    inlines = [AuditMetricInline]

    @admin.display(description="Token GA4")
    def ga4_token_status(self, obj) -> str:
        """Czy konto Google jest podłączone - bez pokazywania samego tokenu."""
        from auditor.services.crypto import ENCRYPTED_PREFIX

        zapisany = obj.ga4_refresh_token_encrypted or ""
        if not zapisany:
            return "brak połączenia"
        if zapisany.startswith(ENCRYPTED_PREFIX):
            return "podłączony (token zaszyfrowany)"
        return "podłączony (token JAWNY - uruchom manage.py encrypt_tokens)"


class GeoQueryInline(admin.TabularInline):
    model = GeoQuery
    extra = 0
    readonly_fields = ("text", "citation_rate", "stability")
    can_delete = False
    show_change_link = True


@admin.register(GeoStudy)
class GeoStudyAdmin(admin.ModelAdmin):
    list_display = ("domain", "brand_name", "owner", "status", "overall_score", "created_at")
    list_filter = ("status", "owner")
    search_fields = ("domain", "brand_name", "owner__username")
    readonly_fields = ("created_at", "finished_at")
    raw_id_fields = ("owner", "audit")
    inlines = [GeoQueryInline]


@admin.register(GeoRun)
class GeoRunAdmin(admin.ModelAdmin):
    """Pojedyncze odpowiedzi modelu - przydatne przy sprawdzaniu, co zwrócił.

    Tylko do odczytu: są to zapisy tego, co faktycznie odpowiedziała wyszukiwarka AI,
    a ręczna edycja zafałszowałaby pomiar.
    """

    list_display = ("query", "attempt", "visibility", "brand_position", "created_at")
    list_filter = ("visibility", "brand_cited", "brand_mentioned")
    search_fields = ("query__text", "answer")
    raw_id_fields = ("query",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "category")
    list_filter = ("category",)
    search_fields = ("title", "content")
