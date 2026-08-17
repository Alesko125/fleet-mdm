#!/usr/bin/env bash
# =========================================================================
#  Fleet MDM — запуск і налаштування бекенду одним файлом (Ubuntu/Debian).
#
#  Розрахований на чисту систему: якщо чогось бракує (Python, Docker,
#  Node.js) — скрипт запропонує це встановити. Нічого не ставиться мовчки:
#  перелік пакетів показується й потребує підтвердження.
#
#  Скрипт ідемпотентний: перший запуск усе налаштовує, наступні просто
#  піднімають сервер. Наявну конфігурацію не перезаписує.
#
#  Використання:
#      ./start-backend.sh              налаштувати (за потреби) і запустити
#      ./start-backend.sh setup        лише налаштувати, не запускати
#      ./start-backend.sh test         прогнати тести
#      ./start-backend.sh reset        прибрати локальну конфігурацію та БД
#      ./start-backend.sh stop         зупинити службові контейнери
#
#  Прапорці:
#      -y, --yes       не питати підтвердження на встановлення пакетів
#          --no-docker працювати на SQLite, не ставити й не піднімати Docker
#          --host ADDR адреса прослуховування (типово 127.0.0.1)
#          --port N    порт (типово 8000)
# =========================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
VENV="$BACKEND/.venv"
PY="$VENV/bin/python"
ENVFILE="$BACKEND/.env"
STAMP="$VENV/.deps-installed"

MODE="run"
ASSUME_YES=0
WANT_DOCKER=1
HOST="127.0.0.1"
PORT="8000"

# --------------------------------------------------------------- вивід ---
BOLD=""; RED=""; YELLOW=""; GREEN=""; RESET=""
if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"; RED="$(printf '\033[31m')"
    YELLOW="$(printf '\033[33m')"; GREEN="$(printf '\033[32m')"
    RESET="$(printf '\033[0m')"
fi

step() { printf '%s[%s/8]%s %s\n' "$BOLD" "$1" "$RESET" "$2"; }
info() { printf '        %s\n' "$1"; }
warn() { printf '%s[УВАГА]%s %s\n' "$YELLOW" "$RESET" "$1"; }
die()  { printf '%s[ПОМИЛКА]%s %s\n' "$RED" "$RESET" "$1" >&2; exit 1; }

# Будь-яка неперехоплена помилка має пояснювати, де саме впало: інакше
# `set -e` просто мовчки завершує скрипт.
trap 'die "перервано на рядку $LINENO (команда: $BASH_COMMAND)"' ERR

# ------------------------------------------------------------ аргументи ---
while [ $# -gt 0 ]; do
    case "$1" in
        run|setup|test|reset|stop) MODE="$1" ;;
        -y|--yes) ASSUME_YES=1 ;;
        --no-docker) WANT_DOCKER=0 ;;
        --host) HOST="${2:?--host потребує значення}"; shift ;;
        --port) PORT="${2:?--port потребує значення}"; shift ;;
        -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "невідомий аргумент: $1 (див. --help)" ;;
    esac
    shift
done

printf '\n%s==========================================%s\n' "$BOLD" "$RESET"
printf '%s  Fleet MDM — бекенд%s\n' "$BOLD" "$RESET"
printf '%s==========================================%s\n\n' "$BOLD" "$RESET"

# =========================================================================
#  Системне оточення
# =========================================================================

detect_os() {
    [ -r /etc/os-release ] || die "не знайдено /etc/os-release — скрипт розрахований на Ubuntu/Debian."
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-}"
    OS_LIKE="${ID_LIKE:-}"
    OS_CODENAME="${VERSION_CODENAME:-}"
    case "$OS_ID $OS_LIKE" in
        *ubuntu*|*debian*) : ;;
        *) die "підтримуються Ubuntu та Debian, знайдено «${PRETTY_NAME:-$OS_ID}»." ;;
    esac
}

# Права на встановлення пакетів. Під root sudo не потрібен і може бути відсутнім.
setup_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        SUDO=""
        return
    fi
    command -v sudo >/dev/null 2>&1 || die "потрібні права root: встановіть sudo або запустіть від root."
    SUDO="sudo"
}

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    if [ ! -t 0 ]; then
        die "потрібне підтвердження, але введення недоступне. Перезапустіть із --yes."
    fi
    printf '        Продовжити? [y/N] '
    local answer
    read -r answer
    case "$answer" in [yY]|[yY][eE][sS]|[тТ]|[тТ][аА][кК]) return 0 ;; *) return 1 ;; esac
}

APT_UPDATED=0
apt_update_once() {
    [ "$APT_UPDATED" -eq 1 ] && return 0
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get update -qq
    APT_UPDATED=1
}

# Ставить лише те, чого справді бракує: повторний запуск нічого не робить.
apt_install() {
    local missing=()
    for pkg in "$@"; do
        dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed" || missing+=("$pkg")
    done
    [ ${#missing[@]} -eq 0 ] && return 0

    info "бракує пакетів: ${missing[*]}"
    confirm || die "без цих пакетів продовжити не можна."
    apt_update_once
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq "${missing[@]}"
}

ensure_base_packages() {
    # python3-venv в Ubuntu окремим пакетом: без нього `python3 -m venv` падає
    # з підказкою поставити його — і скрипт зупинявся б на порожньому місці.
    apt_install python3 python3-venv curl ca-certificates
}

# ------------------------------------------------------------- Docker ----
# Змінні, які docker-compose.yml вимагає ще на етапі розбору.
compose_env() {
    printf 'POSTGRES_PASSWORD=%s POSTGRES_APP_PASSWORD=%s REDIS_PASSWORD=%s'         "$POSTGRES_PASSWORD" "$POSTGRES_APP_PASSWORD" "$REDIS_PASSWORD"
}

docker_ready() {
    command -v docker >/dev/null 2>&1 || return 1
    if docker info >/dev/null 2>&1; then
        DOCKER="docker"
        # shellcheck disable=SC2086
        DOCKER_ENV="env $(compose_env) docker"
        return 0
    fi
    # Без членства у групі docker сокет доступний лише через sudo. Групу
    # навмисно не чіпаємо: членство в ній рівносильне правам root, і таке
    # рішення має ухвалювати людина, а не скрипт запуску.
    if [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then
        DOCKER="$SUDO docker"
        # shellcheck disable=SC2086
        DOCKER_ENV="$SUDO env $(compose_env) docker"
        return 0
    fi
    return 1
}

install_docker() {
    info "Docker не знайдено. Буде додано офіційний репозиторій Docker і встановлено"
    info "docker-ce, docker-ce-cli, containerd.io, docker-buildx-plugin, docker-compose-plugin."
    info "Причина саме офіційного репозиторію: у стандартному compose буває застарілим,"
    info "а docker-compose.yml використовує синтаксис, доданий у Compose 2.24."
    confirm || return 1

    apt_install curl ca-certificates gnupg
    local repo="ubuntu"
    case "$OS_ID" in debian) repo="debian" ;; esac

    $SUDO install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/$repo/gpg" \
        | $SUDO tee /etc/apt/keyrings/docker.asc >/dev/null
    $SUDO chmod a+r /etc/apt/keyrings/docker.asc

    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
        "$(dpkg --print-architecture)" "$repo" "$OS_CODENAME" \
        | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null

    APT_UPDATED=0
    apt_update_once
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    # У контейнерах і мінімальних образах systemd немає — це не привід падати.
    if command -v systemctl >/dev/null 2>&1; then
        $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
    fi
    docker_ready
}

# -------------------------------------------------------------- Node.js --
node_ok() {
    command -v node >/dev/null 2>&1 || return 1
    local major
    major="$(node -v | sed 's/^v\([0-9]*\).*/\1/')"
    [ "${major:-0}" -ge 18 ]
}

ensure_node() {
    node_ok && command -v npm >/dev/null 2>&1 && return 0

    apt_install nodejs npm || true
    if node_ok && command -v npm >/dev/null 2>&1; then
        return 0
    fi

    info "Node.js із репозиторію системи застарілий або відсутній (потрібен 18+)."
    info "Можна підключити NodeSource і поставити Node.js 20."
    confirm || return 1

    curl -fsSL https://deb.nodesource.com/setup_20.x | $SUDO -E bash - >/dev/null
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq nodejs
    node_ok && command -v npm >/dev/null 2>&1
}

# =========================================================================
#  Режими обслуговування
# =========================================================================

# Паролі задаємо до розгалуження за режимом: у docker-compose.yml вони
# оголошені обов'язковими (${VAR:?...}), і без них падає навіть розбір
# файлу — зокрема у reset/stop, які теж кличуть compose.
export POSTGRES_PASSWORD="fleet-local-dev"
export POSTGRES_APP_PASSWORD="fleet-local-dev"
export REDIS_PASSWORD="fleet-local-dev"

compose() {
    # Паролі передаємо через `env` явно, а не покладаємось на export: коли
    # доступ до сокета лише через sudo, той за замовчуванням стирає оточення
    # (env_reset у sudoers) — і compose падав на розборі файлу, бо змінні
    # оголошені обов'язковими. `sudo env VAR=... docker` працює скрізь,
    # на відміну від -E/--preserve-env, які залежать від налаштувань sudoers.
    #
    # Без subshell і cd: --project-directory дає той самий ефект, а ERR-trap
    # у підоболонці друкував помилку двічі.
    # shellcheck disable=SC2086
    $DOCKER_ENV compose         --project-directory "$ROOT"         -f "$ROOT/docker-compose.yml"         -f "$ROOT/docker-compose.dev.yml"         "$@"
}

do_reset() {
    echo "Прибираємо локальну конфігурацію..."
    [ -f "$ENVFILE" ] && { rm -f "$ENVFILE"; info "- видалено backend/.env"; }
    [ -f "$BACKEND/fleet-local.db" ] && { rm -f "$BACKEND/fleet-local.db"; info "- видалено локальну БД"; }

    setup_sudo
    if docker_ready; then
        if compose down -v; then
            info "- зупинено контейнери й видалено томи"
        else
            warn "не вдалося прибрати контейнери й томи (причина вище). Дані у томах лишилися."
        fi
    else
        warn "Docker недоступний — томи з даними НЕ видалено.
        Якщо пізніше запустите Docker, у базі лишиться адміністратор зі
        старим паролем, а у новому .env буде інший — увійти не вдасться.
        Тоді виконайте reset ще раз із запущеним Docker."
    fi
    echo "Готово. Наступний запуск налаштує все наново."
}

do_stop() {
    setup_sudo
    docker_ready || die "Docker недоступний — зупиняти нічого."
    echo "Зупиняємо службові контейнери..."
    compose stop postgres redis >/dev/null
    echo "Готово."
}

detect_os
case "$MODE" in
    reset) do_reset; exit 0 ;;
    stop)  do_stop;  exit 0 ;;
esac

# =========================================================================
#  Налаштування й запуск
# =========================================================================

# ------------------------------------------------- [1/8] системні пакети --
step 1 "Перевіряємо системні пакети..."
setup_sudo
ensure_base_packages
info "Python: $(python3 --version 2>&1)"

DOCKER=""
DOCKER_ENV=""
HAVE_DOCKER=0
if [ "$WANT_DOCKER" -eq 1 ]; then
    if docker_ready || install_docker; then
        HAVE_DOCKER=1
        info "Docker: $($DOCKER --version 2>&1 | head -1)"
        [ "$DOCKER" != "docker" ] && info "доступ до Docker лише через sudo — так і використовуємо."
    else
        warn "Docker недоступний — далі працюватимемо на SQLite."
    fi
else
    info "Docker вимкнено прапорцем --no-docker."
fi

# ------------------------------------------------------------ [2/8] venv --
if [ ! -x "$PY" ]; then
    step 2 "Створюємо віртуальне середовище..."
    python3 -m venv "$VENV"
else
    step 2 "Віртуальне середовище на місці."
fi

# ------------------------------------------------------ [3/8] залежності --
# Позначка зберігає хеш обох requirements. Порівнюємо саме хеш, а не дати:
# git-checkout оновлює час модифікації навіть коли вміст не змінився.
DEPS_HASH="$(cat "$BACKEND/requirements.txt" "$BACKEND/requirements-dev.txt" | sha256sum | cut -d' ' -f1)"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$DEPS_HASH" ]; then
    step 3 "Встановлюємо залежності (це може зайняти хвилину)..."
    "$PY" -m pip install --upgrade pip --quiet
    "$PY" -m pip install -r "$BACKEND/requirements-dev.txt" --quiet
    printf '%s\n' "$DEPS_HASH" > "$STAMP"
else
    step 3 "Залежності актуальні."
fi

# ----------------------------------------------------- [4/8] конфігурація --
# Спершу конфігурація, потім служби: який саме сервер БД піднімати —
# вирішує вже наявний .env, а не те, чи запущено зараз Docker.
if [ "$HAVE_DOCKER" -eq 1 ]; then
    DB_URL="postgresql+asyncpg://fleet:fleet-local-dev@127.0.0.1:5432/fleet"
    RD_URL="redis://:fleet-local-dev@127.0.0.1:6379/0"
    RL="true"
else
    DB_URL="sqlite+aiosqlite:///./fleet-local.db"
    RD_URL="redis://127.0.0.1:6379/0"
    RL="false"
fi

read_env_value() {
    grep -E "^$1=" "$ENVFILE" | tail -1 | cut -d= -f2- || true
}

if [ ! -f "$ENVFILE" ]; then
    step 4 "Створюємо backend/.env зі згенерованими секретами..."
    mapfile -t SECRETS < <("$PY" -c "import secrets;[print(secrets.token_urlsafe(48)) for _ in range(4)]")
    cat > "$ENVFILE" <<ENVEOF
# Локальна конфігурація розробки. Створена start-backend.sh.
# НЕ для production: там свої секрети і APP_ENV=production.
# Файл у .gitignore і в репозиторій не потрапляє.

APP_ENV=development
LOG_FORMAT=console
PUBLIC_BASE_URL=http://$HOST:$PORT
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173

DATABASE_URL=$DB_URL
REDIS_URL=$RD_URL

JWT_SECRET=${SECRETS[0]}
TOKEN_HASH_SECRET=${SECRETS[1]}
COMMS_ENCRYPTION_KEY=${SECRETS[3]}

BOOTSTRAP_ADMIN_EMAIL=admin@example.com
BOOTSTRAP_ADMIN_PASSWORD=${SECRETS[2]}
BOOTSTRAP_ADMIN_NAME=Локальний адміністратор

RATE_LIMIT_ENABLED=$RL
ENVEOF
    chmod 600 "$ENVFILE"
    info "секрети згенеровано, пароль адміністратора — у backend/.env"
else
    step 4 "Конфігурація вже існує — лишаємо без змін."
    # Саме за цим файлом працюватимуть alembic і uvicorn, тож усі подальші
    # рішення мають спиратися на нього, а не на замовчування.
    DB_URL="$(read_env_value DATABASE_URL)"
    RD_URL="$(read_env_value REDIS_URL)"
fi

# --------------------------------------------------------- [5/8] служби ---
case "$DB_URL" in
    *postgres*) NEED_DOCKER=1 ;;
    *) NEED_DOCKER=0 ;;
esac

if [ "$NEED_DOCKER" -eq 1 ]; then
    if [ "$HAVE_DOCKER" -eq 0 ]; then
        die "backend/.env вказує на PostgreSQL, але Docker недоступний.
           База не запущена — міграції не пройдуть.

           Що зробити (одне з двох):
             1) Дайте скрипту встановити чи запустити Docker і повторіть.
             2) Перейдіть на локальний SQLite:
                    ./start-backend.sh reset && ./start-backend.sh --no-docker
                Це видалить локальну конфігурацію та дані й згенерує все наново."
    fi
    step 5 "Піднімаємо PostgreSQL і Redis..."
    compose up -d postgres redis
    info "чекаємо на готовність PostgreSQL..."
    # Перевіряємо справжнім підключенням тими самими обліковими даними, якими
    # працюватиме застосунок: `pg_isready` через compose мовчки падає з причин,
    # не пов'язаних зі станом бази.
    if ! (cd "$BACKEND" && "$PY" scripts/wait_for_db.py --url "$DB_URL" --timeout 90); then
        echo
        info "стан контейнерів:"
        compose ps || true
        info "останні рядки логів Redis:"
        compose logs --tail 10 redis || true
        die "база не піднялася."
    fi
    info "служби готові."
else
    step 5 "Режим SQLite — контейнери не потрібні."
    warn "без Redis не працюватимуть вхід у панель, автентифікація агентів
        і черга команд. Обмеження частоти вимкнено, щоб панель хоча б
        відкривалася. Для повної функціональності потрібен Docker."
fi

# --------------------------------------------------------- [6/8] панель ---
# Без зібраної панелі «/» показував би лише довідку. Збираємо один раз,
# далі пропускаємо — перезбірка потрібна, лише коли змінили фронтенд.
if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
    if ensure_node; then
        step 6 "Збираємо панель (один раз, ~1 хвилина)..."
        (
            cd "$ROOT/frontend"
            [ -d node_modules ] || npm ci --no-audit --no-fund >/dev/null 2>&1 \
                || npm install --no-audit --no-fund >/dev/null 2>&1
            npm run build >/dev/null 2>&1
        ) && info "панель зібрано." || warn "панель не зібралася — бекенд усе одно запуститься."
    else
        step 6 "Node.js недоступний — панель не збирається."
        info "бекенд працюватиме, «/» покаже довідку з посиланнями."
    fi
else
    step 6 "Панель уже зібрана."
fi

# ------------------------------------------------------- [7/8] міграції ---
step 7 "Застосовуємо міграції..."
(cd "$BACKEND" && "$PY" -m alembic upgrade head) || die "міграції не застосувалися. Подробиці вище."

# ---------------------------------------------------------- [8/8] режим ---
case "$MODE" in
    setup)
        printf '\n'
        step 8 "Налаштування завершено. Запуск: ./start-backend.sh"
        exit 0
        ;;
    test)
        step 8 "Запускаємо тести..."
        (cd "$BACKEND" && "$PY" -m pytest -q)
        exit $?
        ;;
esac

step 8 "Запускаємо сервер..."
printf '\n'
printf '   ПАНЕЛЬ           : http://%s:%s\n' "$HOST" "$PORT"
printf '   Документація API : http://%s:%s/docs\n' "$HOST" "$PORT"
printf '   Стан             : http://%s:%s/health\n' "$HOST" "$PORT"
if [ -f "$ENVFILE" ]; then
    printf '   Логін            : %s\n' "$(read_env_value BOOTSTRAP_ADMIN_EMAIL)"
    printf '   Пароль           : %s\n' "$(read_env_value BOOTSTRAP_ADMIN_PASSWORD)"
fi
printf '\n   Зупинити: Ctrl+C\n\n'

case "$HOST" in
    127.0.0.1|localhost|::1) RELOAD="--reload" ;;
    *)
        RELOAD=""
        warn "сервер слухає $HOST без TLS. Назовні його має віддавати nginx
        із сертифікатом — див. docs/DEPLOYMENT.md §3."
        ;;
esac

cd "$BACKEND"
# shellcheck disable=SC2086
exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" $RELOAD
