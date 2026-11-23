"""Shared instrument configuration. A running test freezes this file's inputs."""

import json
import re
from pathlib import Path

SECURITIES = json.loads((Path(__file__).parent / "dashboard/securities.json").read_text())
if not SECURITIES or any(
    not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}", s)
    or not isinstance(v["enabled"], bool)
    or v["security_type"] not in ("STK", "IND")
    or v["contract_id"] <= 0
    for s, v in SECURITIES.items()
):
    raise ValueError("Invalid security configuration.")
SUBJECTS = {s: v["subject"] for s, v in SECURITIES.items()}
CONTRACTS = {s: v["contract_id"] for s, v in SECURITIES.items()}
CALENDARS = {s: v["calendar"] for s, v in SECURITIES.items()}
ENABLED = tuple(s for s, v in SECURITIES.items() if v["enabled"])
