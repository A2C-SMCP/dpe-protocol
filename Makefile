TFROBOT_PYTHON ?= ../../project/TFRobotServer/.venv/bin/python

.PHONY: test lint format vectors test-python test-rust lint-python lint-rust

test: test-python test-rust

lint: lint-python lint-rust

test-python:
	cd python && uv run pytest

test-rust:
	cd rust && cargo test

lint-python:
	cd python && uv run ruff format --check . && uv run ruff check . && uv run mypy src tests examples

lint-rust:
	cd rust && cargo fmt --check && cargo clippy --all-targets --all-features -- -D warnings

format:
	cd python && uv run ruff format . && uv run ruff check --select I --fix .
	cd rust && cargo fmt

vectors:
	$(TFROBOT_PYTHON) scripts/gen_vectors.py
