"""Logowanie adresem e-mail."""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

logger = logging.getLogger(__name__)


class EmailBackend(ModelBackend):
    """Uwierzytelnianie adresem e-mail zamiast nazwą użytkownika.

    Szukamy po `email` ORAZ po `username`: część kont ma adres wpisany w obu polach
    (nowe konta zakładane w panelu tak właśnie powstają), a część tylko w `email`.
    Porównanie bez rozróżniania wielkości liter, bo adresy e-mail podaje się różnie,
    a `USERNAME_FIELD` Django domyślnie rozróżnia wielkość znaków.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()

        # Formularz logowania nazywa to pole `username`, nawet gdy zbiera adres e-mail.
        login = username or kwargs.get(UserModel.USERNAME_FIELD) or kwargs.get("email")
        if not login or not password:
            return None

        users = list(
            UserModel._default_manager.filter(
                Q(email__iexact=login) | Q(username__iexact=login)
            )
        )

        if not users:
            # Ten sam koszt czasowy co przy istniejącym koncie - inaczej czas
            # odpowiedzi zdradzałby, które adresy są zarejestrowane.
            UserModel().set_password(password)
            return None

        if len(users) > 1:
            # Dwa konta na jeden adres to błąd danych, a nie sytuacja do rozstrzygania
            # przy logowaniu - zgadywanie, które z nich wpuścić, byłoby gorsze.
            logger.warning(
                "Logowanie odrzucone: adres %s wskazuje na %s kont.", login, len(users)
            )
            return None

        user = users[0]
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
