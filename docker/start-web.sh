#!/bin/sh
# Start serwera WWW w kontenerze.
#
# Dlaczego osobny plik, a nie `command` w docker-compose.yml: zwinięty blok YAML
# (`>`) scala tylko linie o tym samym wcięciu, a głębiej wcięte ZACHOWUJE razem
# ze znakami nowej linii. Rozpisane tam polecenie gunicorna rozpadało się więc na
# osobne linie - `sh -c` uruchamiał samo `gunicorn config.wsgi:application`, bez
# flag z następnych linii, i serwer słuchał na domyślnym 127.0.0.1:8000. To adres
# WNĘTRZA kontenera, więc mapowanie portu prowadziło w nic, a przeglądarka
# dostawała ERR_EMPTY_RESPONSE. Tu newline nic nie psuje: każda linia to polecenie,
# a kontynuacje mają jawny `\`.
set -e

# Migracje przed startem serwera, w JEDNEJ usłudze. Worker ich nie uruchamia -
# dwa procesy startujące je równolegle na tej samej bazie to wyścig.
python manage.py migrate --noinput

# `exec` zastępuje proces skryptu: gunicorn dostaje PID 1, a z nim sygnały od
# Dockera. Bez tego `docker compose stop` czeka na timeout i ubija kontener.
#
# `--bind 0.0.0.0:8000` - wszystkie interfejsy, inaczej ruch z gospodarza nie wchodzi.
# `--timeout 180`, bo pojedyncze żądanie czeka na odpowiedź Google albo na skan
# strony. Domyślne 30 s zabijałoby je w połowie.
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 3 \
    --timeout 180 \
    --access-logfile - \
    --error-logfile -
