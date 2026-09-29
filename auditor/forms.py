"""Formularze uwierzytelniania i zarządzania kontami."""
from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    AdminUserCreationForm,
    AuthenticationForm,
    UserChangeForm,
)
from django.core.validators import validate_email


class EmailAuthenticationForm(AuthenticationForm):
    """Logowanie adresem e-mail.

    Pole nazywa się nadal `username` - tak wymaga `AuthenticationForm` i tak trafia
    do backendu. Zmienia się etykieta, typ pola i walidacja formatu.
    """

    username = forms.CharField(
        label="Adres e-mail",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "Nieprawidłowy adres e-mail lub hasło.",
    }

    def clean_username(self) -> str:
        login = (self.cleaned_data.get("username") or "").strip()

        # Odrzucamy zły format od razu, zamiast pytać bazę o coś, co adresem nie jest.
        validate_email(login)
        return login


class EmailUserCreationForm(AdminUserCreationForm):
    """Zakładanie konta w panelu: adres e-mail jest jedyną potrzebną nazwą.

    `auth.User.email` jest w Django polem opcjonalnym i nie da się tego zmienić bez
    własnego modelu użytkownika (migracja całej tabeli). Wymóg egzekwujemy więc na
    formularzu - z punktu widzenia administratora działa tak samo.
    """

    email = forms.EmailField(
        label="Adres e-mail",
        required=True,
        help_text="Służy zarazem jako login. Zostanie zapisany również w polu nazwy użytkownika.",
    )

    class Meta(AdminUserCreationForm.Meta):
        model = get_user_model()
        fields = ("email",)

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip()
        UserModel = get_user_model()

        # Adres jest loginem, więc musi być niepowtarzalny - inaczej backend nie
        # wiedziałby, które konto wpuścić.
        if UserModel._default_manager.filter(email__iexact=email).exists():
            raise forms.ValidationError("Konto z tym adresem e-mail już istnieje.")
        if UserModel._default_manager.filter(username__iexact=email).exists():
            raise forms.ValidationError("Konto z tym adresem e-mail już istnieje.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        email = self.cleaned_data["email"]
        # Nazwa użytkownika równa adresowi: logowanie działa wtedy niezależnie od
        # tego, po którym polu backend akurat trafi.
        user.email = email
        user.username = email
        if commit:
            user.save()
        return user


class EmailUserChangeForm(UserChangeForm):
    """Edycja konta: adres e-mail pozostaje wymagany."""

    email = forms.EmailField(label="Adres e-mail", required=True)

    class Meta(UserChangeForm.Meta):
        model = get_user_model()

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip()
        UserModel = get_user_model()

        zajety = (
            UserModel._default_manager.filter(email__iexact=email)
            .exclude(pk=self.instance.pk)
            .exists()
        )
        if zajety:
            raise forms.ValidationError("Konto z tym adresem e-mail już istnieje.")
        return email
