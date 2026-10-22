"""Validate service configuration without connecting to or modifying storage."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit


def validate(env) -> list[str]:
    missing = []
    if not env.get("QB_UI_PASSWORD", ""):
        missing.append("QB_UI_PASSWORD is required")
    try:
        url = urlsplit(env.get("QB_DATABASE_URL", ""))
        valid_db = url.scheme in {"postgres", "postgresql"} and url.hostname and url.path.strip("/")
    except ValueError:
        valid_db = False
    if not valid_db:
        missing.append("QB_DATABASE_URL must select a PostgreSQL host and database")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", env.get("QB_DATABASE_SCHEMA", "question_bank")):
        missing.append("QB_DATABASE_SCHEMA is invalid")
    if env.get("QB_OSS_BUCKET", "").strip():
        for specific, fallback in (("QB_OSS_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_ID"),
                                   ("QB_OSS_ACCESS_KEY_SECRET", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")):
            if not (env.get(specific, "").strip() or env.get(fallback, "").strip()):
                missing.append(f"{specific} or {fallback} is required when OSS is enabled")
    return missing


def main():
    failures = validate(os.environ)
    if failures:
        raise SystemExit("Question Bank configuration invalid: " + "; ".join(failures))
    print("Question Bank service configuration valid (no storage writes)")


if __name__ == "__main__":
    main()
