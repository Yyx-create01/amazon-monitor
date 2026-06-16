@echo off
cd /d "C:\Users\Administrator\Desktop\项目文件\自己ASIN监控"
C:\Users\Administrator\Desktop\项目文件\自己ASIN监控\.venv\Scripts\python.exe run_with_env.py %* >> monitor.log 2>&1
