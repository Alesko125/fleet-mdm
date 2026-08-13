#!/bin/sh
# Резервна копія Fleet MDM.
#
# Копіюється все, без чого відновлення неможливе:
#   1. дамп PostgreSQL (усі дані: пристрої, команди, аудит);
#   2. сховище артефактів (APK та файли передач);
#   3. МАНІФЕСТ із версією схеми — щоб при відновленні звірити міграції.
#
# Секрети (.env) НЕ потрапляють у копію свідомо: інакше вкрадений архів
# давав би і дані, і ключі для їх розшифрування. Зберігайте .env окремо —
# без COMMS_ENCRYPTION_KEY зашифровані комунікаційні дані не прочитати
# навіть із цього дампа, і це навмисна властивість.
set -eu

COMPOSE="${COMPOSE:-docker compose}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_DIR/$STAMP"

POSTGRES_USER="${POSTGRES_USER:-fleet}"
POSTGRES_DB="${POSTGRES_DB:-fleet}"

mkdir -p "$TARGET"
echo "[backup] каталог: $TARGET"

# --- 1. база даних ---------------------------------------------------------
# Дамп робиться роллю-власником: їй видно всі таблиці.
echo "[backup] дамп PostgreSQL…"
$COMPOSE exec -T postgres pg_dump \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --format=custom \
    --no-owner \
    > "$TARGET/database.dump"

# --- 2. сховище артефактів -------------------------------------------------
echo "[backup] сховище артефактів…"
$COMPOSE exec -T api tar czf - -C /var/lib/fleet storage \
    > "$TARGET/storage.tar.gz"

# --- 3. маніфест -----------------------------------------------------------
SCHEMA_VERSION="$($COMPOSE exec -T api alembic current 2>/dev/null | tail -1 | tr -d '\r')"
cat > "$TARGET/MANIFEST.txt" <<EOF
Fleet MDM backup
created_at:     $STAMP
schema_version: $SCHEMA_VERSION
database:       database.dump (pg_dump --format=custom)
storage:        storage.tar.gz (/var/lib/fleet/storage)

УВАГА: .env у копію не входить. Зберігайте секрети окремо.
Без COMMS_ENCRYPTION_KEY комунікаційні дані з цього дампа не розшифрувати.
Відновлення: infra/backup/restore.sh $STAMP
EOF

echo "[backup] готово:"
ls -la "$TARGET"
