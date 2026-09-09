.PHONY: install run test

install:
	python3.12 -m venv .venv
	.venv/bin/pip install -e '.[dev]'

run:
	.venv/bin/swarmboard --reload

test:
	.venv/bin/pytest
	node --test tests/*.cjs
