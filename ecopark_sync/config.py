import os
from pathlib import Path


def load_dotenv(path):
    path = Path(path)
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def require_dependency(module, package_name):
    if module is None:
        raise RuntimeError(
            f"Python package '{package_name}' is not installed. "
            "Run: python3 -m pip install -r requirements.txt"
        )
