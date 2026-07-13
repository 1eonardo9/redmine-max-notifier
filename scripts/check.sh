#!/usr/bin/env bash
# Полный прогон всех проверок проекта: pre-commit (ruff/mypy/whitespace) + pytest.
# Запуск руками перед коммитом или в любой момент: bash scripts/check.sh
# На pre-push повторяет то же самое автоматически.

set -euo pipefail

echo "=== pre-commit (ruff, mypy, whitespace) ==="
uv run pre-commit run --all-files

echo
echo "=== pytest ==="
uv run pytest

echo
echo "✅ Все проверки пройдены"
