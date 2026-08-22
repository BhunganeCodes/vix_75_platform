"""Model artifact persistence with SHA256 integrity verification.

Every ML artifact is stored as ``<name>.joblib`` plus a ``<name>.joblib.sha256``
sidecar. Loading FAILS CLOSED: a missing sidecar or checksum mismatch raises
:class:`ArtifactIntegrityError` (audit finding fix - the legacy codebase
deserialized pickle files with no integrity checks).

``joblib`` is an optional dependency of vix_core (installed transitively via
scikit-learn in ml-service).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path

from vix_core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "ArtifactIntegrityError",
    "artifact_digest",
    "load_artifact",
    "save_artifact",
    "verify_artifact",
]


class ArtifactIntegrityError(RuntimeError):
    """Raised when an artifact is missing its manifest or fails verification."""


def artifact_digest(path: str | Path) -> str:
    """SHA256 hex digest of a file, streamed to bound memory usage."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def save_artifact(obj: object, path: str | Path) -> Path:
    """Serialize ``obj`` with joblib and write the ``.sha256`` sidecar."""
    import joblib

    target = Path(path)
    if target.suffix != ".joblib":
        target = target.with_suffix(".joblib")
    target.parent.mkdir(parents=True, exist_ok=True)

    joblib.dump(obj, target, compress=3)
    digest = artifact_digest(target)
    sidecar = target.with_name(target.name + ".sha256")
    sidecar.write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    logger.info("artifact saved", path=str(target), sha256=digest[:12])
    return target


def _read_expected_digest(sidecar: Path) -> str:
    text = sidecar.read_text(encoding="utf-8").strip()
    match = re.match(r"^([0-9a-fA-F]{64})\b", text)
    if not match:
        raise ArtifactIntegrityError(f"malformed checksum manifest: {sidecar}")
    return match.group(1).lower()


def verify_artifact(path: str | Path) -> None:
    """Verify SHA256 sidecar; raises :class:`ArtifactIntegrityError` on failure."""
    target = Path(path)
    sidecar = target.with_name(target.name + ".sha256")
    if not target.exists():
        raise ArtifactIntegrityError(f"artifact missing: {target}")
    if not sidecar.exists():
        raise ArtifactIntegrityError(f"checksum sidecar missing for {target}")
    expected = _read_expected_digest(sidecar)
    actual = artifact_digest(target)
    if not hmac.compare_digest(expected, actual):
        raise ArtifactIntegrityError(
            f"checksum mismatch for {target}: expected {expected[:12]}, got {actual[:12]}"
        )


def load_artifact(path: str | Path) -> object:
    """Load a joblib artifact after mandatory SHA256 verification."""
    import joblib

    target = Path(path)
    verify_artifact(target)
    obj = joblib.load(target)
    logger.info("artifact loaded", path=str(target))
    return obj
