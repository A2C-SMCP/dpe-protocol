.PHONY: vectors check-vectors test test-python test-rust

vectors:
	python3 scripts/gen_vectors.py

check-vectors:
	python3 scripts/gen_vectors.py --check

test-python:
	python3 sdk/python/tests/test_vectors.py

test-rust:
	cd sdk/rust && cargo test

test: check-vectors test-python test-rust
