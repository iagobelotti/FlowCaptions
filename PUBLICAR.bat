@echo off
cd /d "%~dp0"
set VER=
for /f "tokens=2 delims==# " %%v in ('findstr /b "VERSAO" legendar.py') do set VER=%%v
set VER=%VER:"=%
if "%VER%"=="" goto semversao
echo Publicando a versao v%VER% ...
echo.
if exist .git goto atualizar

git init -b main
git add -A
git commit -m "FlowCaption v%VER%"
gh repo create flowcaption --public --source . --push --description "FlowCaption: baixa, legenda e transcreve videos"
goto release

:atualizar
git add -A
git commit -m "FlowCaption v%VER%"
git push

:release
if not "%1"=="zip" goto semzip
if not exist "..\FlowCaption_para_enviar.zip" goto semarquivo
gh release create v%VER% legendar.py "..\FlowCaption_para_enviar.zip" --title "FlowCaption v%VER%" --notes "Versao %VER%"
goto fim

:semzip
gh release create v%VER% legendar.py --title "FlowCaption v%VER%" --notes "Versao %VER%"
goto fim

:semversao
echo Nao achei a linha VERSAO no legendar.py
goto fim

:semarquivo
echo Falta o arquivo ..\FlowCaption_para_enviar.zip - rode o CRIAR_PACOTE.bat primeiro.

:fim
echo.
echo Fim. Veja em https://github.com/iagobelotti/flowcaption/releases
pause
