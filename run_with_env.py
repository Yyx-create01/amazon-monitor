import os, sys, runpy

# Load .env before importing main
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
with open(env_path, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()

# Run main.py
main_path = os.path.join(os.path.dirname(__file__), "main.py")
runpy.run_path(main_path, run_name="__main__")
