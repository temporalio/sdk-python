# LangChain Integration Tests

This directory contains comprehensive tests for the Temporal LangChain integration.

## Test Structure

### Test Categories

- **Unit Tests** (`@pytest.mark.unit`): Fast tests that don't require external dependencies
- **Integration Tests** (`@pytest.mark.integration`): Tests that require Temporal worker setup
- **Smoke Tests** (`@pytest.mark.smoke`): End-to-end tests using real external providers (OpenAI)

### Test Files

- **Configuration**
    - `conftest.py` - Shared fixtures and configuration
- **Unit Tests**
    - `test_mocks.py` - Mock object sanity checks
    - `test_langchain.py` - Basic functionality and import tests
- **Integration Tests**
    - `test_simple_workflows.py` - Activity-as-tool conversion tests
- **Maybe Junk Tests**
    - `test_temporal_behavior.py` - Temporal-specific behavior (timeouts, cancellation, concurrency)
    - `test_error_scenarios.py` - Error handling and edge cases
    - `test_schema_edge_cases.py` - Complex schema generation tests
- **Smoke Tests**    
    - `test_smoke_workflows.py` - Real provider smoke tests using OpenAI

## Running Tests

### Quick Start

```bash
# Run all tests
uv run python -m pytest tests/contrib/langchain/ -v

# Run only unit tests (fast)
uv run python -m pytest tests/contrib/langchain/ -m unit -v

# Run only integration tests
uv run python -m pytest tests/contrib/langchain/ -m integration -v

# Run only smoke tests (requires OPENAI_API_KEY and TEST_LANGCHAIN_INTEGRATION=1)
uv run python -m pytest tests/contrib/langchain/ -m smoke -v

# Run with coverage
uv run python -m pytest tests/contrib/langchain/ --cov=temporalio.contrib.langchain --cov-report=term-missing -v
```

### Using Test Runner

```bash
# Run unit tests only
python tests/contrib/langchain/run_tests.py unit

# Run integration tests only
python tests/contrib/langchain/run_tests.py integration

# Run smoke tests only (requires external services)
python tests/contrib/langchain/run_tests.py smoke

# Run all tests
python tests/contrib/langchain/run_tests.py all

# Run with coverage reporting
python tests/contrib/langchain/run_tests.py coverage
```

## Test Coverage

The test suite covers:

### Core Functionality
- ✅ Wrapper activity registration
- ✅ Activity-as-tool conversion
- ✅ Basic workflow execution
- ✅ Mock object behavior

### Error Scenarios
- ✅ Invalid inputs to `activity_as_tool`
- ✅ Activity exceptions propagation
- ✅ Timeout handling
- ✅ Type validation

### Schema Edge Cases
- ✅ Optional parameters and defaults
- ✅ Pydantic model inputs/outputs
- ✅ Reserved word parameters
- ✅ Keyword-only arguments
- ✅ Dataclass inputs

### Temporal Behavior
- ✅ Workflow cancellation
- ✅ Activity timeouts
- ✅ Concurrent tool execution
- ✅ Worker concurrency limits

### Real Provider Integration (Smoke Tests)
- ✅ End-to-end OpenAI LangChain integration
- ✅ Real model execution through Temporal
- ✅ Multi-tool workflow validation
- ✅ Error handling with actual API failures
- ✅ Concurrent real provider requests

## Requirements

- Python 3.8+
- Temporal Python SDK
- pytest
- pytest-asyncio
- pytest-cov (for coverage)
- LangChain (optional, tests use mocks when not available)
- langchain-openai (dev dependency, only needed for smoke tests)

## CI/CD Integration

The tests are designed for CI/CD pipelines:

- Fast unit tests for PR validation
- Full integration tests for protected branches
- Coverage reporting with 85% minimum threshold
- Clear test markers for selective execution

## Development

### Adding New Tests

1. Use appropriate markers (`@pytest.mark.unit` or `@pytest.mark.integration`)
2. Use shared fixtures from `conftest.py`
3. Follow naming conventions (`test_*`)
4. Add docstrings explaining what the test validates

### Test Guidelines

- Unit tests should complete in < 1 second
- Integration tests should complete in < 10 seconds
- Use descriptive test names and docstrings
- Mock external dependencies in unit tests
- Test both success and failure scenarios

## Troubleshooting

### Common Issues

1. **Import errors**: Ensure LangChain is installed or tests will be skipped
2. **Timeout errors**: Integration tests may need longer timeouts in slow environments
3. **Coverage failures**: Ensure new code is covered by tests

### Environment Variables

- `TEST_LANGCHAIN_INTEGRATION=1` - Enable extended integration tests and smoke tests with real providers
- `OPENAI_API_KEY` - Required for smoke tests that use OpenAI models

### Installing Dependencies for Smoke Tests

```bash
# Install dev dependencies (includes langchain-openai)
uv sync --all-extras --dev

# Or install just langchain-openai for smoke tests
pip install langchain-openai
```

## Contributing

When adding new features to the LangChain integration:

1. Add unit tests for the core functionality
2. Add integration tests for end-to-end workflows  
3. Test error scenarios and edge cases
4. Consider adding smoke tests for real provider validation
5. Ensure coverage remains above 85%
6. Update this README if adding new test categories 