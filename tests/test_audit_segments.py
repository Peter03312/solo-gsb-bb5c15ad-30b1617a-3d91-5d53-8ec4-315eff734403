"""审定履历“前后状态变化半开区段”纯函数测试。

运行：PYTHONPATH=. python -m pytest tests/test_audit_segments.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.audit import changed_segments
from app.evaluation import evaluate

WS = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
WE = WS + timedelta(hours=1)


def _ev(readings, max_gap=600.0):
    return evaluate(WS, WE, 18, 22, 300, max_gap, readings)


def _ranges(changes):
    return [(c["start"][11:16], c["end"][11:16],
             c["status_before"][0], c["status_after"][0]) for c in changes]


def test_identical_evaluations_no_changes():
    ev = _ev([(WS, 20.0), (WS + timedelta(minutes=10), 20.0)])
    assert changed_segments(ev, ev) == []


def test_new_reading_turns_unknown_into_known():
    before = _ev([])  # 整窗未知
    # 间隔 600s 合法：末读之前 [00:00,00:10) 变为正常覆盖；[00:10,01:00) 仍未知
    after = _ev([(WS, 20.0), (WS + timedelta(minutes=10), 20.0)])
    changes = changed_segments(before, after)
    assert _ranges(changes) == [("00:00", "00:10", "u", "n")]


def test_adjacent_same_transition_merged():
    # before: [WS,WE) 全未知（无读数）
    before = _ev([])
    # after: 两条正常读数把 [00:00,00:20) 切成两段正常，边界并集下
    # 两段变化前后同为 unknown->normal，应合并为 [00:00,00:20)
    after = _ev([
        (WS, 20.0),
        (WS + timedelta(minutes=10), 20.0),
        (WS + timedelta(minutes=20), 20.0),
    ])
    changes = changed_segments(before, after)
    assert _ranges(changes) == [("00:00", "00:20", "u", "n")]
    assert changes[0]["duration_seconds"] == 1200.0


def test_different_transitions_not_merged():
    before = _ev([])  # 全未知
    # after：[00:00,00:10) 超限、[00:10,00:20) 正常
    after = _ev([
        (WS, 23.0),
        (WS + timedelta(minutes=10), 20.0),
        (WS + timedelta(minutes=20), 20.0),
    ])
    changes = sorted(changed_segments(before, after), key=lambda c: c["start"])
    assert _ranges(changes) == [
        ("00:00", "00:10", "u", "e"),
        ("00:10", "00:20", "u", "n"),
    ]


def test_unaffected_region_omitted():
    # before/after 在 [00:00,00:10) 都正常，只在 00:10 之后有差异
    before = _ev([
        (WS, 20.0),
        (WS + timedelta(minutes=10), 23.0),
        (WS + timedelta(minutes=20), 20.0),
    ])
    after = _ev([
        (WS, 20.0),
        (WS + timedelta(minutes=10), 20.0),
        (WS + timedelta(minutes=20), 20.0),
    ])
    changes = changed_segments(before, after)
    assert _ranges(changes) == [("00:10", "00:20", "e", "n")]


def test_rule_tightening_gap_turns_normal_to_unknown():
    readings = [
        (WS, 20.0),
        (WS + timedelta(minutes=10), 20.0),  # 间隔 600s
    ]
    before = _ev(readings, max_gap=600.0)   # 间隔合法 -> 正常
    after = _ev(readings, max_gap=300.0)    # 间隔超限 -> 未知
    changes = changed_segments(before, after)
    assert _ranges(changes) == [("00:00", "00:10", "n", "u")]
