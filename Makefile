.PHONY: serve mock dev containers-down api-down test lint spec install

# Start the API server (hot-reload via uvicorn --reload)
serve:
	uv run dfe-api run

# Start Prism mock server (for dfe-ui dev — no running Python needed)
mock:
	docker compose -f docker-compose.dev.yaml up

# Full dev environment: tear down containers and API, start mock and API
dev: containers-down api-down mock serve

# Tear down Docker containers
containers-down:
	docker compose -f docker-compose.dev.yaml down

# Tear down API process
api-down:
	@pkill -f 'dfe-api run' 2>/dev/null || true

# Run test suite
test:
	uv run python -m pytest -q

# Lint + format check
lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

# Regenerate openapi-spec/openapi.json from the live FastAPI app
spec:
	uv run python openapi-spec/generate.py

# Install dev dependencies
install:
	uv sync
