@echo off
chcp 65001 >nul
setlocal

set "ROOT=%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] "uv" が見つかりません。https://docs.astral.sh/uv/ からインストールしてください。
    pause
    exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
    echo [ERROR] "npm" が見つかりません。Node.js をインストールしてください。
    pause
    exit /b 1
)

echo [1/3] Python 依存関係を確認しています...
pushd "%ROOT%backend"
call uv sync
if errorlevel 1 (
    echo [ERROR] backend の依存関係インストールに失敗しました。
    popd
    pause
    exit /b 1
)
popd

pushd "%ROOT%frontend"

if not exist node_modules (
    echo [2/3] npm パッケージをインストールしています(初回のみ)...
    call npm install
    if errorlevel 1 (
        echo [ERROR] frontend の依存関係インストールに失敗しました。
        popd
        pause
        exit /b 1
    )
) else (
    echo [2/3] npm パッケージは既にインストール済みです。
)

echo [3/3] アプリを起動します(終了するにはこのウィンドウを閉じるか Ctrl+C)...
call npm run dev

popd
endlocal
