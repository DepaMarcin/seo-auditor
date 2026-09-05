"""Rejestracja aplikacji Celery przy starcie Django (wymagana przez @shared_task)."""
from .celery import app as celery_app

__all__ = ("celery_app",)
