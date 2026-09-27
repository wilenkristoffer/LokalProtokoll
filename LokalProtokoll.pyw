"""Double-click to open LokalProtokoll without a console window.
Runs with the project's virtual environment (.venv) when it exists."""

import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
venv_pythonw = root / ".venv" / "Scripts" / "pythonw.exe"

if venv_pythonw.exists() and Path(sys.executable).resolve() != venv_pythonw.resolve():
    subprocess.Popen([str(venv_pythonw), str(root / "LokalProtokoll.pyw")], cwd=root)
    sys.exit(0)

os.chdir(root)
sys.path.insert(0, str(root))
from lokalprotokoll import app  # noqa: E402
from lokalprotokoll.config import load_config  # noqa: E402

app.run(load_config())
