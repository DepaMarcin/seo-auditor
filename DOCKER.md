# Uruchomienie w Dockerze

Cztery usługi: PostgreSQL, Redis, serwer WWW i worker Celery. Obraz aplikacji jest
jeden — web i worker różnią się wyłącznie poleceniem startowym, więc nie zdarzy się,
że worker pracuje na innej wersji kodu niż aplikacja.

## Pierwsze uruchomienie

```bash
cp .env.docker.example .env        # uzupełnij klucze i hasła
docker compose up --build
```

Aplikacja odpowiada na `http://localhost:8000`. Konto administratora:

```bash
docker compose exec web python manage.py createsuperuser
```

## Co trzeba przygotować przed startem

| Plik / zmienna | Po co | Skutek braku |
|---|---|---|
| `.env` | klucze API, sekret Django, hasło bazy | `compose` przerywa z komunikatem o `POSTGRES_PASSWORD` |
| `client_secret.json` | OAuth do Google Analytics i Search Console | aplikacja wstaje, ale podłączenie konta Google kończy się błędem |
| `TOKEN_ENCRYPTION_KEY` | szyfrowanie zapisanych tokenów Google | zapisane tokeny stają się nieczytelne |

Oba pliki są poza repozytorium (`.gitignore`) i poza obrazem (`.dockerignore`) —
`client_secret.json` montuje się jako wolumen tylko do odczytu. Obraz z sekretem
w warstwie trafiłby do rejestru razem z nim, także po późniejszym usunięciu pliku.

## Dlaczego PostgreSQL, a nie SQLite

Badanie multi-agentowe uruchamia trzech agentów **równolegle**, w osobnych procesach
workera. SQLite blokuje przy tym tabelę („database is locked") i ustalenia agentów
po cichu przepadają — dlatego w `auditor/agents/orchestrator.py` tryb równoległy jest
domyślnie wyłączony. Na PostgreSQL to ograniczenie znika.

Aplikacja wybiera bazę po obecności zmiennej `POSTGRES_DB`: bez niej zostaje SQLite,
więc uruchomienie lokalne nie wymaga żadnej konfiguracji.

## Redis pełni dwie role

Jest pośrednikiem kolejki (`/0`), backendem wyników (`/1`) i cache'em aplikacji (`/2`).
Backend wyników jest **wymagany** przez `chord` — bez niego badanie multi-agentowe
schodzi na wykonanie sekwencyjne (patrz `auditor/tasks.py`). Stąd `--appendonly yes`:
restart Redisa bez zapisu gubiłby wyniki trwających badań.

## Migracje

Wykonuje je usługa `web` przed startem serwera. Worker ich **nie** uruchamia — dwa
procesy startujące migracje równolegle na tej samej bazie to wyścig.

## Czego ten obraz nie zawiera

- **Playwright/Chromium** — fallback przeglądarkowy scrapera dla stron renderowanych
  po stronie klienta (`auditor/services/renderer.py`). Dokłada kilkaset megabajtów,
  więc wymaga świadomej decyzji. Bez niego audyt takich stron kończy się wynikiem
  statycznym, nie błędem.
- **Serwer WWW przed aplikacją** — pliki statyczne serwuje WhiteNoise z procesu
  Django. Przy ruchu produkcyjnym warto postawić przed tym nginxa i tam przenieść
  terminację TLS.
- **Kopie zapasowe bazy** — wolumen `dane_postgres` przeżywa restart kontenera, ale
  nie jest kopią zapasową.

## Przydatne polecenia

```bash
docker compose logs -f worker              # co robią agenci
docker compose exec web python manage.py test   # pakiet testów w kontenerze
docker compose exec db psql -U seo_auditor      # konsola bazy
docker compose down                        # zatrzymanie, wolumeny zostają
docker compose down -v                     # zatrzymanie i USUNIĘCIE danych
```

## Uwaga o zależnościach

`gunicorn` leży w `requirements-docker.txt`, a nie we wspólnym `requirements.txt`,
bo nie instaluje się na Windows (wymaga modułu `fcntl`). Projekt rozwija się lokalnie
na Windows, więc `pip install -r requirements.txt` musi tam przechodzić bez błędu.
Obraz instaluje oba pliki.
