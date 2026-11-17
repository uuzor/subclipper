.PHONY: install install-ai test lint fmt run worker clean

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"

install-ai:
	.venv/bin/pip install -e ".[dev,ai]"

test:
	.venv/bin/python -m pytest

lint:
	.venv/bin/ruff check src tests

fmt:
	.venv/bin/ruff format src tests

run:
	.venv/bin/uvicorn quickreel.main:app --reload --port 12000

worker:
	.venv/bin/celery -A quickreel.pipeline.celery_app:celery_app worker -l info

clean:
	rm -rf .pytest_cache .ruff_cache tests/.tmp data
