# Contributing

Thanks for your interest in Bifrost. Issues and pull requests are welcome.

## Before you open a pull request

1. Open an issue first for anything larger than a bug fix, so we can agree on the approach.
2. Keep the change small and focused. One subject for each pull request.
3. Add or update tests for the behaviour you change.

## Setup

The repository is a [uv](https://docs.astral.sh/uv/) workspace on Python 3.13.

```sh
uv sync
```

## Linting, formatting and tests

CI runs these five commands. Run them before you push:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
helm lint charts/bifrost --set database.dsn=postgresql://u:p@localhost/db
```

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): subject`, for
example `fix(app): reject an empty query`. Keep the subject short and in the imperative.

## Licence

Your contributions are licensed under the [Apache License 2.0](LICENSE).
