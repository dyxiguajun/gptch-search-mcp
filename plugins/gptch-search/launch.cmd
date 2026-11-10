@echo off
if not defined GPTCH_PYTHON exit /b 2
"%GPTCH_PYTHON%" -m personal_search.server
