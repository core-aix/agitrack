@echo off
setlocal enableextensions
rem ===================================================================
rem  aGiTrack MSI self-update bootstrapper (runs ELEVATED via UAC).
rem
rem  aGiTrack hands the install off to this script because the MSI replaces
rem  the very agitrack.exe that is running -- so the install has to happen
rem  after that process exits. This script:
rem    1. waits for the running aGiTrack (its PID) to exit,
rem    2. installs the downloaded MSI with msiexec,
rem    3. writes msiexec's exit code to a marker file that aGiTrack's
rem       (non-elevated) relauncher polls, so the updated build comes back
rem       at the user's normal integrity level rather than as admin.
rem
rem  Arguments:
rem    %1  full path to the downloaded .msi
rem    %2  PID of the aGiTrack process to wait for
rem    %3  full path to the result marker file to write
rem ===================================================================
set "MSI=%~1"
set "WAITPID=%~2"
set "MARKER=%~3"

echo Updating aGiTrack...
echo.
echo If Windows SmartScreen or a security prompt warned about this installer,
echo that is expected: the aGiTrack MSI is not code-signed yet. It is safe to
echo continue (choose "More info" then "Run anyway" if you saw SmartScreen).
echo.

rem --- 1. Wait for the running aGiTrack to release its files ---------
if not "%WAITPID%"=="" (
  echo Waiting for aGiTrack to close...
  :waitloop
  tasklist /fi "PID eq %WAITPID%" 2>nul | find "%WAITPID%" >nul
  if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto waitloop
  )
)

rem --- 2. Install the new MSI ---------------------------------------
echo Installing the update...
msiexec /i "%MSI%" /passive /norestart REINSTALL=ALL REINSTALLMODE=vomus
set "RC=%errorlevel%"

rem --- 3. Publish the result for the relauncher ---------------------
if not "%MARKER%"=="" (
  >"%MARKER%" echo %RC%
)

if not "%RC%"=="0" (
  echo.
  echo aGiTrack update failed ^(msiexec exit code %RC%^).
  echo You can run the downloaded installer manually: %MSI%
  echo.
  pause
)

endlocal
exit /b %RC%
