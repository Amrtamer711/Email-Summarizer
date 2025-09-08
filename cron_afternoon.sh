#!/bin/bash
# Afternoon Email Digest - Runs at 2pm
# Covers emails from 9am to 2pm today

cd "$(dirname "$0")"
source venv/bin/activate
USER_PROFILE=amr python main.py afternoon >> cron_afternoon.log 2>&1