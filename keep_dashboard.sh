#!/bin/bash
curl -s -o /dev/null -m 3 http://127.0.0.1:8600/ || (cd /home/agentuser && setsid nohup python3 stockdb/web/server.py >> /tmp/dashboard.log 2>&1 &)
