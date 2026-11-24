@echo off
cd /d "%~dp0"
echo Criando o pacote do FlowCaption para enviar...
echo.
"%~dp0python\python.exe" -I "%~dp0criar_pacote.py"
echo.
pause
