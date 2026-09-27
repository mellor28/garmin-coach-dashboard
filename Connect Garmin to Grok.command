#!/bin/zsh

# Finder opens .command files in Terminal. Authenticate Garmin once for Grok CLI.
PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR" || exit 1

clear
echo "Connect Garmin to Grok"
echo "======================"
echo
echo "This is a one-time Garmin login for Grok CLI."
echo "It caches a session next to the dashboard tokens."
echo "Your password stays in this Terminal; it is not stored in Grok config."
echo

UVX="${HOME}/.local/bin/uvx"
if [[ ! -x "$UVX" ]]; then
  UVX="$(command -v uvx || true)"
fi

if [[ -z "$UVX" ]]; then
  echo "uvx was not found. Install uv first: https://docs.astral.sh/uv/"
  echo
  read "?Press Return to close..."
  exit 1
fi

export GARMINTOKENS="$PROJECT_DIR/garmin_tokens"
mkdir -p "$GARMINTOKENS"
chmod 700 "$GARMINTOKENS" 2>/dev/null || true

echo "Token folder: $GARMINTOKENS"
echo

"$UVX" --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth --token-path "$GARMINTOKENS"
auth_status=$?

if (( auth_status != 0 )); then
  echo
  echo "Garmin authentication did not finish."
  read "?Press Return to close..."
  exit "$auth_status"
fi

echo
echo "Checking the saved session..."
"$UVX" --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth --verify --token-path "$GARMINTOKENS"
verify=$?

echo
if (( verify == 0 )); then
  echo "Garmin session is cached. In Grok Bot, add the garmin MCP server and attach it to your Running Coach."
  echo "You do not need to refresh the dashboard for those questions."
else
  echo "The session was saved, but verification failed. Re-run this command."
fi
echo
read "?Press Return to close..."
exit "$verify"
