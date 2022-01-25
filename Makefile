.PHONY: install
install:
	pipenv install --dev

.PHONY: lint
lint:
	pipenv run black --check .

.PHONY: black
black:
	pipenv run black .

.PHONY: build
build:
	pipenv run python scripts/gen-protos.py
