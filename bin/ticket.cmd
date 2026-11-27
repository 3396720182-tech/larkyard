@echo off
rem Windows entry for cmd / PowerShell (Git Bash uses bin/ticket). Interpreter: TICKETD_PYTHON, else py -3.
rem Kept ASCII (cmd reads .cmd files in the OEM code page); goto instead of an if-block so a ) in the arguments can't end the block.
setlocal
if not defined TICKETD_PYTHON goto pylauncher
"%TICKETD_PYTHON%" -c "import sys; sys.path.insert(0, sys.argv.pop(1)); from ticketd.cli import main; sys.exit(main())" "%~dp0.." %*
exit /b %ERRORLEVEL%
:pylauncher
py -3 -c "import sys; sys.path.insert(0, sys.argv.pop(1)); from ticketd.cli import main; sys.exit(main())" "%~dp0.." %*
exit /b %ERRORLEVEL%
