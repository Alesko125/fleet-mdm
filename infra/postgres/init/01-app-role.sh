#!/bin/sh
# Створює роль застосунку з мінімально потрібними правами.
#
# Власником схеми лишається $POSTGRES_USER: під ним виконуються міграції.
# Застосунок у робочому режимі ходить окремою роллю $POSTGRES_APP_USER, яка
# вміє читати й змінювати дані, але не вміє змінювати схему й не має права
# створювати нові обʼєкти. Так помилка чи SQL-ін'єкція в коді не перетворюється
# на DROP TABLE, а компрометація застосунку не дає контролю над структурою БД.
set -e

APP_USER="${POSTGRES_APP_USER:-fleet_app}"
APP_PASSWORD="${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD не задано}"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v app_user="$APP_USER" \
     -v app_password="$APP_PASSWORD" <<-'EOSQL'
	-- Роль без права створювати БД, ролі чи логінитись як суперкористувач.
	SELECT format(
	    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
	    :'app_user', :'app_password'
	)
	WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')
	\gexec

	-- Підключення до бази — так; створення обʼєктів у ній — ні.
	REVOKE ALL ON DATABASE :"POSTGRES_DB" FROM PUBLIC;
	SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_user')
	\gexec

	-- Схема public: читати можна, створювати в ній — ні.
	REVOKE CREATE ON SCHEMA public FROM PUBLIC;
	SELECT format('REVOKE CREATE ON SCHEMA public FROM %I', :'app_user')
	\gexec
	SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_user')
	\gexec

	-- Дані: повний CRUD, але жодного DDL.
	SELECT format(
	    'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I',
	    :'app_user'
	)
	\gexec
	SELECT format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', :'app_user')
	\gexec

	-- Те саме автоматично для таблиць, які створять майбутні міграції.
	SELECT format(
	    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public
	         GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
	    current_user, :'app_user'
	)
	\gexec
	SELECT format(
	    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public
	         GRANT USAGE, SELECT ON SEQUENCES TO %I',
	    current_user, :'app_user'
	)
	\gexec
EOSQL

echo "[init] роль застосунку '$APP_USER' створено з обмеженими правами"
