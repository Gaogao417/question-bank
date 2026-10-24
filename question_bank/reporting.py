"""Pure digest helper shared by repair tooling."""
from __future__ import annotations
import hashlib
import json
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
