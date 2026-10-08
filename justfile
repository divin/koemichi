# Default: run everything in order
default: fmt lint isort

# Format code with ruff (replaces black)
fmt:
    uv run ruff format .

# Lint and auto-fix issues
lint:
    uv run ruff check --fix .

# Sort imports (ruff's isort implementation)
isort:
    uv run ruff check --select I --fix .

# Verify everything passes without changing files (for CI)
check:
    uv run ty check .
    uv run ruff format --check .
    uv run ruff check --extend-ignore I .
    uv run ruff check --select I .

# Run tests
test:
    uv run pytest tests -q
