"""审定履历（按窗口、按时间追溯）。

每个审定窗口维护一条**窗口内连续编号**的履历：

- 序号 ``0`` 是窗口创建时保存的**审定基线**（当时的规则与活动读数重算出的
  审定状态）；既有窗口在服务升级（建表迁移）时以当时的规则与活动读数补建
  基线，``occurred_at`` 取补建时刻、``detail.origin=backfilled``，不伪造
  更早的操作时间。
- 此后每次**实际生效**的写操作（单条提交、批量补传、作废、恢复、规则修订）
  在**同一写事务**内追加一条序号自增的履历，记录操作类型、涉及的读数 ID 或
  规则版本、操作前后的审定结论与最长连续超限时长，以及前后状态不同的半开
  区段（复用修订影响报告的时间线对齐逻辑，含变化前后状态与证据）。
- 幂等重放、签发证书、失败（回滚）的请求不产生履历；写事务以
  ``BEGIN IMMEDIATE`` 串行化，序号按事务实际提交顺序连续分配。

履历读取：``GET /api/v1/windows/{id}/audit-history?start_seq=&limit=``，
按窗口与起始序号升序返回；窗口不存在 404，非法序号 422。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .evaluation import evaluate
from .report import align_timelines
from .timeutils import parse_stored

# 操作类型
OP_BASELINE = "baseline"                  # 序号 0：审定基线
OP_READING_SUBMIT = "reading_submit"      # 单条读数提交（created/updated/restored）
OP_READINGS_BATCH = "readings_batch"      # 批量补传（整批只记一条）
OP_READING_VOID = "reading_void"          # 误采作废（恢复走提交入口，effect=restored）
OP_RULES_REVISION = "rules_revision"      # 窗口审定规则修订生效


# ---------------------------------------------------------------- 现状快照

def _window_snapshot(win: sqlite3.Row) -> dict[str, Any]:
    """窗口行 -> 快照窗口参数（形状与证书快照的 window 块一致，供时间线对齐）。"""
    return {
        "id": win["id"],
        "name": win["name"],
        "start_at": win["start_at"],
        "end_at": win["end_at"],
        "allowed_temp_min": float(win["allowed_temp_min"]),
        "allowed_temp_max": float(win["allowed_temp_max"]),
        "max_continuous_exceed_seconds": float(win["max_continuous_exceed_seconds"]),
        "max_sample_interval_seconds": float(win["max_sample_interval_seconds"]),
        "rule_revision": int(win["rule_revision"]),
    }


def _active_readings(conn: sqlite3.Connection, window_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT reading_id, revision, measured_at, temperature FROM readings "
        "WHERE window_id=? ORDER BY measured_at ASC, reading_id ASC",
        (window_id,),
    ).fetchall()
    return [
        {
            "reading_id": r["reading_id"],
            "revision": int(r["revision"]),
            "measured_at": r["measured_at"],
            "temperature": float(r["temperature"]),
        }
        for r in rows
    ]


def live_evaluation(conn: sqlite3.Connection, win: sqlite3.Row) -> dict[str, Any]:
    """以窗口当前规则与活动读数计算审定结果（与 services.compute_evaluation 同源）。"""
    readings = [
        (parse_stored(r["measured_at"]), float(r["temperature"]))
        for r in _active_readings(conn, win["id"])
    ]
    return evaluate(
        window_start=parse_stored(win["start_at"]),
        window_end=parse_stored(win["end_at"]),
        temp_min=float(win["allowed_temp_min"]),
        temp_max=float(win["allowed_temp_max"]),
        max_exceed_seconds=float(win["max_continuous_exceed_seconds"]),
        max_sample_interval=float(win["max_sample_interval_seconds"]),
        readings=readings,
    )


def capture_state(conn: sqlite3.Connection, win: sqlite3.Row) -> dict[str, Any]:
    """捕获窗口在当前事务内的审定状态：完整审定结果 + 可对齐的时间线快照。"""
    return {
        "evaluation": live_evaluation(conn, win),
        "snapshot": {
            "window": _window_snapshot(win),
            "readings": _active_readings(conn, win["id"]),
        },
        "rule_revision": int(win["rule_revision"]),
    }


def _state_brief(state: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """履历落库的前后状态摘要：结论 + 最长连续超限时长（附规则版本与读数条数）。"""
    if state is None:
        return None
    ev = state["evaluation"]
    return {
        "verdict": ev["verdict"],
        "max_continuous_exceeded_seconds": float(ev["max_continuous_exceeded_seconds"]),
        "rule_revision": int(state["rule_revision"]),
        "reading_count": int(ev["reading_count"]),
    }


# ---------------------------------------------------------------- 追加履历

def _changed_segments(before: Optional[dict[str, Any]],
                      after: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """前后时间线中状态发生变化的半开区段（按全部边界对齐、相邻同态合并）。"""
    if before is None or after is None:
        return []
    return align_timelines(before["snapshot"], after["snapshot"])


def _next_seq(conn: sqlite3.Connection, window_id: int) -> int:
    """窗口内下一个履历序号。调用方持有 BEGIN IMMEDIATE 写事务，故并发写入
    按实际拿到写锁（提交）的顺序取得连续序号。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
        "FROM window_audit_history WHERE window_id=?",
        (window_id,),
    ).fetchone()
    return int(row["next_seq"])


def insert_baseline(
    conn: sqlite3.Connection, win: sqlite3.Row, *, backfilled: bool
) -> Optional[int]:
    """写入序号 0 的审定基线；已存在则跳过（幂等），返回基线序号（已存在返回 None）。

    - 建窗路径：``backfilled=False``，基线即建窗时的初始规则与空活动读数；
    - 升级补建：``backfilled=True``，以当时规则与活动读数重算，操作时间取
      当前（补建）时刻，不伪造更早操作。
    """
    exists = conn.execute(
        "SELECT 1 FROM window_audit_history WHERE window_id=? AND seq=0",
        (win["id"],),
    ).fetchone()
    if exists is not None:
        return None
    state = capture_state(conn, win)
    conn.execute(
        """
        INSERT INTO window_audit_history (
            window_id, seq, operation, reading_ids_json, rule_revisions_json,
            detail_json, before_state_json, after_state_json, changed_segments_json)
        VALUES (?, 0, ?, '[]', '[]', ?, NULL, ?, '[]')
        """,
        (
            win["id"], OP_BASELINE,
            json.dumps(
                {"origin": "backfilled" if backfilled else "window_created"},
                ensure_ascii=False,
            ),
            json.dumps(_state_brief(state), ensure_ascii=False),
        ),
    )
    return 0


def backfill_baselines(conn: sqlite3.Connection) -> int:
    """为所有尚无履历的既有窗口补建序号 0 基线（服务升级迁移时调用）。

    以窗口**当时**的规则与活动读数重算，不补造任何更早操作；返回补建条数。
    """
    rows = conn.execute(
        """
        SELECT w.* FROM windows w
        WHERE NOT EXISTS (
            SELECT 1 FROM window_audit_history h WHERE h.window_id = w.id
        )
        ORDER BY w.id ASC
        """
    ).fetchall()
    for win in rows:
        insert_baseline(conn, win, backfilled=True)
    return len(rows)


def append_entry(
    conn: sqlite3.Connection,
    *,
    window_id: int,
    operation: str,
    detail: dict[str, Any],
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
    reading_ids: Optional[list[str]] = None,
    rule_revisions: Optional[list[int]] = None,
) -> int:
    """在调用方的写事务内追加一条生效操作履历，返回分配到的连续序号。

    履历只在操作实际生效后追加；幂等重放、签发与失败请求不应调用本函数。
    """
    seq = _next_seq(conn, window_id)
    conn.execute(
        """
        INSERT INTO window_audit_history (
            window_id, seq, operation, reading_ids_json, rule_revisions_json,
            detail_json, before_state_json, after_state_json, changed_segments_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            window_id,
            seq,
            operation,
            json.dumps(list(reading_ids or []), ensure_ascii=False),
            json.dumps(list(rule_revisions or []), ensure_ascii=False),
            json.dumps(detail or {}, ensure_ascii=False),
            json.dumps(_state_brief(before), ensure_ascii=False),
            json.dumps(_state_brief(after), ensure_ascii=False),
            json.dumps(_changed_segments(before, after), ensure_ascii=False),
        ),
    )
    return seq


# ---------------------------------------------------------------- 读取

def entry_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": int(row["seq"]),
        "window_id": int(row["window_id"]),
        "operation": row["operation"],
        "occurred_at": row["occurred_at"],
        "reading_ids": json.loads(row["reading_ids_json"]),
        "rule_revisions": json.loads(row["rule_revisions_json"]),
        "detail": json.loads(row["detail_json"]),
        "before": json.loads(row["before_state_json"]) if row["before_state_json"] else None,
        "after": json.loads(row["after_state_json"]),
        "changed_segments": json.loads(row["changed_segments_json"]),
    }


def list_entries(
    conn: sqlite3.Connection, window_id: int, start_seq: int, limit: int
) -> tuple[list[dict[str, Any]], Optional[int]]:
    """按窗口与起始序号升序读取履历。

    返回 (entries, next_start_seq)：多取 1 条判断分页，尚有后续时
    ``next_start_seq`` 为下一页起始序号（末条 seq + 1），否则为 None。
    """
    rows = conn.execute(
        """
        SELECT * FROM window_audit_history
        WHERE window_id=? AND seq >= ?
        ORDER BY seq ASC
        LIMIT ?
        """,
        (window_id, start_seq, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]
    next_start_seq = int(page[-1]["seq"]) + 1 if has_more and page else None
    return [entry_to_dict(r) for r in page], next_start_seq
