export PATH := $(CURDIR)/.venv/bin:$(PATH)

IMAGE ?= ghcr.io/sessrumnir/lldp-exporter
VERSION := $(shell sed -n 's/.*"\.": "\(.*\)".*/\1/p' .release-please-manifest.json)

.PHONY: deps lint test build

.venv/bin/pytest: requirements.txt
	python3 -m venv .venv
	pip install --require-hashes -r requirements.txt
	touch $@

deps: .venv/bin/pytest
	pre-commit install --hook-type pre-commit --hook-type pre-push

lint: deps
	pre-commit run --all-files

test: deps
	pytest

build:
	podman build --tag $(IMAGE):$(VERSION) .
