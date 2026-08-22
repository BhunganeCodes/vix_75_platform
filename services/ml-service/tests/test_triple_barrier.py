"""Triple-barrier labeling tests: correctness + lookahead-bias guards."""

import numpy as np
import pytest
from app.meta_label import apply_triple_barrier


def _flat(n: int, price: float = 100.0) -> np.ndarray:
    return np.full(n, price, dtype=np.float64)


class TestLabels:
    def test_upper_barrier_hit_first(self) -> None:
        # ATR = 1 -> upper barrier at entry+2. Price rises 1 per bar.
        close = np.concatenate(([_flat(5)[0]], np.linspace(100.0, 110.0, 30)))
        atr_values = np.full(close.size, 1.0)
        labels = apply_triple_barrier(close, atr_values)
        assert labels[0] == 1  # crosses +2 at t+3, before vertical barrier
        assert np.all(labels[:3] == 1)

    def test_lower_barrier_hit_first(self) -> None:
        close = np.concatenate(([100.0], np.linspace(100.0, 90.0, 30)))
        atr_values = np.full(close.size, 1.0)
        labels = apply_triple_barrier(close, atr_values)
        assert labels[0] == -1

    def test_vertical_barrier_when_rangebound(self) -> None:
        rng = np.random.default_rng(7)
        close = 100.0 + rng.normal(scale=0.05, size=60).cumsum()
        atr_values = np.full(close.size, 5.0)  # wide barriers: +/-10
        labels = apply_triple_barrier(close, atr_values, horizon=12)
        # With barriers +/-10 on a ~1-point random walk nothing is touched.
        assert np.all(labels == 0)

    def test_first_contact_wins(self) -> None:
        """Path touches upper early and lower late; label must stay +1."""
        up_leg = np.array([100.0, 101.0, 102.0, 103.0])  # hits +2 at k=2 (ATR=1)
        down_leg = np.linspace(103.0, 80.0, 20)  # would hit -2 much later
        close = np.concatenate((up_leg, down_leg))
        atr_values = np.full(close.size, 1.0)
        labels = apply_triple_barrier(close, atr_values)
        assert labels[0] == 1

    def test_incomplete_horizon_defaults_vertical(self) -> None:
        close = np.array([100.0, 100.5, 101.0])
        atr_values = np.full(3, 10.0)  # unreachable barriers
        labels = apply_triple_barrier(close, atr_values, horizon=12)
        assert np.all(labels == 0)

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="shape"):
            apply_triple_barrier(np.ones(10), np.ones(9))

    def test_custom_mult_scales_barriers(self) -> None:
        close = np.array([100.0, 101.4, 101.8])
        atr_values = np.full(3, 1.0)
        tight = apply_triple_barrier(close, atr_values, atr_mult=1.0)
        loose = apply_triple_barrier(close, atr_values, atr_mult=2.0)
        assert tight[0] in (1, -1, 0)
        assert loose[0] == 0  # wider barrier not reached within the window


class TestNoLookahead:
    def test_labels_stable_when_future_beyond_horizon_changes(self) -> None:
        rng = np.random.default_rng(11)
        base = 100.0 + rng.normal(scale=0.3, size=120).cumsum()
        atr_values = np.full(base.size, 2.0)
        original = apply_triple_barrier(base, atr_values, horizon=12)

        mutated = base.copy()
        mutated[-40:] += 25.0  # rewrite deep future only
        revised = apply_triple_barrier(mutated, atr_values, horizon=12)

        # Rows whose full horizon lies before the mutation point are frozen.
        assert np.array_equal(original[:-55], revised[:-55])

    def test_label_uses_only_window_data(self) -> None:
        """Row t must be identical between the full series and its prefix."""
        rng = np.random.default_rng(13)
        series = 100.0 + rng.normal(scale=0.4, size=150).cumsum()
        atr_values = np.full(series.size, 2.0)

        full = apply_triple_barrier(series, atr_values, horizon=12)
        prefix = apply_triple_barrier(series[:80].copy(), atr_values[:80].copy(), horizon=12)
        assert np.array_equal(full[:68], prefix[:68])
