"""Bounded, self-contained candidate transport; business validation stays in service."""
from __future__ import annotations

import base64
import io
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import socket
import stat
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from question_bank.errors import QbError, error

MAX_UPLOAD = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_FILES = 512


def invalid(message, code="UPLOAD_INVALID"):
    return QbError([error(code, message)])


def unpack(raw: bytes, target: Path):
    if len(raw) > MAX_UPLOAD:
        raise invalid("Upload exceeds 64 MiB", "UPLOAD_TOO_LARGE")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES or sum(e.file_size for e in entries) > MAX_EXPANDED:
                raise invalid("Expanded package exceeds limits", "UPLOAD_TOO_LARGE")
            seen = set()
            expanded = 0
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                mode = entry.external_attr >> 16
                if (not name or "\\" in name or ":" in name or path.is_absolute()
                    or any(p in ("..", ".", "") for p in name.rstrip("/").split("/"))
                    or path.as_posix().rstrip("/") in seen or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                    raise invalid("Unsafe or duplicate ZIP member")
                seen.add(path.as_posix().rstrip("/"))
                dest = target.joinpath(*path.parts)
                if entry.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as src, dest.open("xb") as out:
                        while chunk := src.read(1024 * 1024):
                            expanded += len(chunk)
                            if expanded > MAX_EXPANDED:
                                raise invalid("Expanded package exceeds limits", "UPLOAD_TOO_LARGE")
                            out.write(chunk)
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError) as exc:
        raise invalid("Unreadable ZIP package") from exc
    candidate = target / "candidate.json"
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        from question_bank.models import CandidateEnvelope
        from pydantic import ValidationError
        try:
            envelope = CandidateEnvelope.model_validate(payload)
        except ValidationError as exc:
            raise invalid("Invalid candidate schema", "SCHEMA_INVALID") from exc
        refs = [x.asset for x in envelope.images + envelope.evidence]
        refs += [x.source_asset for x in envelope.images if x.source_asset]
        for ref in refs:
            name = ref.ref
            path = PurePosixPath(name)
            if ("\\" in name or ":" in name or path.is_absolute()
                or any(p in ("", ".", "..") for p in name.split("/"))
                or not (target / name).is_file()):
                raise invalid("Every asset must be an uploaded package file", "UPLOAD_ASSET_MISSING")
        return payload
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise invalid("Missing or invalid candidate.json") from exc


def durable_write(path, raw):
    """Acknowledge archive writes only after bytes reach the filesystem."""
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def submit_upload(service, raw: bytes, actor: str):
    # UUID archives retain each attempt, including conflicting and failed ones.
    attempts = service.root / "inputs" / "remote" / uuid.uuid4().hex
    attempts.mkdir(parents=True)
    for directory in (attempts.parent, attempts.parent.parent, service.root):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    durable_write(attempts / "upload.zip", raw)
    package = attempts / "package"
    package.mkdir()
    try:
        unpack(raw, package)
        receipt = service.submit_candidate(package, actor_id=actor)
    except QbError as exc:
        durable_write(attempts / "error.json", json.dumps({"errors": exc.errors}, ensure_ascii=False).encode())
        raise
    durable_write(attempts / "receipt.json", json.dumps(receipt, ensure_ascii=False).encode())
    return receipt


def package_bytes(package):
    buffer = io.BytesIO()
    paths = sorted(Path(package).rglob("*"))
    if len(paths) > MAX_FILES or sum(p.stat().st_size for p in paths if p.is_file()) > MAX_EXPANDED:
        raise invalid("Expanded package exceeds limits", "UPLOAD_TOO_LARGE")
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            if path.is_symlink():
                raise invalid("Package symlinks are forbidden")
            if path.is_file():
                archive.write(path, path.relative_to(package).as_posix())
    raw = buffer.getvalue()
    if len(raw) > MAX_UPLOAD:
        raise invalid("Upload exceeds 64 MiB", "UPLOAD_TOO_LARGE")
    return raw


def request(server, endpoint, *, raw=None, user=None, password=None, timeout=120):
    parsed = urllib.parse.urlsplit(server)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise invalid("Server must be an HTTP(S) base URL without credentials/query")
    req = urllib.request.Request(server.rstrip("/") + endpoint, data=raw)
    if raw is not None:
        req.add_header("Content-Type", "application/zip")
    user = user if user is not None else os.environ.get("QB_SERVER_USER", "")
    password = password if password is not None else os.environ.get("QB_SERVER_PASSWORD", "")
    if user or password:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
    try:
        # Credentials must never be forwarded to redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
            result = json.load(response)
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise ValueError("Invalid remote response")
            return result
    except urllib.error.HTTPError as exc:
        try:
            result = json.load(exc)
            if not isinstance(result, dict):
                raise ValueError("Invalid error response")
        except (ValueError, UnicodeError, http.client.HTTPException):
            result = {"errors": [error("HTTP_ERROR", f"HTTP {exc.code}", retryable=exc.code >= 500)]}
        result["ok"] = False
        result["http_status"] = exc.code
        if raw is not None and exc.code >= 500:
            result["outcome_unknown"] = True
        return result
    except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.HTTPException, ValueError, UnicodeError) as exc:
        return {"ok": False, "outcome_unknown": raw is not None,
                "errors": [error("REMOTE_UNAVAILABLE", "Request failed; query receipt or retry the exact archived package and submission_id", retryable=True)]}
