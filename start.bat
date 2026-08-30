@echo off
chcp 65001 >nul
setlocal

set "ROOT=%~dp0"

where uv >nul 2>nul
if errorlevel 1 goto :no_uv

where npm >nul 2>nul
if errorlevel 1 goto :no_npm

echo [1/3] Python 依存関係を確認しています...
cd /d "%ROOT%backend"
call uv sync
if errorlevel 1 goto :backend_fail

cd /d "%ROOT%frontend"
if exist node_modules goto :skip_npm_install

echo [2/3] npm パッケージを初回インストールしています...
call npm install
if errorlevel 1 goto :frontend_fail
goto :run_app

:skip_npm_install
echo [2/3] npm パッケージは既にインストール済みです。

:run_app
echo [3/3] アプリを起動します。終了するにはこのウィンドウを閉じるか Ctrl+C を押してください。
call npm run dev
goto :end

:no_uv
echo [ERROR] uv が見つかりません。https://docs.astral.sh/uv/ からインストールしてください。
pause
exit /b 1

:no_npm
echo [ERROR] npm が見つかりません。Node.js をインストールしてください。
pause
exit /b 1

:backend_fail
echo [ERROR] backend の依存関係インストールに失敗しました。
pause
exit /b 1

:frontend_fail
echo [ERROR] frontend の依存関係インストールに失敗しました。
pause
exit /b 1

:end
endlocal
