"""Widok logowania z ochroną przed zgadywaniem haseł."""
from __future__ import annotations

import logging

from django.contrib.auth.views import LoginView
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from auditor.forms import EmailAuthenticationForm
from auditor.ratelimit import is_login_rate_limited

logger = logging.getLogger(__name__)


class ThrottledLoginView(LoginView):
    """Logowanie z limitem prób na adres IP.

    Bez limitu jedyną przeszkodą dla zgadywania haseł jest ich siła, a formularz
    przyjmuje dowolną liczbę żądań na sekundę. Licznik prowadzimy po adresie, nie po
    koncie: przy nieudanej próbie nie wiadomo jeszcze, czyje konto jest celem, a limit
    per konto pozwalałby samym zgadywaniem zablokować komuś dostęp.
    """

    authentication_form = EmailAuthenticationForm

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        # Liczymy wyłącznie żądania POST - samo otwarcie strony logowania nie zbliża
        # nikogo do odgadnięcia hasła.
        if is_login_rate_limited(request):
            logger.warning(
                "Przekroczono limit prób logowania z adresu %s.",
                request.META.get("REMOTE_ADDR", "nieznany"),
            )
            return render(
                request,
                "registration/login_blocked.html",
                {"form": self.get_form()},
                status=429,
            )

        return super().post(request, *args, **kwargs)
