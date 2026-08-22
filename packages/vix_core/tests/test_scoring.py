"""Confluence scorer gating tests (strategy rules as regression tests)."""

from vix_core.schemas import ConfluenceComponents, Direction, RegimeState, ZoneKind
from vix_core.scoring import ConfluenceInputs, ConfluenceScorer


def make_inputs(**overrides: object) -> ConfluenceInputs:
    base: dict[str, object] = {
        "direction": Direction.BUY,
        "htf_trend": "up",
        "regime": RegimeState.S1_TREND_UP,
        "zone_kind_touched": ZoneKind.DEMAND,
        "bbma_confirms": True,
        "rsi_divergence": True,
        "p_win": 0.70,
    }
    base.update(overrides)
    return ConfluenceInputs(**base)  # type: ignore[arg-type]


class TestGates:
    def test_full_confluence_passes(self) -> None:
        result = ConfluenceScorer().evaluate(make_inputs())
        assert result.passed
        assert result.score == result.max_score

    def test_meta_label_below_threshold_blocks_hard(self) -> None:
        result = ConfluenceScorer().evaluate(make_inputs(p_win=0.54))
        assert not result.passed
        assert not result.components.meta_label_ok

    def test_missing_meta_label_blocks_even_at_max_score(self) -> None:
        # 6/7 score but P(win) unknown -> hard gate fails.
        inputs = make_inputs()
        inputs_no_pwin = ConfluenceInputs(
            direction=inputs.direction,
            htf_trend=inputs.htf_trend,
            regime=inputs.regime,
            zone_kind_touched=inputs.zone_kind_touched,
            bbma_confirms=inputs.bbma_confirms,
            rsi_divergence=inputs.rsi_divergence,
            p_win=None,
        )
        result = ConfluenceScorer().evaluate(inputs_no_pwin)
        assert result.score == 6
        assert not result.passed

    def test_counter_trend_blocked_without_fade(self) -> None:
        result = ConfluenceScorer().evaluate(
            make_inputs(htf_trend="down", regime=RegimeState.S2_TREND_DOWN)
        )
        assert not result.components.htf_trend_aligned
        assert "direction counter to HTF trend" in "".join(result.rejections)

    def test_s0_range_blocked_unless_fade_enabled(self) -> None:
        blocked = ConfluenceScorer().evaluate(make_inputs(regime=RegimeState.S0_RANGE))
        assert not blocked.passed

        faded = ConfluenceScorer().evaluate(
            make_inputs(
                regime=RegimeState.S0_RANGE, allow_s0_fade=True, zone_kind_touched=ZoneKind.DEMAND
            )
        )
        assert faded.components.regime_ok

    def test_wrong_zone_kind_rejects(self) -> None:
        result = ConfluenceScorer().evaluate(make_inputs(zone_kind_touched=ZoneKind.SUPPLY))
        assert not result.components.zone_touch
        assert not result.passed


def test_components_default_fresh() -> None:
    c = ConfluenceComponents()
    assert not any(c.model_dump().values())
