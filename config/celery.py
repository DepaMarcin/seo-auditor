"""Konfiguracja Celery dla projektu.

Audyt (scraping + 2x PageSpeed + po jednym wywołaniu LLM na każdy wykryty problem)
trwa minuty, więc nie może blokować wątku HTTP. Uruchomienie workera:

    celery -A config worker -l info --pool=solo     # --pool=solo jest wymagane na Windows
"""
from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("seo_auditor")
# Wszystkie ustawienia z prefiksem CELERY_ w config/settings.py.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
