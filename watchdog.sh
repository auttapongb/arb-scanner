#!/bin/bash
# Watchdog for arb API server
API_PORT=8080
if ! curl -sf http://localhost:$API_PORT/health > /dev/null 2>&1; then
    cd /root/arb-scanner && nohup python3 api_server.py > /dev/null 2>&1 &
    echo "[Watchdog] Started PID $!"
fi
