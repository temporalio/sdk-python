test:
	cd nexus-sdk-python && uv run pytest --log-level warning --log-cli-level warning -s 'tests'
	uv run pytest --log-level warning --log-cli-level warning -s 'tests/nexus'
	
.PHONY: test