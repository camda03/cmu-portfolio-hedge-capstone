"""The research worker’s checkpoint I/O, exchange configuration, and model identities."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re

ROOT = Path(__file__).resolve().parent
SOL_MODEL = "gpt-6.2-sol"
ASTRA_MODEL = "gpt-6.2-astra"
STAGES = ("business", "valuation", "catalysts", "geoeconomics", "draft", "review", "report")
MODELS = {name: ASTRA_MODEL if name in ("review", "report") else SOL_MODEL for name in STAGES}


SECURITIES = json.loads((ROOT / "securities.json").read_text())
SUBJECTS = {symbol: item["subject"] for symbol, item in SECURITIES.items() if item["enabled"]}
CONTRACTS = {symbol: item["contract_id"] for symbol, item in SECURITIES.items() if item["enabled"]}
ENABLED = tuple(SUBJECTS)


def dump(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)


def digest(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def write(path, value):
    """Commit a checkpoint before any following external request."""
    directories = {path.parent}
    ancestor = path.parent
    while not ancestor.exists():
        directories.add(ancestor.parent)
        ancestor = ancestor.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    with pending.open("w") as stream:
        stream.write(dump(value) if not isinstance(value, str) else value)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def read(path):
    return json.loads(path.read_text())


def stamp():
    return datetime.now(timezone.utc).isoformat()


def compact(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True, allow_nan=False)


def instant(value):
    value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("Timestamps must include a timezone.")
    return value.astimezone(timezone.utc)


def matches_model(actual, requested):
    return actual == requested or bool(
        isinstance(actual, str) and re.fullmatch(re.escape(requested) + r"-\d{4}-\d{2}-\d{2}", actual)
    )


def rate_delay(error, number):
    """A retry is safe only after an explicit temporary provider refusal."""
    from openai import RateLimitError

    code = getattr(error, "code", None)
    message = str(error).lower()
    if (
        not isinstance(error, RateLimitError)
        or error.status_code != 429
        or "request too large" in message
        or not (
            code in ("rate_limit_exceeded", "slow_down")
            or (
                code is None
                and "rate limit reached" in message
                and ("tokens per min" in message or "requests per min" in message)
            )
        )
    ):
        return None
    delay = 30 * 2 ** min(2, number)
    hint = error.response.headers.get("retry-after")
    if hint:
        try:
            try:
                server_delay = float(hint)
            except ValueError:
                server_delay = (parsedate_to_datetime(hint) - datetime.now(timezone.utc)).total_seconds()
            if math.isfinite(server_delay):
                delay = max(delay, server_delay)
        except (ValueError, TypeError, OverflowError):
            pass
    return delay + random.uniform(0, 3)


def immutable(path, value):
    if path.exists() and read(path) != value:
        raise ValueError("Existing checkpoint differs: " + path.name)
    if not path.exists():
        write(path, dump(value))
