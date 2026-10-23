# Uruchomienie w Dockerze

Cztery usługi: PostgreSQL, Redis, serwer WWW i worker Celery. Obraz aplikacji jest
jeden — web i worker różnią się wyłącznie poleceniem startowym, więc nie zdarzy się,
że worker pracuje na innej wersji kodu niż aplikacja.

## Pierwsze uruchomienie

```bash
# UWAGA: `cp` nadpisuje istniejący `.env` bez pytania - razem z kluczami,
# których nie da się odtworzyć (TOKEN_ENCRYPTION_KEY odszyfrowuje zapisane
# tokeny Google). Najpierw kopia zapasowa.
[ -f .env ] && cp .env .env.kopia
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
| `DJANGO_SECURE_SSL_REDIRECT=False` | ten zestaw odpowiada po http | `ERR_EMPTY_RESPONSE` w przeglądarce |

Oba pliki są poza repozytorium (`.gitignore`) i poza obrazem (`.dockerignore`) —
`client_secret.json` montuje się jako wolumen tylko do odczytu. Obraz z sekretem
w warstwie trafiłby do rejestru razem z nim, także po późniejszym usunięciu pliku.

## Ten zestaw odpowiada po http, nie po https

Przed aplikacją nie stoi nic, co podawałoby TLS, dlatego compose ustawia
`DJANGO_SECURE_SSL_REDIRECT=False`. Przy włączonym wymuszaniu HTTPS Django odbija
każde żądanie na adres `https://localhost:8000`, pod którym nikt nie nasłuchuje —
przeglądarka pokazuje wtedy **ERR_EMPTY_RESPONSE** przy sprawnym kontenerze
(`docker compose ps` mówi `healthy`, bo `curl` nie uznaje 301 za błąd). Ciasteczka
`Secure` zawodzą jeszcze ciszej: logowanie po http nie zapisuje sesji i formularz
wraca bez komunikatu.

Po dostawieniu nginxa z certyfikatem w `.env` wpisuje się `DJANGO_SECURE_SSL_REDIRECT=True`
i `DJANGO_CSRF_TRUSTED_ORIGINS=https://...`. Poza Dockerem przy `DEBUG=False`
wymuszanie jest **włączone domyślnie** — wdrożenie bez świadomej decyzji zostaje
zabezpieczone.

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

Wykonuje je usługa `web` przed startem serwera, w skrypcie `docker/start-web.sh`.
Worker ich **nie** uruchamia — dwa procesy startujące migracje równolegle na tej
samej bazie to wyścig.

Polecenie startowe leży w skrypcie, a nie w `command:` w compose, bo zwinięty blok
YAML (`>`) scala tylko linie o tym samym wcięciu, a głębiej wcięte zachowuje razem
ze znakami nowej linii. Rozpisany tam gunicorn rozpadał się więc na osobne
polecenia: startował bez flag z dalszych linii, w tym bez `--bind`, i słuchał na
domyślnym `127.0.0.1:8000` — adresie wnętrza kontenera, do którego mapowanie portu
nie prowadzi. W logach było to widać jako `Listening at: http://127.0.0.1:8000`.

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
