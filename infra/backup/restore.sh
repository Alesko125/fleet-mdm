#!/bin/sh
# Відновлення Fleet MDM з резервної копії.
#
#   ./infra/backup/restore.sh 20260813T120000Z
#
# Операція РУЙНІВНА: наявні дані замінюються вмістом копії. Скрипт вимагає
# явного підтвердження й зупиняє API на час відновлення, щоб застосунок не
# писав у базу, яку ми саме перезаливаємо.
set -eu

COMPOSE="${COMPOSE:-docker compose}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
POSTGRES_USER="${POSTGRES_USER:-fleet}"
POSTGRES_DB="${POSTGRES_DB:-fleet}"

if [ $# -lt 1 ]; then
    echo "Використання: $0 <мітка копії>   (напр. 20260813T120000Z)" >&2
    echo "Доступні копії:" >&2
    ls -1 "$BACKUP_DIR" 2>/dev/null >&2 || echo "  (каталог $BACKUP_DIR порожній)" >&2
    exit 1
fi

SOURCE="$BACKUP_DIR/$1"
[ -d "$SOURCE" ] || { echo "Копію не знайдено: $SOURCE" >&2; exit 1; }
[ -f "$SOURCE/database.dump" ] || { echo "Немає database.dump у $SOURCE" >&2; exit 1; }

echo "--- Маніфест копії ---"
cat "$SOURCE/MANIFEST.txt" 2>/dev/null || echo "(маніфест відсутній)"
echo "----------------------"
printf 'Замінити ПОТОЧНІ дані вмістом цієї копії? Введіть YES: '
read -r CONFIRM
[ "$CONFIRM" = "YES" ] || { echo "Скасовано."; exit 1; }

# API зупиняємо, щоб він не писав у БД під час відновлення.
echo "[restore] зупиняємо api…"
$COMPOSE stop api

echo "[restore] відновлення бази…"
# --clean --if-exists прибирає наявні обʼєкти перед відновленням.
$COMPOSE exec -T postgres pg_restore \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --clean --if-exists --no-owner \
    < "$SOURCE/database.dump"

if [ -f "$SOURCE/storage.tar.gz" ]; then
    echo "[restore] відновлення сховища артефактів…"
    $COMPOSE run --rm -T --entrypoint sh api -c \
        'rm -rf /var/lib/fleet/storage && tar xzf - -C /var/lib/fleet' \
        < "$SOURCE/storage.tar.gz"
fi

echo "[restore] піднімаємо api (міграції застосуються автоматично)…"
$COMPOSE up -d api

echo "[restore] готово. Перевірте: curl -fsS https://<домен>/health"
echo "[restore] нагадування: .env у копію не входив — переконайтеся, що"
echo "          COMMS_ENCRYPTION_KEY той самий, інакше комунікаційні дані"
echo "          лишаться нечитабельними."
