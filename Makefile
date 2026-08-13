.DEFAULT_GOAL := help
COMPOSE := docker compose
# Файл розробки підключається лише явно — див. коментар у docker-compose.dev.yml.
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml

.PHONY: help
help: ## Показати цю довідку
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: env
env: ## Створити .env з прикладу (не перезаписує наявний)
	@test -f .env || cp .env.example .env
	@echo "Готово. Замініть секрети у .env!"

.PHONY: up
up: ## Підняти production-конфігурацію (nginx + TLS, без hot-reload)
	$(COMPOSE) up -d --build

.PHONY: up-dev
up-dev: ## Підняти оточення розробки (hot-reload, порти на 127.0.0.1)
	$(COMPOSE_DEV) up -d --build

.PHONY: secrets
secrets: ## Згенерувати нові значення секретів для .env
	@python -c "import secrets;print('JWT_SECRET='+secrets.token_urlsafe(48))"
	@python -c "import secrets;print('TOKEN_HASH_SECRET='+secrets.token_urlsafe(48))"
	@python -c "import secrets;print('POSTGRES_PASSWORD='+secrets.token_urlsafe(32))"
	@python -c "import secrets;print('POSTGRES_APP_PASSWORD='+secrets.token_urlsafe(32))"
	@python -c "import secrets;print('REDIS_PASSWORD='+secrets.token_urlsafe(32))"
	@python -c "import secrets;print('COMMS_ENCRYPTION_KEY='+secrets.token_urlsafe(48))"

.PHONY: down
down: ## Зупинити оточення
	$(COMPOSE) down

.PHONY: logs
logs: ## Логи API
	$(COMPOSE) logs -f api

.PHONY: migrate
migrate: ## Застосувати міграції
	$(COMPOSE) exec api alembic upgrade head

.PHONY: revision
revision: ## Нова автогенерована ревізія: make revision m="опис"
	$(COMPOSE) exec api alembic revision --autogenerate -m "$(m)"

.PHONY: test
test: ## Тести бекенду в контейнері
	$(COMPOSE_DEV) run --rm api pytest -q

.PHONY: test-local
test-local: ## Тести бекенду локально (потрібен .venv)
	cd backend && python -m pytest -q

.PHONY: lint
lint: ## Перевірка стилю
	cd backend && python -m ruff check .

.PHONY: backup
backup: ## Резервна копія (БД + сховище) у ./backups/<мітка>
	@sh infra/backup/backup.sh

.PHONY: restore
restore: ## Відновлення з копії: make restore s=20260813T120000Z
	@sh infra/backup/restore.sh "$(s)"

.PHONY: backups
backups: ## Перелік наявних копій
	@ls -1 backups 2>/dev/null || echo "копій ще немає"

.PHONY: rotate-check
rotate-check: ## Стан ротації секретів (потрібен токен superadmin у $$TOKEN)
	@curl -fsS -H "Authorization: Bearer $$TOKEN" 		$${BASE_URL:-http://127.0.0.1:8000}/api/v1/maintenance/secrets

.PHONY: shell
shell: ## Оболонка в контейнері API
	$(COMPOSE) exec api bash

.PHONY: psql
psql: ## psql у контейнері БД
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-fleet} -d $${POSTGRES_DB:-fleet}
