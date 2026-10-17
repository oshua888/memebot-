#!/bin/bash
# Lance le bot SIMULÉ automatiquement chaque minute (via cron).
# Désinstaller : ./installer_mac_linux.sh --remove
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
LINE="* * * * * cd \"$DIR\" && \"$PY\" memescanner.py bot --cycles 1 >> \"$DIR/bot.log\" 2>&1 # memescanner"

crontab -l 2>/dev/null | grep -v '# memescanner' > /tmp/memescanner_cron
if [ "$1" = "--remove" ]; then
  crontab /tmp/memescanner_cron && echo "Agent désinstallé."
else
  echo "$LINE" >> /tmp/memescanner_cron
  crontab /tmp/memescanner_cron && echo "Agent installé : il tourne chaque minute. Suivi : tail -f \"$DIR/bot.log\""
fi
rm -f /tmp/memescanner_cron
