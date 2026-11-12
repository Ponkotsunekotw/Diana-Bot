@echo off
cd /d "%~dp0\.."

set "CSC_PATH=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC_PATH%" set "CSC_PATH=C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"

if not exist "%CSC_PATH%" (
    echo [ERROR] Cannot find built-in .NET Framework csc.exe compiler.
    exit /b 1
)

echo === Compiling zero-dependency native installer: DianaSeedInstaller.exe ===
"%CSC_PATH%" /nologo /codepage:65001 /utf8output /target:winexe /optimize+ /out:"DianaSeedInstaller.exe" /r:System.dll /r:System.Core.dll /r:System.Drawing.dll /r:System.Windows.Forms.dll /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll "installer\DianaSeedInstaller.cs"

if errorlevel 1 (
    echo [FAILED] Compilation failed.
    exit /b 1
)

echo [SUCCESS] Built zero-dependency installer: %CD%\DianaSeedInstaller.exe
exit /b 0
