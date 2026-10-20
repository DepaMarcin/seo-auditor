# Obraz aplikacji SEO Auditor - wspólny dla serwera WWW i workera Celery.
#
# Jeden obraz, dwa polecenia uruchomienia: web startuje gunicorna, worker startuje
# Celery. Rozdzielenie ich na osobne obrazy znaczyłoby dwa razy dłuższe budowanie
# i ryzyko, że worker pracuje na innej wersji kodu niż aplikacja.
#
# Budowanie dwuetapowe: zależności instalują się w etapie `builder`, a do obrazu
# końcowego trafia samo środowisko wirtualne. Narzędzia kompilacji (gcc, nagłówki)
# zostają po drodze i nie powiększają tego, co idzie do rejestru.

# ----------------------------------------------------------------------
# Etap 1: zależności
# ----------------------------------------------------------------------
FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# `build-essential` jest potrzebny tylko wtedy, gdy dla którejś zależności
# zabraknie gotowego koła (wheel) na tę wersję Pythona.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Zależności kopiujemy przed kodem: warstwa z nimi zmienia się rzadko, więc
# przebudowa po zmianie kodu nie instaluje wszystkiego od nowa.
COPY requirements.txt requirements-docker.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install -r requirements-docker.txt

# ----------------------------------------------------------------------
# Etap 2: obraz uruchomieniowy
# ----------------------------------------------------------------------
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings

# `libpq5` to biblioteka kliencka PostgreSQL wymagana w czasie działania.
# `curl` służy wyłącznie badaniu stanu kontenera (healthcheck w compose).
RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /venv /venv

WORKDIR /app

# Użytkownik bez uprawnień administratora: kod aplikacji pobiera i renderuje cudze
# strony, więc proces nie ma powodu działać jako root.
RUN useradd --create-home --uid 1000 aplikacja

COPY --chown=aplikacja:aplikacja . /app

# Pliki statyczne panelu administracyjnego zbieramy przy budowaniu, nie przy starcie:
# inaczej każdy restart kontenera powtarzałby tę samą pracę.
#
# Zmienne są atrapami na czas budowania - obraz nie może zawierać prawdziwych
# sekretów, a `collectstatic` i tak ich nie używa.
RUN DJANGO_SECRET_KEY=budowanie-obrazu \
    DJANGO_DEBUG=False \
    python manage.py collectstatic --noinput \
    && chown -R aplikacja:aplikacja /app/staticfiles

USER aplikacja

EXPOSE 8000

# Domyślnie serwer WWW. Worker Celery nadpisuje to poleceniem w compose.
#
# `--timeout 180`, bo pojedyncze żądanie potrafi czekać na odpowiedź Google
# albo na skan strony. Domyślne 30 s zabijałoby je w połowie.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "180", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
