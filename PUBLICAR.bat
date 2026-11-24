@echo off
cd /d "%~dp0"
set VER=
for /f "tokens=2 delims==# " %%v in ('findstr /b "VERSAO" legendar.py') do set VER=%%v
set VER=%VER:"=%
if "%VER%"=="" goto semversao
echo Publicando a versao v%VER% ...
echo.
git config user.name >nul 2>&1 || git config --global user.name "iagobelotti"
git config user.email >nul 2>&1 || git config --global user.email "iagobelotti@users.noreply.github.com"
git remote get-url origin >nul 2>&1 && goto atualizar

git init -b main
git add -A
git commit -m "FlowCaptions v%VER%"
gh repo create FlowCaptions --public --source . --push --description "FlowCaptions: baixa, legenda e transcreve videos"
goto release

:atualizar
git add -A
git commit -m "FlowCaptions v%VER%"
git push

:release
if not "%1"=="zip" goto semzip
if not exist "..\FlowCaptions.zip" goto semarquivo
gh release create v%VER% legendar.py "..\FlowCaptions.zip" --title "FlowCaptions v%VER%" --notes "Versao %VER%"
goto fim

:semzip
gh release create v%VER% legendar.py --title "FlowCaptions v%VER%" --notes "Versao %VER%"
goto fim

:semversao
echo Nao achei a linha VERSAO no legendar.py
goto fim

:semarquivo
echo Falta o arquivo ..\FlowCaptions.zip - rode o CRIAR_PACOTE.bat primeiro.

:fim
echo.
echo Fim. Veja em https://github.com/iagobelotti/FlowCaptions/releases
pause
