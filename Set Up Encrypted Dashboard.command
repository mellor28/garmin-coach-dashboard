#!/bin/zsh

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR" || exit 1

clear
echo "Run Atlas — Encrypted Website Setup"
echo "===================================="
echo
echo "Choose a strong password you can enter on your iPhone."
echo "It will be stored in macOS Keychain and never uploaded."
echo

if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then
  echo "Python environment not found: $PROJECT_DIR/venv/bin/python"
  read "?Press Return to close..."
  exit 1
fi

"$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/publish_dashboard.py" --setup-key
status=$?

echo
if (( status == 0 )); then
  echo "Encryption setup complete."
else
  echo "Encryption setup did not complete."
fi
read "?Press Return to close..."
exit "$status"
