@echo off
echo ============================================
echo  KolonMall Banner Integrity Guard - Frontend
echo ============================================
echo Opening dashboard in your browser...
start "" "%~dp0frontend\index.html"
echo.
echo Dashboard opened! Make sure the backend is also running.
echo (Run start_backend.bat in another window)
pause
