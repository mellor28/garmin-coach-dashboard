#!/bin/zsh

# Finder opens .command files in Terminal. Resolve this file's folder so the
# launcher works regardless of the current Terminal directory.
PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR" || exit 1

clear
echo "Garmin Dashboard Refresh"
echo "========================"
echo

if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then
  echo "The Python environment was not found."
  echo "Expected: $PROJECT_DIR/venv/bin/python"
  echo
  read "?Press Return to close..."
  exit 1
fi

"$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/refresh_dashboard.py"
status=$?

if (( status != 0 )); then
  echo
  echo "The refresh did not complete."
  read "?Press Return to close..."
  exit "$status"
fi

echo
echo "Refresh complete. The dashboard has opened in your browser."
sleep 2
