import json
from base64 import b64encode
from urllib.parse import urlsplit, urlunsplit

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


def receipt_url():
    configured = env("ONEC_RECEIPT_URL", "").strip()
    if configured:
        return configured
    snapshot_url = env("ONEC_API_URL", required=True)
    parts = urlsplit(snapshot_url)
    path = parts.path.rstrip("/")
    if path.endswith("/snapshot"):
        path = f"{path[:-len('/snapshot')]}/receipt"
    else:
        path = f"{path}/receipt"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def fetch_receipt_pdf(owner_plot_id):
    require_dependency(requests, "requests")
    response = requests.get(
        receipt_url(),
        params={"owner_plot_id": str(owner_plot_id)},
        headers=basic_auth_header(
            env("ONEC_API_USER", required=True),
            env("ONEC_API_PASSWORD", required=True),
        ),
        timeout=int(env("REQUEST_TIMEOUT_SECONDS", "120")),
    )
    if not response.ok:
        try:
            payload = response.json()
            message = payload.get("error") or payload.get("code")
        except ValueError:
            message = ""
        raise RuntimeError(
            f"1С не сформировала квитанцию: HTTP {response.status_code}"
            + (f", {message}" if message else "")
        )
    document = response.content
    if not document.startswith(b"%PDF-"):
        raise RuntimeError("1С вернула ответ, который не является PDF")
    return document


def load_snapshot_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
