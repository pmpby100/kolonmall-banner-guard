@echo off
echo ============================================
echo  KolonMall Banner Integrity Guard - Backend
echo ============================================
cd /d "%~dp0backend"
uvicorn main:app --reload --port 8000
pause
