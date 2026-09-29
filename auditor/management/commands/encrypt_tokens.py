"""Szyfruje tokeny OAuth zapisane w bazie jawnym tekstem.

Potrzebne po dodaniu `TOKEN_ENCRYPTION_KEY` do konfiguracji: rekordy zapisane
wcześniej (albo lokalnie bez klucza) mają token w postaci czytelnej i zostałyby
takie na zawsze - `encrypt_secret` działa dopiero przy kolejnym zapisie pola.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from auditor.models import Audit
from auditor.services.crypto import ENCRYPTED_PREFIX, encrypt_secret


class Command(BaseCommand):
    help = "Szyfruje tokeny odświeżania Google zapisane w bazie jawnym tekstem."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Pokazuje, ile rekordów zostałoby zaszyfrowanych, bez zapisu.",
        )

    def handle(self, *args, **options):
        from django.conf import settings

        if not getattr(settings, "TOKEN_ENCRYPTION_KEY", ""):
            raise CommandError(
                "Brak TOKEN_ENCRYPTION_KEY - bez klucza nie ma czym szyfrować.\n"
                "Wygeneruj go poleceniem:\n"
                '  python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"\n'
                "i dopisz do pliku .env jako TOKEN_ENCRYPTION_KEY."
            )

        dry_run = options["dry_run"]

        # Bierzemy rekordy z niepustym tokenem i sprawdzamy prefiks w Pythonie:
        # zapytanie po `startswith` działałoby inaczej w różnych silnikach bazy.
        kandydaci = Audit.objects.exclude(ga4_refresh_token_encrypted__isnull=True).exclude(
            ga4_refresh_token_encrypted=""
        )

        jawne = [
            audit
            for audit in kandydaci
            if not (audit.ga4_refresh_token_encrypted or "").startswith(ENCRYPTED_PREFIX)
        ]

        if not jawne:
            self.stdout.write(self.style.SUCCESS("Wszystkie tokeny są już zaszyfrowane."))
            return

        self.stdout.write(f"Tokenów do zaszyfrowania: {len(jawne)}")
        for audit in jawne:
            # Nie drukujemy tokenu ani jego fragmentu - trafiłby do historii terminala.
            self.stdout.write(f"  audyt #{audit.pk} ({audit.url})")

        if dry_run:
            self.stdout.write(self.style.WARNING("Tryb próbny - nic nie zapisano."))
            return

        for audit in jawne:
            audit.ga4_refresh_token_encrypted = encrypt_secret(
                audit.ga4_refresh_token_encrypted
            )
            audit.save(update_fields=["ga4_refresh_token_encrypted"])

        self.stdout.write(
            self.style.SUCCESS(f"Zaszyfrowano tokeny w {len(jawne)} rekordach.")
        )
