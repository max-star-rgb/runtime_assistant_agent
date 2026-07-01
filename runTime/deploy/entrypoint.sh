#!/bin/bash
set -e

# Ensure log directory exists
mkdir -p /app/logs

# Start supervisord (manages both gateway and runtime with auto-restart)
exec supervisord -c /app/deploy/supervisord.conf
