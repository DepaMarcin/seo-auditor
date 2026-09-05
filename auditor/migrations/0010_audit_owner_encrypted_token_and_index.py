"""Autoryzacja audytów, szyfrowanie tokenów OAuth i indeks na dacie utworzenia.

Migracja jest napisana ręcznie (zamiast `makemigrations`), bo obejmuje trzy zmiany,
które muszą wejść razem, oraz migrację danych szyfrującą istniejące tokeny:

1. `Audit.owner` - audyty przestają być publiczne (nullable wyłącznie dla rekordów
   sprzed wdrożenia; przypisz je poleceniem `manage.py claim_audits <login>`).
2. `ga4_refresh_token` -> `ga4_refresh_token_encrypted` + zaszyfrowanie zastanych
   wartości. Rename zamiast pary add/remove, żeby nie utracić żadnego połączenia z GA4.
3. `created_at` z `db_index=True` - kolumna sortowania listy audytów (`Meta.ordering`).
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

from auditor.services.crypto import ENCRYPTED_PREFIX, encrypt_secret


def encrypt_existing_tokens(apps, schema_editor):
    """Szyfruje tokeny zapisane wcześniej jawnym tekstem."""
    Audit = apps.get_model("auditor", "Audit")
    for audit in Audit.objects.exclude(ga4_refresh_token_encrypted__isnull=True).exclude(
        ga4_refresh_token_encrypted=""
    ):
        if audit.ga4_refresh_token_encrypted.startswith(ENCRYPTED_PREFIX):
            continue  # już zaszyfrowany (np. przy ponownym uruchomieniu migracji)
        audit.ga4_refresh_token_encrypted = encrypt_secret(audit.ga4_refresh_token_encrypted)
        audit.save(update_fields=["ga4_refresh_token_encrypted"])


def noop_reverse(apps, schema_editor):
    """Świadomie pusta operacja odwrotna.

    Odszyfrowanie z powrotem do postaci jawnej byłoby cofnięciem zabezpieczenia, a
    `decrypt_secret` i tak radzi sobie z wartościami zapisanymi w obu formatach.
    """


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("auditor", "0009_alter_auditmetric_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="audit",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="audits",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RenameField(
            model_name="audit",
            old_name="ga4_refresh_token",
            new_name="ga4_refresh_token_encrypted",
        ),
        migrations.AlterField(
            model_name="audit",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, db_index=True),
        ),
        migrations.RunPython(encrypt_existing_tokens, noop_reverse),
    ]
