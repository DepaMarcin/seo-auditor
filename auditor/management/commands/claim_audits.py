"""Przypisuje audyty bez właściciela do wskazanego użytkownika.

Potrzebne jednorazowo po wdrożeniu autoryzacji: audyty utworzone, gdy aplikacja była
publiczna, mają `owner = NULL` i po zmianie nie są widoczne dla nikogo. Polecenie
pozwala je odzyskać, zamiast kasować historię:

    python manage.py createsuperuser
    python manage.py claim_audits <login>
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from auditor.models import Audit


class Command(BaseCommand):
    help = "Przypisuje wszystkie audyty bez właściciela do wskazanego użytkownika."

    def add_arguments(self, parser) -> None:
        parser.add_argument("username", help="Login użytkownika, który ma przejąć audyty.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Pokaż, ile audytów zostałoby przypisanych, bez zapisu do bazy.",
        )

    def handle(self, *args, **options) -> None:
        username = options["username"]
        user_model = get_user_model()

        try:
            user = user_model.objects.get(username=username)
        except user_model.DoesNotExist as exc:
            raise CommandError(
                f"Nie ma użytkownika '{username}'. Utwórz go: python manage.py createsuperuser"
            ) from exc

        orphaned = Audit.objects.filter(owner__isnull=True)
        count = orphaned.count()

        if options["dry_run"]:
            self.stdout.write(f"[dry-run] Do przypisania: {count} audyt(ów) -> {username}.")
            return

        orphaned.update(owner=user)
        self.stdout.write(self.style.SUCCESS(f"Przypisano {count} audyt(ów) do użytkownika {username}."))
