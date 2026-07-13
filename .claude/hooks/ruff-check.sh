#!/bin/bash
# Auto-run ruff after Claude edits a Python file

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# Only lint Python files
if [[ "$FILE_PATH" != *.py ]]; then
  exit 0
fi

RUFF="${HOME}/.local/bin/ruff"

# Run ruff check (lint) with auto-fix, then format
"$RUFF" check --fix "$FILE_PATH" 2>&1
"$RUFF" format "$FILE_PATH" 2>&1
