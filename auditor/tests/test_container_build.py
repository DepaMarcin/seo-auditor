"""Konfiguracja, od której zależy zbudowanie obrazu kontenera.

Budowanie wywróciło się na `collectstatic`: Dockerfile podawał atrapę
`DJANGO_SECRET_KEY`, ale nie `TOKEN_ENCRYPTION_KEY`, a przy `DEBUG=False` strażnik
produkcyjny w `config/settings.py` zatrzymuje import ustawień, zanim Django
cokolwiek zrobi. Plik `.env` nie trafia do obrazu (`.dockerignore`), więc klucza
nie było skąd wziąć.

Ustawienia sprawdzamy w PODPROCESIE: w procesie testowym są już zaimportowane,
a to właśnie ich import jest tu przedmiotem badania.
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys

import yaml

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

# Zmienne, które Dockerfile podaje krokowi `collectstatic`. Atrapy - `collectstatic`
# żadnej z nich nie używa, ale import ustawień wymaga ich obecności.
SRODOWISKO_BUDOWANIA = {
    "DJANGO_SETTINGS_MODULE": "config.settings",
    "DJANGO_DEBUG": "False",
    "DJANGO_SECRET_KEY": "atrapa-tylko-na-czas-budowania",
    "TOKEN_ENCRYPTION_KEY": "atrapa-tylko-na-czas-budowania-nieuzywana",
    "DJANGO_STATIC_MANIFEST": "1",
}

ODCZYT_USTAWIEN = (
    "import os, django, json;"
    "django.setup();"
    "from django.conf import settings as s;"
    "print('WYNIK' + json.dumps({"
    "'debug': s.DEBUG,"
    "'staticfiles': s.STORAGES['staticfiles']['BACKEND'].split('.')[-1],"
    "'static_root_konczy': os.path.basename(str(s.STATIC_ROOT)),"
    "'ssl_redirect': s.SECURE_SSL_REDIRECT,"
    "'sesja_secure': s.SESSION_COOKIE_SECURE,"
    "'csrf_secure': s.CSRF_COOKIE_SECURE,"
    "'hsts': s.SECURE_HSTS_SECONDS,"
    "'nosniff': s.SECURE_CONTENT_TYPE_NOSNIFF,"
    "'proxy_naglowek': bool(s.SECURE_PROXY_SSL_HEADER),"
    "}))"
)


def _import_ustawien(env: dict) -> subprocess.CompletedProcess:
    """Importuje `config.settings` w osobnym procesie z podanym środowiskiem.

    Przekazujemy PEŁNE środowisko docelowe, nie nakładkę na bieżące: inaczej
    zmienne z `.env` wczytane w procesie testowym maskowałyby braki, o które tu
    właśnie chodzi.
    """
    srodowisko = {
        # Minimum, bez którego interpreter nie wstaje na Windows.
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PYTHONIOENCODING": "utf-8",
        **env,
    }
    return subprocess.run(
        [sys.executable, "-c", ODCZYT_USTAWIEN],
        capture_output=True,
        text=True,
        cwd=str(settings.BASE_DIR),
        env=srodowisko,
    )


# Nazwy, pod jakimi sekrety naprawdę lądują w katalogu projektu. Konsola Google
# zapisuje sekret OAuth pod kilkoma postaciami, w tym ze spacją - wzorzec
# `client_secret*.json` mijał tę ze spacją, a plik poza wzorcem to plik, który
# da się zacommitować.
NAZWY_SEKRETOW = (
    ".env",
    ".env.local",
    ".env.kopia",
    "client_secret.json",
    "client secret.json",
    "client_secret_1-abc.apps.googleusercontent.com.json",
    "klucz.pem",
    "db.sqlite3",
)

# Szablony bez wartości - te mają być wersjonowane, bo z nich powstaje `.env`.
NAZWY_SZABLONOW = (".env.example", ".env.docker.example")


def _czy_ignorowany(nazwa: str, ignore: str) -> bool:
    """Czy wzorce z pliku ignorowania zakrywają tę nazwę.

    Sprawdzamy DOPASOWANIE, nie obecność napisu w pliku: test szukający napisu
    przechodzi także wtedy, gdy wzorzec mija nazwę, której miał dotyczyć.
    Ostatnie dopasowanie wygrywa - tak samo czytają to git i Docker, dzięki czemu
    liczy się kolejność reguł i zaprzeczeń (`!`).
    """
    ignorowany = False
    for linia in ignore.splitlines():
        wzorzec = linia.strip()
        if not wzorzec or wzorzec.startswith("#"):
            continue
        zaprzeczenie = wzorzec.startswith("!")
        wzorzec = wzorzec.lstrip("!").rstrip("/")
        if fnmatch.fnmatch(nazwa, wzorzec):
            ignorowany = not zaprzeczenie
    return ignorowany


class BuildEnvironmentTests(SimpleTestCase):
    """Import ustawień w warunkach, w jakich pracuje `collectstatic` w obrazie."""

    def test_settings_import_succeeds(self):
        wynik = _import_ustawien(SRODOWISKO_BUDOWANIA)

        self.assertEqual(wynik.returncode, 0, wynik.stderr[-600:])

    def test_manifest_storage_is_active_during_the_build(self):
        # Bez tego `collectstatic` nie tworzy `staticfiles.json`, a pliki statyczne
        # jadą bez haszy w nazwach - przeglądarka podaje wtedy starą wersję.
        wynik = _import_ustawien(SRODOWISKO_BUDOWANIA)

        dane = json.loads(
            next(l for l in wynik.stdout.splitlines() if l.startswith("WYNIK"))[5:]
        )
        self.assertEqual(dane["staticfiles"], "CompressedManifestStaticFilesStorage")
        self.assertFalse(dane["debug"])

    def test_missing_encryption_key_is_still_refused(self):
        # Naprawa budowania nie może osłabić strażnika: na produkcji brak klucza
        # znaczyłby tokeny Google zapisywane w bazie jawnym tekstem.
        bez_klucza = {**SRODOWISKO_BUDOWANIA, "TOKEN_ENCRYPTION_KEY": ""}

        wynik = _import_ustawien(bez_klucza)

        self.assertNotEqual(wynik.returncode, 0)
        self.assertIn("TOKEN_ENCRYPTION_KEY", wynik.stderr)

    def test_manifest_is_off_by_default(self):
        # Lokalnie manifestu nie ma, bo nikt nie uruchamia `collectstatic` -
        # wariant manifestowy wywracałby wtedy panel administracyjny.
        bez_flagi = {
            k: v for k, v in SRODOWISKO_BUDOWANIA.items() if k != "DJANGO_STATIC_MANIFEST"
        }

        wynik = _import_ustawien(bez_flagi)

        self.assertEqual(wynik.returncode, 0, wynik.stderr[-400:])
        dane = json.loads(
            next(l for l in wynik.stdout.splitlines() if l.startswith("WYNIK"))[5:]
        )
        self.assertEqual(dane["staticfiles"], "CompressedStaticFilesStorage")


class DockerfileContractTests(SimpleTestCase):
    """Co Dockerfile musi podać krokowi `collectstatic`."""

    @property
    def dockerfile(self) -> str:
        return (Path(settings.BASE_DIR) / "Dockerfile").read_text(encoding="utf-8")

    def test_build_step_supplies_the_encryption_key(self):
        # Pominięcie tej zmiennej było przyczyną wywrotki budowania.
        krok = self.dockerfile[self.dockerfile.index("collectstatic") - 600 :]

        self.assertIn("TOKEN_ENCRYPTION_KEY=", krok)
        self.assertIn("DJANGO_SECRET_KEY=", krok)

    def test_manifest_flag_is_set_for_the_image(self):
        self.assertIn("DJANGO_STATIC_MANIFEST=1", self.dockerfile)

    def test_build_time_values_are_named_as_dummies(self):
        # Wartość w warstwie obrazu zostaje w niej na zawsze. Nazwa musi mówić,
        # że to atrapa, żeby nikt nie wziął jej za działający sekret.
        for linia in self.dockerfile.splitlines():
            if "TOKEN_ENCRYPTION_KEY=" in linia and not linia.strip().startswith("#"):
                with self.subTest(linia=linia.strip()):
                    self.assertIn("atrapa", linia)

    def test_secrets_never_enter_the_build_context(self):
        # `.dockerignore` jest tu jedyną ochroną: plik skopiowany do warstwy
        # zostaje w niej także po późniejszym usunięciu.
        ignore = (Path(settings.BASE_DIR) / ".dockerignore").read_text(encoding="utf-8")

        for nazwa in NAZWY_SEKRETOW:
            with self.subTest(nazwa=nazwa):
                self.assertTrue(_czy_ignorowany(nazwa, ignore))

        # Środowisko wirtualne trzymamy poza kontekstem z powodu rozmiaru.
        self.assertTrue(_czy_ignorowany("venv", ignore))


class RepositoryIgnoreTests(SimpleTestCase):
    """Czego `.gitignore` nie może wpuścić do repozytorium.

    Sekret raz zacommitowany zostaje w historii także po usunięciu pliku, więc
    reguła musi zakrywać nazwę, pod jaką plik naprawdę powstaje - nie tylko tę,
    którą miał autor reguły w głowie.
    """

    @property
    def ignore(self) -> str:
        return (Path(settings.BASE_DIR) / ".gitignore").read_text(encoding="utf-8")

    def test_secret_files_are_ignored_under_every_name_they_take(self):
        for nazwa in NAZWY_SEKRETOW:
            with self.subTest(nazwa=nazwa):
                self.assertTrue(_czy_ignorowany(nazwa, self.ignore))

    def test_templates_stay_versioned(self):
        # Z nich powstaje prawdziwy `.env` - bez nich nie ma z czego startować.
        for nazwa in NAZWY_SZABLONOW:
            with self.subTest(nazwa=nazwa):
                self.assertFalse(_czy_ignorowany(nazwa, self.ignore))

    def test_the_rag_knowledge_base_stays_versioned(self):
        # Dokumenty audytowe klientów są poufne, ale zanonimizowana baza wiedzy
        # zasila silnik rekomendacji i musi być w repozytorium.
        self.assertTrue(_czy_ignorowany("docs/audits/klient.pdf", self.ignore))
        self.assertFalse(
            _czy_ignorowany("docs/audits/rag_knowledge_base.json", self.ignore)
        )

    def test_derived_and_local_directories_are_ignored(self):
        for nazwa in ("venv", "chroma_db", "staticfiles", "media", ".vscode", ".idea"):
            with self.subTest(nazwa=nazwa):
                self.assertTrue(_czy_ignorowany(nazwa, self.ignore))

    def test_editor_and_system_litter_is_ignored(self):
        for nazwa in ("Thumbs.db", ".DS_Store"):
            with self.subTest(nazwa=nazwa):
                self.assertTrue(_czy_ignorowany(nazwa, self.ignore))


class HttpsEnforcementTests(SimpleTestCase):
    """Wymuszanie HTTPS przy DEBUG=False.

    `docker compose up` odpowiada po http - nic w tym zestawie nie podaje TLS-u.
    Przy wymuszonym przekierowaniu Django odbija każde żądanie na adres https,
    pod którym nikt nie nasłuchuje, i przeglądarka pokazuje ERR_EMPTY_RESPONSE
    przy kontenerze zgłaszającym `healthy` (`curl` nie uznaje 301 za błąd).
    """

    def _ustawienia(self, **nadpisania) -> dict:
        srodowisko = {**SRODOWISKO_BUDOWANIA, **nadpisania}
        srodowisko = {k: v for k, v in srodowisko.items() if v is not None}

        wynik = _import_ustawien(srodowisko)

        self.assertEqual(wynik.returncode, 0, wynik.stderr[-600:])
        return json.loads(
            next(l for l in wynik.stdout.splitlines() if l.startswith("WYNIK"))[5:]
        )

    def test_enforced_by_default_on_production(self):
        # Wdrożenie, przy którym nikt nie podjął decyzji, zostaje zabezpieczone.
        dane = self._ustawienia()

        self.assertTrue(dane["ssl_redirect"])
        self.assertTrue(dane["sesja_secure"])
        self.assertTrue(dane["csrf_secure"])
        self.assertGreater(dane["hsts"], 0)

    def test_switch_lets_the_app_answer_over_plain_http(self):
        dane = self._ustawienia(DJANGO_SECURE_SSL_REDIRECT="False")

        self.assertFalse(dane["ssl_redirect"])
        # Ciasteczka muszą pójść razem z przekierowaniem: `Secure` po http znaczy
        # sesja, której przeglądarka nie zapisze - logowanie wraca bez komunikatu.
        self.assertFalse(dane["sesja_secure"])
        self.assertFalse(dane["csrf_secure"])
        self.assertEqual(dane["hsts"], 0)
        # Nagłówek od proxy ma sens tylko wtedy, gdy proxy istnieje: inaczej klient
        # podaje go sam i udaje połączenie szyfrowane.
        self.assertFalse(dane["proxy_naglowek"])

    def test_switch_does_not_disable_the_other_headers(self):
        dane = self._ustawienia(DJANGO_SECURE_SSL_REDIRECT="False")

        self.assertTrue(dane["nosniff"])

    def test_switch_can_be_turned_back_on(self):
        # Po dostawieniu proxy z certyfikatem - jeden wpis w `.env`.
        dane = self._ustawienia(DJANGO_SECURE_SSL_REDIRECT="True")

        self.assertTrue(dane["ssl_redirect"])
        self.assertTrue(dane["proxy_naglowek"])

    def test_local_development_is_untouched(self):
        dane = self._ustawienia(DJANGO_DEBUG="True", TOKEN_ENCRYPTION_KEY="")

        self.assertTrue(dane["debug"])
        self.assertFalse(dane["ssl_redirect"])


class ComposeContractTests(SimpleTestCase):
    """Co `docker-compose.yml` musi podać kontenerom."""

    @property
    def compose(self) -> str:
        return (Path(settings.BASE_DIR) / "docker-compose.yml").read_text(encoding="utf-8")

    def test_plain_http_is_the_default_for_the_compose_stack(self):
        # Bez tego wpisu `.env` skopiowany z wzoru daje DEBUG=False i puste pole,
        # czyli wymuszanie HTTPS - a przed aplikacją nie ma czym go obsłużyć.
        self.assertIn("DJANGO_SECURE_SSL_REDIRECT: ${DJANGO_SECURE_SSL_REDIRECT:-False}", self.compose)

    def test_dockerfile_default_command_binds_the_same_way(self):
        # Usługi uruchamiane bez `command` (np. jednorazowe `docker run`) biorą CMD
        # z obrazu - tam flaga musi być tak samo.
        dockerfile = (Path(settings.BASE_DIR) / "Dockerfile").read_text(encoding="utf-8")
        cmd = dockerfile[dockerfile.index("CMD ["):]

        self.assertIn('"--bind", "0.0.0.0:8000"', cmd)


# Odwrotny ukośnik na końcu linii: w powłoce scala ją z następną.
KONTYNUACJA_LINII = "\\\n"
CRLF = b"\r\n"


def _logiczne_polecenia(skrypt: str) -> list[str]:
    r"""Dzieli tekst na polecenia tak, jak podzieli go `sh`.

    Kontynuacja `\` scala linie, ale sam znak nowej linii KOŃCZY polecenie.
    Dokładnie to przegapiła pierwsza wersja tych testów: sprawdzała obecność
    `--bind 0.0.0.0:8000` w pliku, a flaga stała w osobnej linii - czyli w osobnym
    poleceniu, do którego gunicorn nigdy nie zaglądał.
    """
    scalony = skrypt.replace(KONTYNUACJA_LINII, " ")
    return [
        linia.strip()
        for linia in scalony.splitlines()
        # Komentarze odpadają: piszą o gunicornie, ale go nie uruchamiają.
        if linia.strip() and not linia.strip().startswith("#")
    ]


class WebStartupCommandTests(SimpleTestCase):
    """Z czym naprawdę startuje gunicorn w usłudze `web`.

    Logi pokazały `Listening at: http://127.0.0.1:8000` przy pliku, w którym
    `--bind 0.0.0.0:8000` było obecne - zwinięty blok YAML (`>`) zachowuje znaki
    nowej linii w liniach wciętych głębiej niż pierwsza, więc `sh -c` dostawał
    polecenie gunicorna rozbite na kawałki i uruchamiał sam jego początek.
    """

    SKRYPT = "docker/start-web.sh"

    @property
    def compose(self) -> dict:
        return yaml.safe_load(
            (Path(settings.BASE_DIR) / "docker-compose.yml").read_text(encoding="utf-8")
        )

    @property
    def skrypt(self) -> str:
        return (Path(settings.BASE_DIR) / self.SKRYPT).read_text(encoding="utf-8")

    def test_gunicorn_is_started_with_the_bind_flag(self):
        # 127.0.0.1 to adres WNĘTRZA kontenera - mapowanie portu prowadzi wtedy w nic.
        polecenia = [p for p in _logiczne_polecenia(self.skrypt) if "gunicorn" in p]

        self.assertEqual(len(polecenia), 1, polecenia)
        self.assertIn("--bind 0.0.0.0:8000", polecenia[0])

    def test_no_shipped_snippet_starts_gunicorn_without_a_bind(self):
        # Ta sama pułapka wraca przy każdym przepisaniu polecenia wprost do YAML-a.
        zrodla = {self.SKRYPT: self.skrypt}
        for nazwa, usluga in self.compose["services"].items():
            polecenie = usluga.get("command")
            if isinstance(polecenie, list):
                polecenie = " ".join(polecenie)
            if polecenie:
                zrodla[f"command: {nazwa}"] = polecenie

        for zrodlo, tekst in zrodla.items():
            for polecenie in _logiczne_polecenia(tekst):
                if "gunicorn" not in polecenie:
                    continue
                with self.subTest(zrodlo=zrodlo):
                    self.assertIn("--bind 0.0.0.0:8000", polecenie)

    def test_web_service_runs_the_script(self):
        polecenie = self.compose["services"]["web"]["command"]

        # Postać listy, nie łańcuch: nie przechodzi przez powłokę, więc nie ma tu
        # cytowania, które mogłoby się rozjechać.
        self.assertIsInstance(polecenie, list)
        self.assertIn("start-web.sh", " ".join(polecenie))
        # `sh <plik>`, a nie `./plik`: bit wykonywalności nie przechodzi przez
        # repozytorium klonowane na Windows.
        self.assertEqual(polecenie[0], "sh")

    def test_the_script_hands_the_process_over_to_gunicorn(self):
        # Bez `exec` PID 1 zostaje przy skrypcie i gunicorn nie dostaje sygnałów -
        # `docker compose stop` czeka wtedy na timeout i ubija kontener.
        self.assertTrue(
            any(p.startswith("exec gunicorn") for p in _logiczne_polecenia(self.skrypt)),
            _logiczne_polecenia(self.skrypt),
        )

    def test_migrations_run_before_the_server_and_can_stop_it(self):
        polecenia = _logiczne_polecenia(self.skrypt)
        migracje = next(i for i, p in enumerate(polecenia) if "migrate" in p)
        serwer = next(i for i, p in enumerate(polecenia) if "gunicorn" in p)

        self.assertLess(migracje, serwer)
        # `set -e`: nieudana migracja musi zatrzymać start, a nie wpuścić serwer
        # na bazę w niespójnym stanie.
        self.assertIn("set -e", self.skrypt)

    def test_the_script_keeps_unix_line_endings(self):
        # CRLF w skrypcie uruchamianym przez `sh` w kontenerze Linux kończy się
        # komunikatem `\r: not found` - kontener nie wstaje w ogóle.
        self.assertNotIn(CRLF, (Path(settings.BASE_DIR) / self.SKRYPT).read_bytes())

        # `core.autocrlf` jest na Windows włączone, więc sam plik nie wystarcza:
        # bez reguły git podmieniłby końce linii przy kopii roboczej.
        atrybuty = (Path(settings.BASE_DIR) / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", atrybuty)

    def test_the_script_reaches_the_image(self):
        # Wykluczenie go z kontekstu budowania znaczyłoby kontener bez punktu startu.
        ignore = (Path(settings.BASE_DIR) / ".dockerignore").read_text(encoding="utf-8")
        wzorce = {l.strip().rstrip("/") for l in ignore.splitlines() if l.strip()}

        for wzorzec in ("docker", "*.sh", "docker/start-web.sh"):
            with self.subTest(wzorzec=wzorzec):
                self.assertNotIn(wzorzec, wzorce)
