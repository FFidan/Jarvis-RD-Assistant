#!/usr/bin/env bash
# Double-click launcher for macOS. Terminal.app runs .command files in a new
# window and, on some configurations, closes that window automatically when
# the script exits — which would hide setup.sh's exit code (especially exit
# 3: "Docker was just installed, log out and back in"). This script always
# pauses at the end so the final message stays on screen instead of a silent
# window-close.
set -u

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR" || exit 1

./setup.sh
STATUS=$?

echo
case "$STATUS" in
  0) echo "Setup finished." ;;
  3) echo "Docker was just installed. Log out and back in (or run 'newgrp docker'), then double-click this launcher again." ;;
  *) echo "setup.sh exited with status $STATUS. See the output above for details." ;;
esac

read -n 1 -s -r -p "Press any key to close this window..."
echo
exit "$STATUS"
