@echo off
cd /d %~dp0
python -m pip install -q -r requirements.txt
python -m uvicorn alertwhy.api:app --host 0.0.0.0 --port 8000
