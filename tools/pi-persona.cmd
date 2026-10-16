@echo off
rem pi-persona.cmd - launcher wrapper for pi-acp (tested with 0.0.34), which drops
rem the session/new _meta.systemPrompt that acp.py sends. driver.py sets
rem TEACH_PERSONA_FILE when TEACH_AGENT_SYSTEM_PROMPT=launcher.
rem
rem Contract: TEACH_PERSONA_FILE = absolute path of the persona file (prompts/<persona>.md).
rem pi --append-system-prompt reads the file when the path exists, and otherwise
rem treats the argument as literal text - hence the two guards below.
rem
rem Keep this file pure ASCII: cmd.exe reads .cmd files in the OEM codepage, so
rem non-ASCII text here would garble. Keep @echo off: pi-acp treats every non-JSON
rem line on pi's stdout as prelude text and forwards it to the ACP client.
if not defined TEACH_PERSONA_FILE goto no_persona
if not exist "%TEACH_PERSONA_FILE%" goto missing_persona
call pi --append-system-prompt "%TEACH_PERSONA_FILE%" %*
exit /b %ERRORLEVEL%

:no_persona
echo pi-persona: TEACH_PERSONA_FILE is not set; refusing to start pi. 1>&2
exit /b 2

:missing_persona
echo pi-persona: persona file not found: %TEACH_PERSONA_FILE% 1>&2
exit /b 3
