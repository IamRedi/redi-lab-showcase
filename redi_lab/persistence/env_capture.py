"""Reproducibility environment capture + pricing evidence helpers.

Explicit blocklist: never capture HOME/USER/USERNAME/KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/APIKEY.
Pricing evidence stores opaque_provider_ref as str|None only (preserves provider independence).
"""

from __future__ import annotations

import os
import platform
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from ..domain import (
    SCHEMA_VERSION,
    EnvironmentInfo,
)

_BLOCKLIST_SUBSTRINGS: tuple[str, ...] = (
    "HOME",
    "USER",
    "USERNAME",
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "APIKEY",
)


def _blocklisted(key: str) -> bool:
    up = key.upper()
    return any(s in up for s in _BLOCKLIST_SUBSTRINGS)


def capture_repro_environment(
    extra: Mapping[str, Any] | None = None,
) -> EnvironmentInfo:
    """Capture minimal reproducibility facts. Returns EnvironmentInfo value object.

    Reads (intentionally limited):
      - platform.python_version()
      - sys.platform + '/' + os.name
      - `git rev-parse HEAD` subprocess (2s timeout; None if fails)
      - SCHEMA_VERSION → metadata['redi_schema_version']

    NEVER reads os.environ['HOME']/USER/USERNAME; never calls getpass or Path.home().
    Raises ValueError if any supplied extra key contains blocklisted substring.
    """
    # --- extra validation FIRST -------------------------------
    if extra:
        for k in extra.keys():
            if _blocklisted(str(k)):
                raise ValueError(
                    f"Environment capture: extra key {k!r} matches blocklist; refuse to record"
                )

    python_version: str = platform.python_version()
    os_description: str = f"{sys.platform}/{os.name}"
    software_revision: str | None = None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if completed.returncode == 0:
            out = completed.stdout.strip()
            if out:
                software_revision = out
    except (OSError, subprocess.SubprocessError):
        software_revision = None

    metadata: dict[str, Any] = {"redi_schema_version": str(SCHEMA_VERSION)}
    if extra:
        metadata.update({k: v for k, v in dict(extra).items()})

    # Defense in depth: make sure nothing in metadata keys contains blocklist
    for k in list(metadata.keys()):
        if _blocklisted(str(k)):  # pragma: no cover - defensive
            del metadata[k]

    return EnvironmentInfo(
        software_revision=software_revision,
        dependency_hash=None,
        python_version=python_version,
        os_description=os_description,
        sandbox_version=None,
        metadata=metadata,
    )


def make_pricing_evidence_row(
    amount: Decimal,
    currency: str = "USD",
    *,
    source_ref: str,
    opaque_provider_ref: str | None = None,
    schedule_version: str | None = None,
    effective_dt: datetime | None = None,
) -> dict[str, Any]:
    """Construct dict row for insert_pricing_evidence. No persistence side effects."""
    if not isinstance(amount, Decimal):
        raise TypeError(f"amount must be Decimal, got {type(amount).__name__}")
    if not isinstance(currency, str) or len(currency) != 3:
        raise ValueError(f"currency must be 3-letter ISO code, got {currency!r}")
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise ValueError("source_ref must be non-empty string")
    if opaque_provider_ref is not None and not isinstance(opaque_provider_ref, str):
        raise TypeError("opaque_provider_ref must be str|None (provider-independence)")
    now = datetime.now(timezone.utc).isoformat()
    return {
        "currency": currency,
        "amount_decimal_str": str(amount),
        "pricing_schedule_version": schedule_version,
        "effective_dt_iso": effective_dt.isoformat() if effective_dt is not None else None,
        "source_ref": source_ref,
        "opaque_provider_ref": opaque_provider_ref,
        "captured_at": now,
    }


def insert_pricing_evidence(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
) -> int:
    """Insert pricing_evidence row (append-only). Returns new row id."""
    cur = conn.execute(
        """
        INSERT INTO pricing_evidence (
            currency, amount_decimal_str, pricing_schedule_version,
            effective_dt_iso, source_ref, opaque_provider_ref, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["currency"],
            row["amount_decimal_str"],
            row.get("pricing_schedule_version"),
            row.get("effective_dt_iso"),
            row["source_ref"],
            row.get("opaque_provider_ref"),
            row["captured_at"],
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


__all__ = [
    "capture_repro_environment",
    "insert_pricing_evidence",
    "make_pricing_evidence_row",
]
