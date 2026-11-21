.PHONY: install install-ai install-download install-all test lint fmt run worker serve deploy build-frontend seed-review clean

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"

install-ai:
	.venv/bin/pip install -e ".[dev,ai]"

install-download:
	.venv/bin/pip install -e ".[dev,download]"

install-all:
	.venv/bin/pip install -e ".[dev,ai,download]"

test:
	.venv/bin/python -m pytest

lint:
	.venv/bin/ruff check src tests

fmt:
	.venv/bin/ruff format src tests

run:
	.venv/bin/uvicorn quickreel.main:app --reload --port 12000

build-frontend:
	cd frontend && npm ci && npm run build

# Serve the API and the built frontend from one process on one origin.
# Binds 0.0.0.0 so a container's port forward (not just localhost) can reach it.
serve: build-frontend
	QUICKREEL_FRONTEND_DIR=$(CURDIR)/frontend/dist \
		.venv/bin/uvicorn quickreel.main:app --host 0.0.0.0 --port 12000

# Same as `serve`, but for a host that forwards a port to a public URL. Pass
# PUBLIC_BASE_URL (and PORT if it is not 12000) so OAuth redirects and the
# WebSub callback address the public origin rather than localhost.
deploy: build-frontend
	QUICKREEL_FRONTEND_DIR=$(CURDIR)/frontend/dist \
	QUICKREEL_PUBLIC_BASE_URL=$(PUBLIC_BASE_URL) \
		.venv/bin/uvicorn quickreel.main:app --host 0.0.0.0 --port $(or $(PORT),12000)

worker:
	.venv/bin/celery -A quickreel.pipeline.celery_app:celery_app worker -l info

# Seed the app-review demo account: a processed project, a rendered 9:16 clip
# and a scheduled post, built over the public API. Idempotent. Pass the review
# password at run time; it is never stored in the repository.
seed-review:
	.venv/bin/python scripts/seed_review_account.py \
		--base-url $(or $(PUBLIC_BASE_URL),http://localhost:12000) \
		--email $(or $(EMAIL),appreview@clipviralmoment.com) \
		--password $(PASSWORD)

clean:
	rm -rf .pytest_cache .ruff_cache tests/.tmp data
