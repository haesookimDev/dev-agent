.PHONY: dev migrate-api test test-api test-runner test-worker test-gateway test-web test-monitoring lint

PYTHON ?= .venv/bin/python
PROMTOOL ?= promtool

dev:
	docker compose --profile demo up --build

migrate-api:
	$(PYTHON) -m alembic -c apps/api/alembic.ini upgrade head

test: test-api test-runner test-worker test-gateway test-web

test-api:
	$(PYTHON) -m pytest -q apps/api/tests

test-runner:
	$(PYTHON) -m pytest -q apps/runner/tests

test-worker:
	cd apps/worker && go test ./...

test-gateway:
	cd apps/gateway && go test ./...

test-web:
	cd apps/web && npm test

test-monitoring:
	$(PROMTOOL) check config --lint-fatal infra/monitoring/prometheus.example.yml
	$(PROMTOOL) test rules infra/monitoring/alerts.test.yml

lint:
	$(PYTHON) -m ruff check apps/api/app apps/api/tests apps/runner/kelpie_runner apps/runner/tests
	cd apps/worker && go vet ./...
	cd apps/gateway && go vet ./...
	cd apps/web && npm run lint
