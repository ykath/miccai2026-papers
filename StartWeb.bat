@echo off
cd /d "%~dp0"
python scripts/web_server.py --db data/miccai2026.sqlite --host 127.0.0.1 --port 8002
pause
