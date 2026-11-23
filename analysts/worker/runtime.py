"""Read existing secrets only on the VM; never log their contents."""

import base64
import json
import os
from pathlib import Path
import sys
from google.cloud import secretmanager

AUTH_FILES = ("secrets.json", "secret-key", "oauth.json")
OPTIONAL_AUTH_FILES = ("oauth.json.namespaces.json",)


# Encode the broker authentication files for storage as a single bundle.
def auth_bundle(auth):
    files = {n: base64.b64encode((auth / n).read_bytes()).decode() for n in AUTH_FILES}
    files.update(
        {
            n: base64.b64encode((auth / n).read_bytes()).decode()
            for n in OPTIONAL_AUTH_FILES
            if (auth / n).is_file()
        }
    )
    return files


# Validate the bundle's file list and decode its contents.
def unpack_bundle(value):
    if not set(AUTH_FILES) <= set(value) <= set(AUTH_FILES + OPTIONAL_AUTH_FILES):
        raise ValueError("Authentication bundle must include the OAuth index, vault and key.")
    files = {n: base64.b64decode(v, validate=True) for n, v in value.items()}
    state = json.loads(files["oauth.json"])
    if not any(
        u.rstrip("/") == "https://api.ibkr.com/v1/api/mcp-public" for u in state.get("servers", {})
    ):
        raise ValueError("OAuth index does not contain the approved IBKR server.")
    return files


# Create one authentication file exclusively with restricted permissions.
def put_auth(path, value):
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# Read the requested secret version from the configured Google Cloud project.
def secret(name):
    project = os.environ["CAPSTONE_PROJECT"]
    resource = f"projects/{project}/secrets/{name}/versions/latest"
    return (
        secretmanager.SecretManagerServiceClient()
        .access_secret_version(request={"name": resource}, timeout=30)
        .payload.data
    )


# Check the broker files and load the model credential for the worker process.
def credentials():
    auth = Path.home() / ".config/capstone-ibkr-paper"
    if not all((auth / n).is_file() for n in AUTH_FILES):
        raise ValueError("Stored IBKR authentication is absent; prepare it before research.")
    os.environ["OPENAI_API_KEY"] = secret(os.environ["MODEL_SECRET_NAME"]).decode().strip()
    if not os.environ["OPENAI_API_KEY"]:
        raise ValueError("Approved API credential is empty.")


# Install the saved broker bundle into a new worker authentication directory.
def initialize_auth():
    auth = Path.home() / ".config/capstone-ibkr-paper"
    if auth.exists():
        if not all((auth / n).is_file() for n in AUTH_FILES):
            raise ValueError("Partial auth store requires operator recovery, not replacement.")
        return
    files = unpack_bundle(json.loads(secret(os.environ["BROKER_SECRET_NAME"])))
    auth.mkdir(parents=True, mode=0o700)
    for name, value in files.items():
        put_auth(auth / name, value)


# Restore the broker's missing OAuth index after matching the original vault and key.
def restore_missing_index():
    """Repair the v1 omission only when the original vault/key still match exactly."""
    auth = Path.home() / ".config/capstone-ibkr-paper"
    if (auth / "oauth.json").exists():
        return "index_already_present_not_replaced"
    files = unpack_bundle(json.loads(secret(os.environ["BROKER_SECRET_NAME"])))
    if any((auth / n).read_bytes() != files[n] for n in ("secrets.json", "secret-key")):
        raise ValueError(
            "VM credentials differ from the transfer; reconcile without replacing refreshed state."
        )
    for name in OPTIONAL_AUTH_FILES:
        if (auth / name).exists() and (
            name not in files or (auth / name).read_bytes() != files[name]
        ):
            raise ValueError("Existing namespace ledger differs; do not replace it.")
    for name in (*OPTIONAL_AUTH_FILES, "oauth.json"):
        if name in files and not (auth / name).exists():
            put_auth(auth / name, files[name])
    return "missing_index_restored"


if __name__ == "__main__":
    if sys.argv[1:] == ["initialize-auth"]:
        initialize_auth()
    elif len(sys.argv) == 4 and sys.argv[1] == "export-auth":
        bundle = auth_bundle(Path(sys.argv[2]))
        unpack_bundle(bundle)
        put_auth(Path(sys.argv[3]), json.dumps(bundle).encode())
    else:
        raise SystemExit("Use initialize-auth or export-auth AUTH_DIRECTORY OUTPUT_FILE.")
