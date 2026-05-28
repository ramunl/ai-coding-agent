#!/bin/bash
set -e

cd /opt/ai_agent

/usr/bin/git fetch origin
/usr/bin/git reset --hard origin/main

source /opt/ai_agent_venv/bin/activate
pip install -r requirements.txt

sudo systemctl restart ai-agent
