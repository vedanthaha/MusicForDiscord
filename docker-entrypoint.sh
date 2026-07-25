#!/bin/sh
# docker-entrypoint.sh – Start Lavalink then the Python bot.
# Lavalink needs a few seconds to become ready before the bot connects.

set -e

echo "==> Starting Lavalink..."
cd /lavalink
java -jar Lavalink.jar &
LAVALINK_PID=$!

echo "==> Waiting for Lavalink to become ready (10 s)..."
sleep 10

echo "==> Starting Discord music bot..."
cd /app
exec python bot.py
