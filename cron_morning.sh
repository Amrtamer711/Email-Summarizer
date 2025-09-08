#!/bin/bash
# Morning Email Digest - Runs at 9am
# Covers emails from 2pm yesterday to 9am today

cd "$(dirname "$0")"
source venv/bin/activate
USER_PROFILE=amr python main.py morning >> cron_morning.log 2>&1