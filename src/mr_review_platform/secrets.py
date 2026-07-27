from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse


class SecretResolutionError(RuntimeError):
    pass


class SecretResolver:
    """Resolve Secret Manager-style references without storing values in business data."""

    def resolve(self, reference: str) -> str:
        parsed = urlparse(reference)
        if parsed.scheme == "env":
            name = (parsed.netloc + parsed.path).lstrip("/")
            value = os.getenv(name)
            if not value:
                raise SecretResolutionError(f"environment secret is unavailable: {name}")
            return value
        if parsed.scheme == "file":
            path = Path(unquote(parsed.path))
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SecretResolutionError("secret file is unavailable") from exc
            if not value:
                raise SecretResolutionError("secret file is empty")
            return value
        raise SecretResolutionError("secret reference must use env:// or file://")
