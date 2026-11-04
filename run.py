import os
import subprocess
import sys
from pathlib import Path

from app import app

if __name__ == "__main__":
    if "--skip-tests" not in sys.argv and not os.environ.get("WERKZEUG_RUN_MAIN"):
        tests = subprocess.run([sys.executable, "-m", "tests"], cwd=Path(__file__).resolve().parent)
        if tests.returncode != 0:
            sys.exit("tests failed, server not started (python run.py --skip-tests to start anyway)")
    app.run(debug=True, port=5000, host="0.0.0.0")
