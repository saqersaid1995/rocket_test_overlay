@echo off
REM Stellar Kinetics - Test Stand Console
cd /d "%~dp0"
if not exist config.yaml copy config.example.yaml config.yaml
python -m teststand.app --config config.yaml
pause
