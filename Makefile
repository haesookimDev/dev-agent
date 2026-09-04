.PHONY: dev test test-api test-runner test-worker test-gateway test-web lint

PYTHON ?= .venv/bin/python

dev:
	docker compose --profile demo up --build

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

lint:
	$(PYTHON) -m ruff check apps/api/app apps/api/tests apps/runner/kelpie_runner apps/runner/tests
	cd apps/worker && go vet ./...
	cd apps/gateway && go vet ./...
	cd apps/web && npm run lint
