from django.contrib import admin

from .models import Audit, AuditMetric, KnowledgeDocument


class AuditMetricInline(admin.TabularInline):
    model = AuditMetric
    extra = 0
    readonly_fields = ("category", "key", "value", "status")
    can_delete = False


@admin.register(Audit)
class AuditAdmin(admin.ModelAdmin):
    list_display = ("url", "status", "score", "created_at")
    list_filter = ("status",)
    readonly_fields = ("created_at",)
    inlines = [AuditMetricInline]


@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "category")
    list_filter = ("category",)
    search_fields = ("title", "content")
