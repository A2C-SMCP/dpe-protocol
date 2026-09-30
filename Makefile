.PHONY: vectors check-vectors

vectors:
	python3 scripts/gen_vectors.py

check-vectors:
	python3 scripts/gen_vectors.py --check
