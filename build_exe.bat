@echo off
title Building OmniStream Studio Lightweight Edition
cd /d "%~dp0"

echo =======================================================
echo     OmniStream Studio - Lightweight Packager
echo =======================================================
echo.

set PYTHON_EXE=C:\Users\angel\AppData\Local\Programs\Python\Python311\python.exe
if not exist "%PYTHON_EXE%" (
    set PYTHON_EXE=python.exe
)

echo [1/3] Checking PyInstaller...
"%PYTHON_EXE%" -m pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo PyInstaller not found. Installing now...
    "%PYTHON_EXE%" -m pip install pyinstaller
)

echo.
echo [2/3] Compiling standalone executable with PyInstaller...
"%PYTHON_EXE%" -m PyInstaller --noconsole --onefile --name "OmniStream_Studio" "Youtube_Downloader.py"
if %errorlevel% neq 0 (
    echo [ERROR] PyInstaller compilation failed.
    pause
    exit /b %errorlevel%
)

echo.
echo [3/3] Assembling lightweight distribution bundle...
set DIST_BUNDLE=dist\OmniStream_Studio
if exist "%DIST_BUNDLE%" (
    rmdir /s /q "%DIST_BUNDLE%"
)
mkdir "%DIST_BUNDLE%"

copy /y "dist\OmniStream_Studio.exe" "%DIST_BUNDLE%\OmniStream_Studio.exe" >nul
if exist "chevron_down.png" copy /y "chevron_down.png" "%DIST_BUNDLE%\" >nul
if exist "chevron_down_hover.png" copy /y "chevron_down_hover.png" "%DIST_BUNDLE%\" >nul

(
echo ===============================================================
echo   OmniStream Studio ^| by Angelo Golloberda
echo   Quick Setup Guide for yt-dlp ^& ffmpeg
echo ===============================================================
echo.
echo OmniStream Studio is a sleek graphical interface designed to download
echo videos, audio, and university Webex recordings using open-source engines.
echo.
echo ---------------------------------------------------------------
echo METHOD 1 (Recommended - Takes 10 Seconds):
echo ---------------------------------------------------------------
echo Windows includes a built-in package manager called "WinGet".
echo.
echo 1. Press Windows Key + R, type:
echo    cmd
echo    and press Enter.
echo.
echo 2. In the terminal window, copy and paste this command:
echo    winget install yt-dlp
echo.
echo 3. Press Enter. Windows will automatically download and install 
echo    yt-dlp and configure your system PATH.
echo.
echo 4. (Optional, for 1080p/4K audio-video merging):
echo    winget install Gyan.FFmpeg
echo.
echo 5. Double-click "OmniStream_Studio.exe" and enjoy!
echo.
echo ---------------------------------------------------------------
echo METHOD 2 (Direct Download from GitHub - No Terminal Needed):
echo ---------------------------------------------------------------
echo If you prefer downloading files directly:
echo.
echo 1. Download "yt-dlp.exe" from the official GitHub releases:
echo    https://github.com/yt-dlp/yt-dlp/releases/latest
echo    (Under "Assets", click on "yt-dlp.exe")
echo.
echo 2. Place "yt-dlp.exe" directly in this folder alongside
echo    "OmniStream_Studio.exe".
echo.
echo 3. (Optional for merging 1080p/4K video and audio):
echo    Download ffmpeg from:
echo    https://github.com/yt-dlp/FFmpeg-Builds/releases
echo    and place "ffmpeg.exe" in this same folder.
echo.
echo ===============================================================
echo Enjoy using OmniStream Studio!
) > "%DIST_BUNDLE%\HOW_TO_INSTALL_ENGINES.txt"

echo.
echo Creating lightweight OmniStream_Studio.zip...
powershell -NoProfile -Command "Compress-Archive -Path '%DIST_BUNDLE%' -DestinationPath 'dist\OmniStream_Studio.zip' -Force"

echo.
echo =======================================================
echo  SUCCESS! Lightweight bundle is ready:
echo  Folder: %~dp0dist\OmniStream_Studio
echo  Zip:    %~dp0dist\OmniStream_Studio.zip
echo =======================================================
echo.
echo Opening output folder...
explorer "%~dp0dist"
pause
