"""Przenosi tokeny Google z audytów do konta użytkownika.

Token odświeżania leżał przy każdym audycie osobno, więc po kilkunastu autoryzacjach
w bazie zbierało się kilkanaście niezależnych poświadczeń. Aplikacja brała "jakieś"
z nich i uznawała powiązaną domenę za podłączoną - także wtedy, gdy zalogowane konto
Google nie miało do niej dostępu.

Zachowujemy token z NAJNOWSZEGO audytu: to ostatnia autoryzacja, więc jedyna, co do
której mamy pewność, że nie została unieważniona przez kolejne logowanie. Pozostałe
czyścimy - były martwymi kopiami, a każda z nich to długoterminowe poświadczenie
do cudzych danych analitycznych.
"""
from django.db import migrations


def move_tokens(apps, schema_editor):
    Audit = apps.get_model("auditor", "Audit")
    GoogleAccount = apps.get_model("auditor", "GoogleAccount")

    z_tokenem = (
        Audit.objects.exclude(ga4_refresh_token_encrypted__isnull=True)
        .exclude(ga4_refresh_token_encrypted="")
        .exclude(owner__isnull=True)
        .order_by("owner_id", "-created_at")
    )

    widziani = set()
    for audit in z_tokenem:
        if audit.owner_id in widziani:
            continue
        widziani.add(audit.owner_id)

        GoogleAccount.objects.update_or_create(
            user_id=audit.owner_id,
            defaults={
                "refresh_token_encrypted": audit.ga4_refresh_token_encrypted,
                "email": audit.ga4_account_email or "",
            },
        )

    # Kopie tokenu w audytach nie mają już po co istnieć.
    z_tokenem.model.objects.exclude(ga4_refresh_token_encrypted__isnull=True).update(
        ga4_refresh_token_encrypted=None
    )


def noop(apps, schema_editor):
    """Wstecz nie odtwarzamy kopii tokenu - to był właśnie problem."""


class Migration(migrations.Migration):

    dependencies = [
        ("auditor", "0019_googleaccount"),
    ]

    operations = [
        migrations.RunPython(move_tokens, noop),
    ]
