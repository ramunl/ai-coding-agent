#!/bin/bash
set -e

cd /opt/ai-coding-agent

/usr/bin/git fetch origin
/usr/bin/git reset --hard origin/main
/usr/bin/git submodule sync --recursive
/usr/bin/git submodule update --init --recursive

source /opt/ai_coding_venv/bin/activate
pip install -r requirements.txt

sudo systemctl restart ai-agent
