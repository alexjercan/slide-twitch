.PHONY: help fmt

help:
	@cat Makefile | grep -E "^\w+$:"

lint: # Lint code
	poetry run pylint slide_twitch/ tests/
	poetry run mypy slide_twitch/ tests/ --ignore-missing-imports

fmt: # Format code
	poetry run isort slide_twitch/ tests/
	poetry run black slide_twitch/ tests/
