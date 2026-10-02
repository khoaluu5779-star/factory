"""Best-effort secret redaction for anything that gets stored or logged."""

from __future__ import annotations

import os
import re
from typing import Iterable, Mapping

_SENSITIVE_NAME = re.compile(r"(KEY|TOKEN|SECRET|WEBHOOK|PASSWORD|CREDENTIAL)", re.IGNORECASE)
_MIN_LEN = 6
MASK = "[REDACTED]"


class SecretRedactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        # Longest first so a secret containing another secret is masked whole.
        self._secrets = sorted({s for s in secrets if s and len(s) >= _MIN_LEN}, key=len, reverse=True)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SecretRedactor":
        env = os.environ if environ is None else environ
        return cls(v for k, v in env.items() if _SENSITIVE_NAME.search(k))

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        return text
