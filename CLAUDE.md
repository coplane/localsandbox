## Project Overview

BashFS is a Python SDK that wraps [just-bash](https://github.com/vercel-labs/just-bash) and [AgentFS](https://github.com/tursodatabase/agentfs) to provide sandboxed filesystem operations for AI agents. Each bash operation spawns a Node subprocess that hydrates filesystem state from SQLite, executes the command, and persists changes back.

## Commands

- Install dependencies: `uv sync`
- Run all tests: `uv run pytest`
- Run all tests outputting stdout: `uv run pytest -sv`
- Type checking: `uv run pyright`
- Lint/fix code: `uv run ruff check --fix`
- Format code: `uv run ruff format`
- Lint/fix and format code: `uv run ruff check --fix && uv run ruff format`

## Tooling

- Always use `uv` to run commands rather than `python` or `pip`

## Code Style

- Imports: stdlib first, third-party, then local modules (alphabetical within groups)
- Avoid importing in the middle of a file, import at the top of the file unless there's a good reason, example:
    - Large optional dependencies (ie. AI providers)
- Use type hints for all function parameters and return values
- Optional and Union type hints should use `T | None` style
- Classes use PascalCase, functions/variables use snake_case
- Error handling: catch specific exceptions, use assert for invariants
- Return types use string literals for self-references (e.g., `def method(self) -> "Class":`)
- Use f-strings for string formatting
- Write docstrings for public methods (parameters, return values, raised exceptions)
- Single responsibility classes with clear public/private method separation
- NEVER use `from __future__ import annotations` as python 3.12+ doesn't require it and can cause issues with type introspection
- Keep lines under 100 characters (E501 is ignored in ruff.toml)
- Don't overly comment every line of body of code, only comment on complex logic or edge cases where the reader might not understand the intent. The code should be self-documenting.
- IMPORTANT: Don't sprinkle obvious comments everywhere!

### Internal Naming

- Reserve a single leading underscore for implementation details (module helpers, glue methods, internal modules) that may change without notice. The goal is to signal “not public” while keeping the code debuggable and testable.
- Do **not** introduce new identifiers/functions with double leading underscores, other than in usage of Dunder methods.

## PERSISTENCE

- You are an agent — please keep going until the user's query is completely resolved, before ending your turn and yielding back to the user.
- Only terminate your turn when you are sure that the problem is solved.
- Let the user know if they are asking for something that is not possible.

## Meta

- When updating the design for core features, update any docs in the docs folder or README
- For major new features, suggest adding a design doc in the docs/design folder
- As you complete feature implementations always run linting, typechecking and tests and fix issues that arise.
