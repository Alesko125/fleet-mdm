@echo off
rem =========================================================================
rem  Fleet MDM - запуск і налаштування бекенду одним файлом.
rem
rem  Скрипт ідемпотентний: перший запуск усе налаштовує, наступні просто
rem  піднімають сервер. Нічого не перезаписує без питання.
rem
rem  Використання:
rem     start-backend.bat            налаштувати (за потреби) і запустити
rem     start-backend.bat setup      лише налаштувати, не запускати
rem     start-backend.bat test       прогнати тести
rem     start-backend.bat reset      прибрати локальну конфігурацію та БД
rem     start-backend.bat stop       зупинити службові контейнери
rem =========================================================================
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1

set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"
set "VENV=%BACKEND%\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "ENVFILE=%BACKEND%\.env"
set "STAMP=%VENV%\.deps-installed"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=run"

echo.
echo ==========================================
echo   Fleet MDM - бекенд
echo ==========================================
echo.

rem Паролі задаємо до розгалуження за режимом: у docker-compose.yml вони
rem оголошені обов'язковими ^(${VAR:?...}^), і без них падає навіть розбір
rem файлу - зокрема у reset/stop, які теж кличуть compose.
set "POSTGRES_PASSWORD=fleet-local-dev"
set "POSTGRES_APP_PASSWORD=fleet-local-dev"
set "REDIS_PASSWORD=fleet-local-dev"

if /i "%MODE%"=="reset" goto do_reset
if /i "%MODE%"=="stop" goto do_stop

rem ---------------------------------------------------------------- Python --
call :find_python
if errorlevel 1 goto fail

rem ------------------------------------------------------------------ venv --
if not exist "%PY%" (
    echo [1/7] Створюємо віртуальне середовище...
    %BOOTPY% -m venv "%VENV%"
    if errorlevel 1 (
        echo [ПОМИЛКА] Не вдалося створити venv у "%VENV%".
        goto fail
    )
) else (
    echo [1/7] Віртуальне середовище на місці.
)

rem -------------------------------------------------------------- залежності --
rem Позначка зберігає хеш обох requirements. Порівнюємо саме хеш, а не дати:
rem git-checkout оновлює час модифікації навіть коли вміст не змінився, і
rem перевірка за датою перевстановлювала все на кожному запуску.
rem Хеш рахуємо у тимчасовий файл: підстановка через `for /f` вимагала б
rem екранування лапок усередині лапок, а це найкрихкіша частина cmd.
set "HASHTMP=%TEMP%\fleet_deps_hash.txt"
pushd "%BACKEND%"
"%PY%" -c "import hashlib,pathlib;h=hashlib.sha256();[h.update(pathlib.Path(p).read_bytes()) for p in ['requirements.txt','requirements-dev.txt']];print(h.hexdigest())" > "%HASHTMP%"
popd
set "DEPS_HASH="
set /p DEPS_HASH=<"%HASHTMP%"
del "%HASHTMP%" >nul 2>&1

set "NEED_DEPS=1"
if exist "%STAMP%" (
    set "OLD_HASH="
    set /p OLD_HASH=<"%STAMP%"
    if /i "!OLD_HASH!"=="!DEPS_HASH!" set "NEED_DEPS="
)

if defined NEED_DEPS (
    echo [2/7] Встановлюємо залежності ^(це може зайняти хвилину^)...
    "%PY%" -m pip install --upgrade pip --quiet
    "%PY%" -m pip install -r "%BACKEND%\requirements-dev.txt" --quiet
    if errorlevel 1 (
        echo [ПОМИЛКА] Не вдалося встановити залежності.
        goto fail
    )
    > "%STAMP%" echo !DEPS_HASH!
) else (
    echo [2/7] Залежності актуальні.
)

rem Чи доступний Docker: від цього залежить лише конфігурація за
rem замовчуванням для НОВОГО .env.
set "HAVE_DOCKER="
docker info >nul 2>&1
if not errorlevel 1 set "HAVE_DOCKER=1"

rem DSN за замовчуванням для НОВОЇ конфігурації. Якщо .env уже є, справжні
rem значення прочитаємо з нього нижче: конфігурація головніша за здогадки.
if defined HAVE_DOCKER (
    set "DB_URL=postgresql+asyncpg://fleet:fleet-local-dev@127.0.0.1:5432/fleet"
    set "RD_URL=redis://:fleet-local-dev@127.0.0.1:6379/0"
    set "RL=true"
) else (
    set "DB_URL=sqlite+aiosqlite:///./fleet-local.db"
    set "RD_URL=redis://127.0.0.1:6379/0"
    set "RL=false"
)

rem ------------------------------------------------------------ конфігурація --
rem Спершу конфігурація, потім служби: який саме сервер БД піднімати -
rem вирішує вже наявний .env, а не те, чи запущено зараз Docker.
if not exist "%ENVFILE%" (
    echo [3/7] Створюємо "%ENVFILE%" зі згенерованими секретами...
    call :write_env
    if errorlevel 1 goto fail
) else (
    echo [3/7] Конфігурація вже існує - лишаємо без змін.
    call :read_env
)

rem SQLite не потребує жодних контейнерів; PostgreSQL - потребує.
set "NEED_DOCKER="
if not "!DB_URL:postgres=!"=="!DB_URL!" set "NEED_DOCKER=1"

rem --------------------------------------------------------- служби у Docker --
rem Без Redis не працюють вхід ^(fail-closed rate limit^), автентифікація
rem агентів і черга команд.
if defined NEED_DOCKER (
    if not defined HAVE_DOCKER (
        echo.
        echo [ПОМИЛКА] "%ENVFILE%" вказує на PostgreSQL, але Docker недоступний.
        echo            База не запущена - міграції не пройдуть.
        echo.
        echo            Що зробити ^(одне з двох^):
        echo.
        echo              1^) Запустіть Docker Desktop, дочекайтеся, поки значок
        echo                 у треї стане зеленим, і запустіть цей файл знову.
        echo.
        echo              2^) Перейдіть на локальний SQLite без Docker:
        echo                     start-backend.bat reset
        echo                 Це видалить локальну конфігурацію та дані й
        echo                 згенерує все наново.
        echo.
        goto fail
    )
    echo [4/7] Піднімаємо PostgreSQL і Redis...
    call :start_services
    if errorlevel 1 goto fail
) else (
    echo [4/7] Режим SQLite - контейнери не потрібні.
    echo.
    echo        УВАГА: без Redis не працюватимуть вхід у панель,
    echo        автентифікація агентів і черга команд. Обмеження частоти
    echo        вимкнено, щоб панель хоча б відкривалася.
    echo        Для повної функціональності запустіть Docker Desktop
    echo        і виконайте start-backend.bat reset.
    echo.
)

rem ----------------------------------------------------------------- панель --
rem Без зібраної панелі "/" показував би лише довідку. Збираємо один раз,
rem далі пропускаємо - перезбірка потрібна лише коли змінили фронтенд.
if not exist "%ROOT%frontend\dist\index.html" (
    where npm >nul 2>&1
    if errorlevel 1 (
        echo [5/7] Node.js не знайдено - панель не збирається.
        echo        Бекенд працюватиме, "/" покаже довідку з посиланнями.
    ) else (
        echo [5/7] Збираємо панель ^(один раз, ~1 хвилина^)...
        pushd "%ROOT%frontend"
        if not exist node_modules (
            call npm ci --no-audit --no-fund >nul 2>&1
            if errorlevel 1 call npm install --no-audit --no-fund >nul 2>&1
        )
        call npm run build >nul 2>&1
        if errorlevel 1 (
            echo        [УВАГА] Панель не зібралася. Бекенд усе одно запуститься.
        ) else (
            echo        Панель зібрано.
        )
        popd
    )
) else (
    echo [5/7] Панель уже зібрана.
)

rem --------------------------------------------------------------- міграції --
echo [6/7] Застосовуємо міграції...
pushd "%BACKEND%"
"%PY%" -m alembic upgrade head
set "MIGRC=%ERRORLEVEL%"
popd
if not "%MIGRC%"=="0" (
    echo [ПОМИЛКА] Міграції не застосувалися. Подробиці вище.
    goto fail
)

if /i "%MODE%"=="setup" (
    echo.
    echo [7/7] Налаштування завершено. Запуск: start-backend.bat
    goto done
)

if /i "%MODE%"=="test" (
    echo [7/7] Запускаємо тести...
    pushd "%BACKEND%"
    "%PY%" -m pytest -q
    set "RC=!ERRORLEVEL!"
    popd
    goto done
)

rem ----------------------------------------------------------------- запуск --
echo [7/7] Запускаємо сервер...
echo.
echo    ПАНЕЛЬ     : http://127.0.0.1:8000
echo    Документація API : http://127.0.0.1:8000/docs
echo    Стан       : http://127.0.0.1:8000/health
call :show_admin
echo.
echo    Зупинити: Ctrl+C
echo.

rem Відкриваємо браузер трохи згодом, коли сервер уже слухає порт.
start "" /b cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:8000"

pushd "%BACKEND%"
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
popd
goto done


rem =========================================================================
rem  Підпрограми
rem =========================================================================

:find_python
set "BOOTPY="
if exist "%PY%" (
    set "BOOTPY=%PY%"
    exit /b 0
)
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "BOOTPY=py -3"
    exit /b 0
)
python --version >nul 2>&1
if not errorlevel 1 (
    set "BOOTPY=python"
    exit /b 0
)
echo [ПОМИЛКА] Python 3.10+ не знайдено.
echo            Встановіть з https://www.python.org/downloads/
echo            і поставте галочку "Add Python to PATH".
exit /b 1

:start_services
rem Піднімаємо лише сховища; сам API запускаємо з venv, щоб працював reload.
pushd "%ROOT%"
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres redis
if errorlevel 1 (
    echo.
    echo [ПОМИЛКА] Не вдалося підняти контейнери. Повідомлення - вище.
    popd
    exit /b 1
)
popd

echo        Чекаємо на готовність PostgreSQL...
rem Перевіряємо справжнім підключенням тими самими обліковими даними, якими
rem працюватиме застосунок. `docker compose exec pg_isready` для цього не
rem годиться: він залежить від розбору всього compose-файлу й мовчки падає
rem з причин, не пов'язаних зі станом бази.
pushd "%BACKEND%"
"%PY%" scripts\wait_for_db.py --url "!DB_URL!" --timeout 90
set "DBRC=!ERRORLEVEL!"
popd
if not "!DBRC!"=="0" (
    echo.
    echo        Стан контейнерів:
    docker ps -a --filter "name=fleet-mdm" --format "          {{.Names}}  {{.Status}}"
    echo.
    echo        Логи Redis ^(останні рядки^):
    docker logs --tail 5 fleet-mdm-redis-1 2>^&1
    exit /b 1
)
echo        Служби готові.
exit /b 0

:read_env
rem Читаємо DSN із наявного .env: саме за ним працюватимуть alembic і uvicorn,
rem тож усі подальші рішення мають спиратися на нього, а не на замовчування.
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ENVFILE%") do (
    if /i "%%A"=="DATABASE_URL" set "DB_URL=%%B"
    if /i "%%A"=="REDIS_URL" set "RD_URL=%%B"
)
exit /b 0

:write_env
rem Секрети генеруються локально; у репозиторій .env не потрапляє (.gitignore).
"%PY%" -c "import secrets,sys;print('\n'.join(secrets.token_urlsafe(48) for _ in range(4)))" > "%TEMP%\fleet_secrets.txt"
if errorlevel 1 exit /b 1
set /p JWT_S=<"%TEMP%\fleet_secrets.txt"
call :read_line "%TEMP%\fleet_secrets.txt" 2 HASH_S
call :read_line "%TEMP%\fleet_secrets.txt" 3 ADMIN_P
call :read_line "%TEMP%\fleet_secrets.txt" 4 COMMS_S
del "%TEMP%\fleet_secrets.txt" >nul 2>&1

>  "%ENVFILE%" echo # Локальна конфігурація розробки. Створена start-backend.bat.
>> "%ENVFILE%" echo # НЕ для production: там свої секрети і APP_ENV=production.
>> "%ENVFILE%" echo # Файл у .gitignore і в репозиторій не потрапляє.
>> "%ENVFILE%" echo.
>> "%ENVFILE%" echo APP_ENV=development
>> "%ENVFILE%" echo LOG_FORMAT=console
>> "%ENVFILE%" echo PUBLIC_BASE_URL=http://127.0.0.1:8000
>> "%ENVFILE%" echo CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
>> "%ENVFILE%" echo.
>> "%ENVFILE%" echo DATABASE_URL=!DB_URL!
>> "%ENVFILE%" echo REDIS_URL=!RD_URL!
>> "%ENVFILE%" echo.
>> "%ENVFILE%" echo JWT_SECRET=!JWT_S!
>> "%ENVFILE%" echo TOKEN_HASH_SECRET=!HASH_S!
>> "%ENVFILE%" echo COMMS_ENCRYPTION_KEY=!COMMS_S!
>> "%ENVFILE%" echo.
>> "%ENVFILE%" echo BOOTSTRAP_ADMIN_EMAIL=admin@example.com
>> "%ENVFILE%" echo BOOTSTRAP_ADMIN_PASSWORD=!ADMIN_P!
>> "%ENVFILE%" echo BOOTSTRAP_ADMIN_NAME=Локальний адміністратор
>> "%ENVFILE%" echo.
>> "%ENVFILE%" echo RATE_LIMIT_ENABLED=!RL!
echo        Секрети згенеровано, пароль адміністратора - у "%ENVFILE%".
exit /b 0

:read_line
rem %1 - файл, %2 - номер рядка, %3 - куди покласти
set "_n=0"
for /f "usebackq delims=" %%L in ("%~1") do (
    set /a _n+=1
    if !_n! EQU %~2 (
        set "%~3=%%L"
        exit /b 0
    )
)
exit /b 0

:show_admin
if not exist "%ENVFILE%" exit /b 0
for /f "usebackq tokens=1,* delims==" %%A in ("%ENVFILE%") do (
    if /i "%%A"=="BOOTSTRAP_ADMIN_EMAIL" echo    Логін      : %%B
    if /i "%%A"=="BOOTSTRAP_ADMIN_PASSWORD" echo    Пароль     : %%B
)
exit /b 0

:do_reset
echo Прибираємо локальну конфігурацію...
if exist "%ENVFILE%" del "%ENVFILE%" && echo   - видалено backend\.env
if exist "%BACKEND%\fleet-local.db" del "%BACKEND%\fleet-local.db" && echo   - видалено локальну БД
docker info >nul 2>&1
if not errorlevel 1 (
    pushd "%ROOT%"
    docker compose -f docker-compose.yml -f docker-compose.dev.yml down -v
    set "DOWNRC=!ERRORLEVEL!"
    popd
    if "!DOWNRC!"=="0" (
        echo   - зупинено контейнери й видалено томи
    ) else (
        echo.
        echo   [УВАГА] Не вдалося прибрати контейнери й томи ^(причина вище^).
        echo           Дані у томах лишилися.
        echo.
    )
) else (
    echo.
    echo   [УВАГА] Docker недоступний - томи з даними НЕ видалено.
    echo           Якщо пізніше запустите Docker Desktop, у базі лишиться
    echo           адміністратор зі старим паролем, а у новому .env буде
    echo           інший - увійти не вдасться. Тоді виконайте reset ще раз
    echo           із запущеним Docker Desktop.
    echo.
)
echo Готово. Наступний запуск налаштує все наново.
goto done

:do_stop
echo Зупиняємо службові контейнери...
pushd "%ROOT%"
docker compose -f docker-compose.yml -f docker-compose.dev.yml stop postgres redis >nul 2>&1
popd
echo Готово.
goto done

:fail
echo.
echo Запуск перервано. Прочитайте повідомлення вище.
echo.
pause
endlocal
exit /b 1

:done
endlocal
exit /b 0
