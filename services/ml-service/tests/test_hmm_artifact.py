"""HMM training + artifact integrity tests (SHA256 fail-closed contract)."""

from pathlib import Path

import numpy as np
import pytest
from app.hmm import build_regime_matrix, predict_regime, train_hmm
from vix_core.artifacts import (
    ArtifactIntegrityError,
    artifact_digest,
    load_artifact,
    save_artifact,
)

REGIME_LABELS = {"S0_range", "S1_trend_up", "S2_trend_down"}


@pytest.fixture(scope="module")
def regime_bundle(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Tiny but valid HMM trained on structured synthetic dummy data.

    Three blocks with distinct drift/vol so the EM fit separates states
    instead of collapsing one component onto a single sample.
    """
    rng = np.random.default_rng(3)
    block = lambda drift, scale: np.column_stack(  # noqa: E731
        (
            rng.normal(loc=drift, scale=scale, size=150),
            rng.normal(loc=0.004, scale=0.001, size=150),
            rng.normal(loc=0.001, scale=0.0002, size=150),
        )
    )
    matrix = np.vstack(
        (
            block(+0.0015, 0.0008),  # up-trend-ish
            block(-0.0002, 0.0006),  # range-ish
            block(-0.0018, 0.0011),  # down-trend-ish
        )
    )
    path = str(tmp_path_factory.mktemp("models") / "regime_hmm.joblib")
    return train_hmm(matrix, artifact_path=path)


class TestArtifactIntegrity:
    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        payload = {"hello": [1, 2, 3]}
        target = tmp_path / "bundle.joblib"
        save_artifact(payload, target)
        assert load_artifact(target) == payload
        assert (tmp_path / "bundle.joblib.sha256").exists()

    def test_sidecar_suffix_enforced(self, tmp_path: Path) -> None:
        target = save_artifact({"x": 1}, tmp_path / "not_joblib_ext.bin")
        assert target.name == "not_joblib_ext.joblib"

    def test_load_fails_without_sidecar(self, tmp_path: Path) -> None:
        import joblib

        target = tmp_path / "orphan.joblib"
        joblib.dump({"a": 1}, target)
        with pytest.raises(ArtifactIntegrityError, match="sidecar missing"):
            load_artifact(target)

    def test_load_fails_on_tampered_manifest(self, tmp_path: Path) -> None:
        target = save_artifact({"a": 1}, tmp_path / "model.joblib")
        sidecar = target.with_name(target.name + ".sha256")
        sidecar.write_text(("0" * 64) + "  model.joblib\n", encoding="utf-8")
        with pytest.raises(ArtifactIntegrityError, match="mismatch"):
            load_artifact(target)

    def test_load_fails_on_tampered_bytes(self, tmp_path: Path) -> None:
        target = save_artifact({"secret": "original"}, tmp_path / "model.joblib")
        raw = bytearray(target.read_bytes())
        raw[-5:] = b"\x00\x00\x00\x00\x00"
        target.write_bytes(bytes(raw))
        with pytest.raises(ArtifactIntegrityError):
            load_artifact(target)

    def test_digest_matches_reference(self, tmp_path: Path) -> None:
        import hashlib

        target = tmp_path / "f.bin"
        target.write_bytes(b"vix75" * 1000)
        assert artifact_digest(target) == hashlib.sha256(b"vix75" * 1000).hexdigest()


class TestRegimeModel:
    def test_trained_artifact_is_verifiable(self, regime_bundle: dict) -> None:
        from vix_core.artifacts import verify_artifact

        verify_artifact(regime_bundle["path"])  # must not raise

    def test_predict_returns_valid_distribution(self, regime_bundle: dict) -> None:
        row = np.array([[0.0005, 0.004, 0.001]])
        label, state_id, probs = predict_regime(regime_bundle, row)
        assert label in REGIME_LABELS
        assert isinstance(state_id, int)
        total = sum(probs)
        assert total == pytest.approx(1.0, abs=1e-6)
        assert all(p >= 0.0 for p in probs)

    def test_state_ordering_by_drift(self, regime_bundle: dict) -> None:
        """S1 must map to the highest-drift state, S2 the lowest."""
        order = regime_bundle["order"]
        means = regime_bundle["model"].means_
        drifts = {label: float(means[state][0]) for label, state in order.items()}
        assert drifts["S1"] >= drifts["S0"] >= drifts["S2"]

    def test_build_matrix_drops_warmup_rows(self) -> None:
        close = np.linspace(100.0, 110.0, 50)
        atr_values = np.full(50, 1.0)
        matrix = build_regime_matrix(close, atr_values)
        assert matrix.shape[1] == 3
        assert np.all(np.isfinite(matrix))

    def test_train_requires_minimum_history(self, tmp_path: Path) -> None:
        tiny = np.random.default_rng(1).normal(size=(20, 3))
        with pytest.raises(ValueError, match=">= 100"):
            train_hmm(tiny, artifact_path=str(tmp_path / "tiny.joblib"))
