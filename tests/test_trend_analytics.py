"""五日均线、三日升幅与中和判断的确定性回归测试。"""
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.app.models import FactRecord
from backend.app.services.trend_analytics import (
    analyze_trend,
    close_series,
    moving_average,
    three_day_rise,
)


def series(values, start=date(2026, 6, 1)):
    return [(start + timedelta(days=index), value) for index, value in enumerate(values)]


def test_moving_average_leaves_warmup_empty():
    assert moving_average([1, 2, 3, 4, 5, 6], 5) == [None, None, None, None, 3.0, 4.0]


def test_conflicting_same_day_values_are_dropped_not_overwritten():
    points = series([10, 10, 11, 12, 13]) + [(date(2026, 6, 2), 99)]
    cleaned = close_series(points)
    assert cleaned.conflicts == 1
    assert [value for _, value in cleaned.points] == [10, 11, 12, 13]


def test_three_day_rise_reports_height_percent_and_streak():
    rise = three_day_rise(close_series(series([10, 10.2, 10.5, 10.8])))
    assert rise.available and rise.all_up
    assert rise.change_pct == pytest.approx(8.0, abs=0.01)
    assert rise.height == pytest.approx(0.8, abs=0.001)
    assert rise.consecutive_up_days == 3
    assert rise.daily_changes_pct == pytest.approx([2.0, 2.94, 2.86], abs=0.01)


def test_three_day_rise_unavailable_with_too_few_sessions():
    assert not three_day_rise(close_series(series([10, 10.2, 10.5]))).available


def test_analysis_needs_six_sessions_before_claiming_a_trend():
    result = analyze_trend(series([10, 10.1, 10.2, 10.3, 10.4]))
    assert result.available is False
    assert result.verdict == "数据不足"
    assert "五日均线" in result.notes[0]


def test_rising_series_is_up_and_carries_ma5_and_three_day_fields():
    result = analyze_trend(series([10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9,
                                   11.0, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9]))
    assert result.available and result.verdict in {"上行", "偏强震荡"}
    assert result.ma5 == pytest.approx(11.7, abs=0.01)
    assert result.ma20 == pytest.approx(10.95, abs=0.01)
    assert result.three_day.available and result.three_day.change_pct > 0
    assert len(result.ma5_series) == len(series([1] * 20)) - 4
    assert result.score is not None and result.score > 50
    assert result.confidence > 0


def test_opposing_signals_are_neutralized_instead_of_single_side_story():
    # 长期下行后出现三日急涨：MA5 仍在 MA20 下方（偏弱），短线斜率与三日升幅偏强。
    closes = [20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7.2, 6.8, 7.0, 7.5, 8.0, 8.6, 9.3]
    result = analyze_trend(series(closes))
    assert result.neutralized is True
    assert result.verdict == "震荡"
    assert any("中和" in note for note in result.notes)
    assert {signal.direction for signal in result.signals} >= {"up", "down"}


def test_benchmark_relative_strength_aligns_on_common_dates():
    asset = analyze_trend(series([10, 10.1, 10.2, 10.3, 10.5, 10.8, 11.0]))
    boosted = analyze_trend(
        series([10, 10.1, 10.2, 10.3, 10.5, 10.8, 11.0]),
        benchmark=series([10, 10, 10, 10, 10, 10, 10]), benchmark_name="沪深300")
    assert boosted.relative_strength_pct is not None
    assert boosted.relative_strength_pct > 0
    assert any(signal.key == "relative_strength" for signal in boosted.signals)
    assert asset.relative_strength_pct is None


def test_fact_records_are_accepted_directly():
    facts = [
        FactRecord(fact_id=f"F{index}", entity="测试指数", field="close_price", value=value,
                   period=(date(2026, 6, 1) + timedelta(days=index)).isoformat(),
                   snapshot_time=datetime.now(timezone.utc), source_id="TEST", quality=0.9)
        for index, value in enumerate([10, 10.2, 10.4, 10.6, 10.8, 11.0])
    ]
    result = analyze_trend(facts)
    assert result.available and result.latest_close == 11.0
