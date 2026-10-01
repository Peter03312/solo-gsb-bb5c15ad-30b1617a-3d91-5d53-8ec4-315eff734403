"""审定履历（审计日志）纯函数：操作类型常量与前后审定区段对比。

履历在每次实际生效的写操作（单条/批量读数提交、作废/恢复、规则修订）后，
按窗口追加一条连续编号记录，其中“前后状态不同的半开区段”由本模块依据操作
前后两次审定结果计算：取两侧全部区段边界的并集切分窗口，只保留状态发生
变化的原子半开区间，相邻且变化前后状态均相同的区间合并。

与 app.report 的时间线对齐区别：履历只关心状态（normal/exceeded/unknown）
本身的变化，不需要读数身份证据。
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
from typing import Any, Sequence

from .timeutils import parse_stored, to_iso

# 操作类型
OP_BASELINE = "baseline"                  # 审定基线（序号 0）
OP_READING_SUBMIT = "reading_submit"      # 单条读数提交/修订（created/updated）
OP_READING_RESTORE = "reading_restore"    # 单条提交恢复已作废读数（restored）
OP_READINGS_BATCH = "readings_batch"      # 批量读数提交（整批只记一条）
OP_READING_VOID = "reading_void"          # 误采作废
OP_RULE_REVISION = "rule_revision"        # 窗口审定规则修订

OP_TYPES_ZH = {
    OP_BASELINE: "审定基线",
    OP_READING_SUBMIT: "读数提交",
    OP_READING_RESTORE: "读数恢复",
    OP_READINGS_BATCH: "批量读数提交",
    OP_READING_VOID: "读数作废",
    OP_RULE_REVISION: "规则修订",
}

_SEGMENT_KEYS = ("normal_segments", "exceeded_segments", "unknown_segments")


def _status_segments(evaluation: dict[str, Any]) -> list[tuple[datetime, datetime, str]]:
    """从审定结果取出全部半开状态区段 (start, end, status)，按起点升序。

    审定输出的非零时长区段首尾相接、恰好铺满半开窗口 [start, end)
    （零时长区段在 evaluate 合并阶段已被丢弃），故可直接按边界并集对齐。
    """
    segs: list[tuple[datetime, datetime, str]] = []
    for key in _SEGMENT_KEYS:
        for s in evaluation.get(key, []):
            segs.append((parse_stored(s["start"]), parse_stored(s["end"]), s["status"]))
    segs.sort(key=lambda x: (x[0], x[1]))
    return segs


def _status_at(segs: Sequence[tuple[datetime, datetime, str]], t: datetime) -> str:
    """t 时刻所在区段的状态（区段半开、铺满窗口，调用方保证 t 被覆盖）。"""
    starts = [s[0] for s in segs]
    i = bisect_right(starts, t) - 1
    start, end, status = segs[i]
    assert start <= t < end, f"时刻 {t} 未被审定区段覆盖"
    return status


def _merge_adjacent_changes(
    changes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并首尾相接且变化前后状态均相同的相邻区间；被无变化区间隔开的不合并。"""
    merged: list[dict[str, Any]] = []
    for ch in changes:
        if merged and merged[-1]["end"] == ch["start"]:
            prev = merged[-1]
            if (prev["status_before"] == ch["status_before"]
                    and prev["status_after"] == ch["status_after"]):
                start_dt = parse_stored(prev["start"])
                end_dt = parse_stored(ch["end"])
                merged[-1] = {
                    "start": prev["start"],
                    "end": ch["end"],
                    "duration_seconds": round((end_dt - start_dt).total_seconds(), 3),
                    "status_before": prev["status_before"],
                    "status_after": prev["status_after"],
                }
                continue
        merged.append(dict(ch))
    return merged


def changed_segments(
    before_evaluation: dict[str, Any],
    after_evaluation: dict[str, Any],
) -> list[dict[str, Any]]:
    """对比操作前后两次审定，返回状态发生变化的半开区段（已合并）。

    输出每个区间：start/end（半开 [start, end)，UTC ISO）、duration_seconds、
    status_before、status_after。结论不变但时间线状态有变化时仍如实列出；
    两次审定完全一致时返回空列表（履历条目本身仍由调用方照常追加）。
    """
    before = _status_segments(before_evaluation)
    after = _status_segments(after_evaluation)

    boundaries = {seg[0] for seg in before} | {seg[1] for seg in before}
    boundaries |= {seg[0] for seg in after} | {seg[1] for seg in after}
    cuts = sorted(boundaries)

    changes: list[dict[str, Any]] = []
    for b0, b1 in zip(cuts, cuts[1:]):
        if b0 == b1:
            continue
        status_before = _status_at(before, b0)
        status_after = _status_at(after, b0)
        if status_before != status_after:
            changes.append({
                "start": to_iso(b0),
                "end": to_iso(b1),
                "duration_seconds": round((b1 - b0).total_seconds(), 3),
                "status_before": status_before,
                "status_after": status_after,
            })
    return _merge_adjacent_changes(changes)
