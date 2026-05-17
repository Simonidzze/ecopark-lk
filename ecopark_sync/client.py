import json
from base64 import b64encode

try:
    import requests
except ModuleNotFoundError:
    requests = None

from .config import env, require_dependency


def basic_auth_header(user, password):
    token = b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def fetch_snapshot():
    require_dependency(requests, "requests")
    response = requests.get(
        env("ONEC_API_URL", required=True),
        headers=basic_auth_header(
            env("ONEC_API_USER", required=True),
            env("ONEC_API_PASSWORD", required=True),
        ),
        timeout=int(env("REQUEST_TIMEOUT_SECONDS", "120")),
    )
    response.raise_for_status()
    return response.json()


def load_snapshot_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
