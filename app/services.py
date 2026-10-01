"""业务服务层：窗口、读数（含误采作废/恢复）、审定、签发、审定履历。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from fastapi import HTTPException, status

from .audit import (
    OP_BASELINE,
    OP_READING_RESTORE,
    OP_READING_SUBMIT,
    OP_READING_VOID,
    OP_READINGS_BATCH,
    OP_RULE_REVISION,
    OP_TYPES_ZH,
    changed_segments,
)
from .evaluation import evaluate
from .models import READING_VOIDED, VERDICT_LABELS_ZH
from .report import build_revision_report
from .schemas import (
    ReadingBatchSubmit,
    ReadingSubmit,
    ReadingVoidSubmit,
    WindowCreate,
    WindowRuleRevise,
)
from .timeutils import parse_stored, to_iso, to_utc


# ---------------------------------------------------------------- 窗口

def create_window(conn: sqlite3.Connection, payload: WindowCreate) -> dict[str, Any]:
    start = to_utc(payload.start_at)
    end = to_utc(payload.end_at)
    if end <= start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="无效窗口：end_at 必须晚于 start_at",
        )
    if payload.allowed_temp_max < payload.allowed_temp_min:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="无效窗口：allowed_temp_max 不能小于 allowed_temp_min",
        )

    cur = conn.execute(
        """
        INSERT INTO windows (name, start_at, end_at, allowed_temp_min,
                             allowed_temp_max, max_continuous_exceed_seconds,
                             max_sample_interval_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name,
            to_iso(start),
            to_iso(end),
            payload.allowed_temp_min,
            payload.allowed_temp_max,
            payload.max_continuous_exceed_seconds,
            payload.max_sample_interval_seconds,
        ),
    )
    row = conn.execute("SELECT * FROM windows WHERE id = ?", (cur.lastrowid,)).fetchone()
    # 初始规则视为第 1 版，随建窗一并留存
    _ensure_initial_rule_revision(conn, row)
    # 窗口创建即在同一写事务内保存序号 0 的审定基线（建窗时无活动读数）
    insert_audit_baseline(conn, row, source="created")
    return window_to_dict(conn, row)


def get_window_row(conn: sqlite3.Connection, window_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM windows WHERE id = ?", (window_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"窗口 {window_id} 不存在")
    return row


def list_windows(conn: sqlite3.Connection, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM windows ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return [window_to_dict(conn, r) for r in rows]


# ---------------------------------------------------------------- 窗口规则修订

def _rules_dict(tmin: float, tmax: float, max_exceed: float, max_gap: float) -> dict[str, Any]:
    """四项审定规则值（统一转 float，便于快照/比较一致）。"""
    return {
        "allowed_temp_min": float(tmin),
        "allowed_temp_max": float(tmax),
        "max_continuous_exceed_seconds": float(max_exceed),
        "max_sample_interval_seconds": float(max_gap),
    }


def _window_rules(win: sqlite3.Row) -> dict[str, Any]:
    return _rules_dict(
        win["allowed_temp_min"], win["allowed_temp_max"],
        win["max_continuous_exceed_seconds"], win["max_sample_interval_seconds"],
    )


def _ensure_initial_rule_revision(conn: sqlite3.Connection, win: sqlite3.Row) -> None:
    """初始规则视为第 1 版：该窗口缺少 v1 记录时按当前窗口参数补记（幂等）。

    正常路径下建窗与库迁移已各自写入 v1；这里兜底保证修订/查询时记录完整。
    """
    exists = conn.execute(
        "SELECT 1 FROM window_rule_revisions WHERE window_id=? AND revision=1",
        (win["id"],),
    ).fetchone()
    if exists is not None:
        return
    conn.execute(
        """
        INSERT INTO window_rule_revisions (window_id, revision, allowed_temp_min,
                                           allowed_temp_max, max_continuous_exceed_seconds,
                                           max_sample_interval_seconds, reason, created_at)
        VALUES (?, 1, ?, ?, ?, ?, NULL, ?)
        """,
        (
            win["id"], win["allowed_temp_min"], win["allowed_temp_max"],
            win["max_continuous_exceed_seconds"], win["max_sample_interval_seconds"],
            win["created_at"],
        ),
    )


def _rule_revision_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """规则修订行 -> API 输出（含前后规则、原因与时间）。"""
    previous = None
    if row["prev_allowed_temp_min"] is not None:
        previous = _rules_dict(
            row["prev_allowed_temp_min"], row["prev_allowed_temp_max"],
            row["prev_max_continuous_exceed_seconds"],
            row["prev_max_sample_interval_seconds"],
        )
    return {
        "window_id": row["window_id"],
        "revision": row["revision"],
        "rules": _rules_dict(
            row["allowed_temp_min"], row["allowed_temp_max"],
            row["max_continuous_exceed_seconds"], row["max_sample_interval_seconds"],
        ),
        "previous_rules": previous,
        "reason": row["reason"],
        "created_at": row["created_at"],
    }


def revise_window_rules(
    conn: sqlite3.Connection, window_id: int, payload: WindowRuleRevise
) -> tuple[str, dict[str, Any]]:
    """修订窗口审定规则，返回 (effect, rule_revision_dict)。调用方需已持有写事务。

    - 初始规则为第 1 版；target_revision 为当前版本 +1 才生效（revised）；
    - target 等于当前版本且四项规则值与原因均一致 -> 幂等（idempotent，不留新记录）；
    - 同号内容/原因不一致、旧号、跳号 -> 409，均不留新记录；
    - 温度下限大于上限 -> 422；窗口不存在 -> 404；
    - 生效时更新窗口当前规则并留存修订记录（前后规则/原因/时间），
      窗口起止时间与读数不变。
    """
    win = get_window_row(conn, window_id)
    if payload.allowed_temp_max < payload.allowed_temp_min:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="无效规则：allowed_temp_max 不能小于 allowed_temp_min",
        )

    _ensure_initial_rule_revision(conn, win)
    current = int(win["rule_revision"])
    target = payload.target_revision
    new_rules = _rules_dict(
        payload.allowed_temp_min, payload.allowed_temp_max,
        payload.max_continuous_exceed_seconds, payload.max_sample_interval_seconds,
    )

    if target <= current:
        cur_row = conn.execute(
            "SELECT * FROM window_rule_revisions WHERE window_id=? AND revision=?",
            (window_id, current),
        ).fetchone()
        if target == current:
            cur_rules = _rules_dict(
                cur_row["allowed_temp_min"], cur_row["allowed_temp_max"],
                cur_row["max_continuous_exceed_seconds"],
                cur_row["max_sample_interval_seconds"],
            )
            if cur_rules == new_rules and cur_row["reason"] == payload.reason:
                # 相同目标号 + 四项规则值与原因均一致的重放 -> 幂等，不留新记录
                return "idempotent", _rule_revision_to_dict(cur_row)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"窗口 {window_id} 规则修订号 {target} 已生效但内容不一致："
                    "相同目标修订号重放须四项规则值与原因均一致（幂等），否则拒绝。"
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"窗口 {window_id} 当前规则版本为 {current}，拒绝目标修订号 {target}："
                "旧规则修订号不能覆盖更高版本。"
            ),
        )
    if target > current + 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"窗口 {window_id} 当前规则版本为 {current}，拒绝目标修订号 {target}："
                f"目标修订号须为当前版加 1（即 {current + 1}），不允许跳号。"
            ),
        )

    # target == current + 1：生效。留存前后规则、原因与时间，并更新窗口当前规则。
    prev_rules = _window_rules(win)
    conn.execute(
        """
        INSERT INTO window_rule_revisions (
            window_id, revision, allowed_temp_min, allowed_temp_max,
            max_continuous_exceed_seconds, max_sample_interval_seconds, reason,
            prev_allowed_temp_min, prev_allowed_temp_max,
            prev_max_continuous_exceed_seconds, prev_max_sample_interval_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            window_id, target,
            new_rules["allowed_temp_min"], new_rules["allowed_temp_max"],
            new_rules["max_continuous_exceed_seconds"],
            new_rules["max_sample_interval_seconds"],
            payload.reason,
            prev_rules["allowed_temp_min"], prev_rules["allowed_temp_max"],
            prev_rules["max_continuous_exceed_seconds"],
            prev_rules["max_sample_interval_seconds"],
        ),
    )
    conn.execute(
        """
        UPDATE windows SET allowed_temp_min=?, allowed_temp_max=?,
                           max_continuous_exceed_seconds=?, max_sample_interval_seconds=?,
                           rule_revision=?
        WHERE id=?
        """,
        (
            new_rules["allowed_temp_min"], new_rules["allowed_temp_max"],
            new_rules["max_continuous_exceed_seconds"],
            new_rules["max_sample_interval_seconds"],
            target, window_id,
        ),
    )
    row = conn.execute(
        "SELECT * FROM window_rule_revisions WHERE window_id=? AND revision=?",
        (window_id, target),
    ).fetchone()
    return "revised", _rule_revision_to_dict(row)


def list_window_rule_revisions(
    conn: sqlite3.Connection, window_id: int
) -> list[dict[str, Any]]:
    """该窗口的全部规则修订历史（含初始第 1 版），按规则版本升序。"""
    rows = conn.execute(
        "SELECT * FROM window_rule_revisions WHERE window_id=? ORDER BY revision ASC",
        (window_id,),
    ).fetchall()
    return [_rule_revision_to_dict(r) for r in rows]


# ---------------------------------------------------------------- 读数

def _latest_revision_row(
    conn: sqlite3.Connection, window_id: int, reading_id: str
) -> Optional[sqlite3.Row]:
    """该 ID 修订号最大的历史行（不限状态）；无历史返回 None。"""
    return conn.execute(
        "SELECT * FROM reading_revisions WHERE window_id=? AND reading_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (window_id, reading_id),
    ).fetchone()


def submit_reading(
    conn: sqlite3.Connection, window_id: int, payload: ReadingSubmit
) -> tuple[str, dict[str, Any]]:
    """提交/修订读数，返回 (effect, reading_dict)。调用方需已持有写事务。"""
    win = get_window_row(conn, window_id)
    win_start = parse_stored(win["start_at"])
    win_end = parse_stored(win["end_at"])

    measured = to_utc(payload.measured_at)
    # 窗口对读数提交为闭区间 [start, end]：终点时刻读数作为“覆盖终止采样”，
    # 用于结束上一条读数的覆盖（其自身区段时长为 0）。严格早于起点或晚于
    # 终点的读数才拒收。输出的温度区段仍是半开 [start, end)。
    if measured < win_start or measured > win_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"读数时间 {to_iso(measured)} 超出窗口 "
                f"[{to_iso(win_start)}, {to_iso(win_end)}]（两端含）"
            ),
        )
    measured_iso = to_iso(measured)
    temperature = float(payload.temperature)

    # 当前活动行（同一 window_id + reading_id 唯一）
    active = conn.execute(
        "SELECT * FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, payload.reading_id),
    ).fetchone()

    if active is not None:
        if payload.revision == active["revision"]:
            # 相同 ID + 修订号重复提交：内容一致幂等，不一致冲突
            if active["measured_at"] != measured_iso or float(active["temperature"]) != temperature:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {payload.reading_id} 修订号 {payload.revision} 已提交但内容冲突："
                        f"已存 ({active['measured_at']}, {active['temperature']})，"
                        f"本次 ({measured_iso}, {temperature})。相同 ID+修订号重复提交内容必须一致。"
                    ),
                )
            return "idempotent", reading_to_dict(active)

        if payload.revision < active["revision"]:
            # 旧修订号一律拒绝，内容是否相同都不得覆盖更高修订号
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"读数 {payload.reading_id} 当前修订号为 {active['revision']}，"
                    f"拒绝修订号 {payload.revision}：旧修订号不能覆盖更高修订号。"
                ),
            )
        effect = "updated"
    else:
        # 无活动行：全新读数，或该 ID 最新修订为作废（恢复场景）。
        latest = _latest_revision_row(conn, window_id, payload.reading_id)
        if latest is None:
            effect = "created"
        elif latest["status"] == READING_VOIDED:
            # 已作废：只有更高修订号可恢复；旧版提交不得复活该读数
            if payload.revision < latest["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {payload.reading_id} 当前修订号为 {latest['revision']}（已作废），"
                        f"拒绝修订号 {payload.revision}：旧修订号不能覆盖更高修订号，"
                        "也不得复活已作废读数。"
                    ),
                )
            if payload.revision == latest["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {payload.reading_id} 修订号 {payload.revision} 已用于作废记录，"
                        "不能以读数内容重复提交；恢复该读数请使用更高修订号。"
                    ),
                )
            effect = "restored"
        else:
            # 防御路径（正常流程不可达：活动行缺失时最新修订必为作废）。
            # 沿用历史精确匹配判幂等/冲突，极端情况下可据其补回活动行。
            hist = conn.execute(
                "SELECT * FROM reading_revisions WHERE window_id=? AND reading_id=? AND revision=?",
                (window_id, payload.reading_id, payload.revision),
            ).fetchone()
            if hist is not None:
                if hist["measured_at"] != measured_iso or float(hist["temperature"]) != temperature:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"读数 {payload.reading_id} 修订号 {payload.revision} 历史内容冲突："
                            f"({hist['measured_at']}, {hist['temperature']}) vs "
                            f"({measured_iso}, {temperature})"
                        ),
                    )
                active_row = _insert_active(conn, window_id, payload.reading_id,
                                            payload.revision, measured_iso, temperature)
                return "idempotent", reading_to_dict(active_row)
            effect = "created"

    # 同刻不同读数拒收（UNIQUE(window_id, measured_at) 兜底；先给清晰报错）。
    # 更新自身读数且时间戳未变时不算冲突。
    clash = conn.execute(
        "SELECT reading_id FROM readings WHERE window_id=? AND measured_at=?",
        (window_id, measured_iso),
    ).fetchone()
    if clash is not None and clash["reading_id"] != payload.reading_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"时刻 {measured_iso} 已存在读数 {clash['reading_id']}，同一窗口不允许同刻的不同读数",
        )

    # 落库：新历史行（active）+ 活动表 upsert；updated 时旧历史行标记 superseded。
    # restored 时作废旧修订行保留 'voided' 状态（作废是历史事实），仅新增一行 active。
    if effect == "updated":
        conn.execute(
            "UPDATE reading_revisions SET status='superseded' "
            "WHERE window_id=? AND reading_id=? AND revision=?",
            (window_id, payload.reading_id, active["revision"]),
        )
    conn.execute(
        """
        INSERT INTO reading_revisions (window_id, reading_id, revision, measured_at,
                                       temperature, status)
        VALUES (?, ?, ?, ?, ?, 'active')
        """,
        (window_id, payload.reading_id, payload.revision, measured_iso, temperature),
    )
    try:
        conn.execute(
            """
            INSERT INTO readings (window_id, reading_id, revision, measured_at, temperature)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(window_id, reading_id) DO UPDATE SET
                revision=excluded.revision,
                measured_at=excluded.measured_at,
                temperature=excluded.temperature,
                created_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (window_id, payload.reading_id, payload.revision, measured_iso, temperature),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"读数拒收：时间戳与窗口内其它读数同刻（{measured_iso}），{exc}",
        )
    active_row = conn.execute(
        "SELECT * FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, payload.reading_id),
    ).fetchone()
    return effect, reading_to_dict(active_row)


def submit_reading_batch(
    conn: sqlite3.Connection, window_id: int, payload: ReadingBatchSubmit
) -> list[tuple[str, dict[str, Any]]]:
    """批量补传读数：整批校验、整批落库，返回与请求同序的 [(effect, reading_dict)]。

    调用方需已持有写事务。任一单项冲突或无效即抛 HTTPException，由事务
    上下文整体回滚（活动读数与修订历史均不变）。每项沿用 submit_reading
    的窗口边界、时区、修订与冲突语义，差异在于：

    - 同批 reading_id 不得重复（422）；
    - 同刻唯一按“批次全部应用后的最终状态”校验，因此允许两条读数都以更高
      修订号互换采样时刻，不按写入顺序误拒；
    - 对已作废的 reading_id，以更高修订号提交即恢复（effect=restored），
      恢复同样遵守窗口边界与最终状态同刻唯一约束；
    - 校验全部通过后一次提交全部变化。
    """
    win = get_window_row(conn, window_id)
    win_start = parse_stored(win["start_at"])
    win_end = parse_stored(win["end_at"])

    # ---- 1) 批内重复 reading_id（422） ----
    seen_ids: set[str] = set()
    for p in payload.readings:
        if p.reading_id in seen_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"批次拒收：reading_id {p.reading_id} 在同批中重复，同批不得重复 reading_id",
            )
        seen_ids.add(p.reading_id)

    # 现有活动读数与修订历史（批前快照，整个事务内没有其它写入方）
    active_rows = conn.execute(
        "SELECT * FROM readings WHERE window_id=?", (window_id,)
    ).fetchall()
    active: dict[str, sqlite3.Row] = {r["reading_id"]: r for r in active_rows}
    hist_rows = conn.execute(
        "SELECT * FROM reading_revisions WHERE window_id=?", (window_id,)
    ).fetchall()
    history: dict[tuple[str, int], sqlite3.Row] = {
        (r["reading_id"], r["revision"]): r for r in hist_rows
    }
    # 每个 reading_id 的最新修订行：识别“已作废、待恢复”的 ID
    latest_by_id: dict[str, sqlite3.Row] = {}
    for r in hist_rows:
        cur_latest = latest_by_id.get(r["reading_id"])
        if cur_latest is None or r["revision"] > cur_latest["revision"]:
            latest_by_id[r["reading_id"]] = r

    # ---- 2) 逐项校验：窗口边界、修订号、内容冲突；得到最终状态 ----
    # final[reading_id] = (revision, measured_iso, temperature, effect)
    final: dict[str, tuple[int, str, float, str]] = {}
    # 活动行缺失但历史行存在的幂等项：需把活动行补回（与单条提交同语义）
    recreate_active: set[str] = set()
    for p in payload.readings:
        measured = to_utc(p.measured_at)
        # 窗口对读数提交为闭区间 [start, end]（与单条提交一致）。
        if measured < win_start or measured > win_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"批次拒收：读数 {p.reading_id} 时间 {to_iso(measured)} 超出窗口 "
                    f"[{to_iso(win_start)}, {to_iso(win_end)}]（两端含）"
                ),
            )
        measured_iso = to_iso(measured)
        temperature = float(p.temperature)

        cur = active.get(p.reading_id)
        if cur is not None:
            if p.revision == cur["revision"]:
                if cur["measured_at"] != measured_iso or float(cur["temperature"]) != temperature:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"读数 {p.reading_id} 修订号 {p.revision} 已提交但内容冲突："
                            f"已存 ({cur['measured_at']}, {cur['temperature']})，"
                            f"本次 ({measured_iso}, {temperature})。相同 ID+修订号重复提交内容必须一致。"
                        ),
                    )
                effect = "idempotent"
            elif p.revision < cur["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {p.reading_id} 当前修订号为 {cur['revision']}，"
                        f"拒绝修订号 {p.revision}：旧修订号不能覆盖更高修订号。"
                    ),
                )
            else:
                effect = "updated"
            final[p.reading_id] = (p.revision, measured_iso, temperature, effect)
            continue

        # 无活动行：全新读数，或该 ID 最新修订为作废（批内恢复）。
        latest = latest_by_id.get(p.reading_id)
        if latest is None:
            effect = "created"
        elif latest["status"] == READING_VOIDED:
            # 已作废：只有更高修订号可恢复；旧版提交不得复活该读数
            if p.revision < latest["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"批次拒收：读数 {p.reading_id} 当前修订号为 {latest['revision']}（已作废），"
                        f"拒绝修订号 {p.revision}：旧修订号不能覆盖更高修订号，"
                        "也不得复活已作废读数。"
                    ),
                )
            if p.revision == latest["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"批次拒收：读数 {p.reading_id} 修订号 {p.revision} 已用于作废记录，"
                        "不能以读数内容重复提交；恢复该读数请使用更高修订号。"
                    ),
                )
            effect = "restored"
        else:
            # 防御路径（正常流程不可达）：活动行缺失但最新修订非作废，
            # 沿用历史精确匹配判幂等/冲突。
            hist = history.get((p.reading_id, p.revision))
            if hist is not None:
                if hist["measured_at"] != measured_iso or float(hist["temperature"]) != temperature:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"读数 {p.reading_id} 修订号 {p.revision} 历史内容冲突："
                            f"({hist['measured_at']}, {hist['temperature']}) vs "
                            f"({measured_iso}, {temperature})"
                        ),
                    )
                effect = "idempotent"
                recreate_active.add(p.reading_id)
            else:
                effect = "created"
        final[p.reading_id] = (p.revision, measured_iso, temperature, effect)
    # ---- 3) 同刻唯一：按批次最终状态校验 ----
    # 未在批内出现的既有读数保留其当前时刻；互换时刻（两条读数同时更新到
    # 对方原时刻）在最终状态下不冲突，不按写入顺序误拒。
    final_time: dict[str, str] = {rid: v[1] for rid, v in final.items()}
    for rid, row in active.items():
        final_time.setdefault(rid, row["measured_at"])
    by_time: dict[str, list[str]] = {}
    for rid, ts in final_time.items():
        by_time.setdefault(ts, []).append(rid)
    for ts, ids in by_time.items():
        if len(ids) > 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"批次拒收：时刻 {ts} 在最终状态下存在多条读数 "
                    f"{sorted(ids)}，同一窗口不允许同刻的不同读数"
                ),
            )

    # ---- 4) 一次提交全部变化 ----
    updated_ids = [rid for rid, v in final.items() if v[3] == "updated"]
    if updated_ids:
        conn.executemany(
            "UPDATE reading_revisions SET status='superseded' "
            "WHERE window_id=? AND reading_id=? AND revision=?",
            [
                (window_id, rid, active[rid]["revision"])
                for rid in updated_ids
            ],
        )
    conn.executemany(
        """
        INSERT INTO reading_revisions (window_id, reading_id, revision, measured_at,
                                       temperature, status)
        VALUES (?, ?, ?, ?, ?, 'active')
        """,
        [
            (window_id, rid, rev, ts, temp)
            for rid, (rev, ts, temp, effect) in final.items()
            if effect in ("created", "updated", "restored")
        ],
    )
    # 先删除被替换的活动行、再统一插入：避免“互换时刻”时 readings 的
    # UNIQUE(window_id, measured_at) 在中间状态误触；同批整事务提交。
    if updated_ids:
        conn.executemany(
            "DELETE FROM readings WHERE window_id=? AND reading_id=?",
            [(window_id, rid) for rid in updated_ids],
        )
    insert_effects = ("created", "updated", "restored")
    try:
        conn.executemany(
            """
            INSERT INTO readings (window_id, reading_id, revision, measured_at, temperature)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (window_id, rid, rev, ts, temp)
                for rid, (rev, ts, temp, effect) in final.items()
                if effect in insert_effects or rid in recreate_active
            ],
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"批次拒收：时间戳与窗口内其它读数同刻，{exc}",
        )

    rows = conn.execute("SELECT * FROM readings WHERE window_id=?", (window_id,)).fetchall()
    rows_by_id = {r["reading_id"]: r for r in rows}
    return [
        (final[p.reading_id][3], reading_to_dict(rows_by_id[p.reading_id]))
        for p in payload.readings
    ]


def _insert_active(
    conn: sqlite3.Connection, window_id: int, reading_id: str,
    revision: int, measured_iso: str, temperature: float,
) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO readings (window_id, reading_id, revision, measured_at, temperature)
        VALUES (?, ?, ?, ?, ?)
        """,
        (window_id, reading_id, revision, measured_iso, temperature),
    )
    return conn.execute(
        "SELECT * FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, reading_id),
    ).fetchone()


def list_active_readings(conn: sqlite3.Connection, window_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM readings WHERE window_id=? ORDER BY measured_at ASC, reading_id ASC",
        (window_id,),
    ).fetchall()


def reading_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "reading_id": row["reading_id"],
        "revision": row["revision"],
        "measured_at": row["measured_at"],
        "temperature": row["temperature"],
        "status": "active",
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------- 误采作废

def void_reading(
    conn: sqlite3.Connection, window_id: int, reading_id: str, payload: ReadingVoidSubmit
) -> tuple[str, dict[str, Any]]:
    """作废当前读数（误采），返回 (effect, void_record_dict)。调用方需已持有写事务。

    作废作为该 ID 的一条修订（status='voided'）留存：原因、操作时间及前一版本
    （被作废读数的修订号/时刻/温度随作废行保存）；活动读数随即剔除，由调用方
    在同一事务内重算审定。

    - 相同作废修订号 + 相同原因重放 -> ("idempotent", 原作废记录)，不产生新行；
    - 相同修订号原因不同、修订号不高于当前 -> 409；
    - 读数从未存在 -> 404；已作废的读数不能再次以新修订号作废 -> 409。
    """
    active = conn.execute(
        "SELECT * FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, reading_id),
    ).fetchone()

    if active is None:
        latest = _latest_revision_row(conn, window_id, reading_id)
        if latest is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"读数 {reading_id} 在窗口 {window_id} 中从未存在，无法作废",
            )
        # 不变式：活动行缺失时该 ID 最新修订必为作废（作废即剔除活动行，
        # 恢复必写更高修订号的活动行）。
        if latest["status"] == READING_VOIDED:
            if payload.revision == latest["revision"]:
                if payload.reason != latest["void_reason"]:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"读数 {reading_id} 作废修订号 {payload.revision} 已存在但原因冲突："
                            f"已存「{latest['void_reason']}」，本次「{payload.reason}」。"
                            "相同作废修订号重放必须原因一致。"
                        ),
                    )
                return "idempotent", void_record_to_dict(latest)
            if payload.revision < latest["revision"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"读数 {reading_id} 当前作废修订号为 {latest['revision']}，"
                        f"拒绝作废修订号 {payload.revision}：旧修订号不能覆盖更高修订号。"
                    ),
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"读数 {reading_id} 已于修订号 {latest['revision']} 作废，"
                    f"不能再次以新修订号 {payload.revision} 作废；"
                    "如需恢复请提交更高修订号的读数。"
                ),
            )
        # 防御：正常流程不可达的异常状态
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"读数 {reading_id} 状态异常：无活动读数且最新修订非作废，拒绝作废。",
        )

    cur_rev = active["revision"]
    if payload.revision <= cur_rev:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"读数 {reading_id} 当前修订号为 {cur_rev}，"
                f"拒绝作废修订号 {payload.revision}：作废修订号必须高于当前修订号。"
            ),
        )

    # 落库：作废旧活动修订（superseded）+ 新增作废修订行（留存前一版本内容）
    # + 剔除活动读数；审定重算由调用方在同一事务内完成。
    conn.execute(
        "UPDATE reading_revisions SET status='superseded' "
        "WHERE window_id=? AND reading_id=? AND revision=?",
        (window_id, reading_id, cur_rev),
    )
    conn.execute(
        """
        INSERT INTO reading_revisions (window_id, reading_id, revision, measured_at,
                                       temperature, status, void_reason, voided_at,
                                       prev_revision)
        VALUES (?, ?, ?, ?, ?, 'voided', ?,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)
        """,
        (window_id, reading_id, payload.revision, active["measured_at"],
         float(active["temperature"]), payload.reason, cur_rev),
    )
    conn.execute(
        "DELETE FROM readings WHERE window_id=? AND reading_id=?",
        (window_id, reading_id),
    )
    row = conn.execute(
        "SELECT * FROM reading_revisions WHERE window_id=? AND reading_id=? AND revision=?",
        (window_id, reading_id, payload.revision),
    ).fetchone()
    return "voided", void_record_to_dict(row)


def void_record_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """作废修订行 -> API 输出（含前一版本内容）。"""
    return {
        "reading_id": row["reading_id"],
        "revision": row["revision"],
        "status": READING_VOIDED,
        "reason": row["void_reason"],
        "voided_at": row["voided_at"],
        "previous": {
            "revision": row["prev_revision"],
            "measured_at": row["measured_at"],
            "temperature": row["temperature"],
        },
    }


def list_reading_revisions(
    conn: sqlite3.Connection, window_id: int, reading_id: str
) -> list[dict[str, Any]]:
    """该 ID 的全部修订历史（含作废记录），按修订号升序。"""
    rows = conn.execute(
        "SELECT * FROM reading_revisions WHERE window_id=? AND reading_id=? "
        "ORDER BY revision ASC",
        (window_id, reading_id),
    ).fetchall()
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"读数 {reading_id} 在窗口 {window_id} 中从未存在",
        )
    return [revision_row_to_dict(r) for r in rows]


def revision_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "revision": row["revision"],
        "status": row["status"],
        "measured_at": row["measured_at"],
        "temperature": row["temperature"],
        "created_at": row["created_at"],
        "void_reason": row["void_reason"],
        "voided_at": row["voided_at"],
        "prev_revision": row["prev_revision"],
    }


# ---------------------------------------------------------------- 审定计算

def compute_evaluation(conn: sqlite3.Connection, win: sqlite3.Row) -> dict[str, Any]:
    """基于当前活动读数计算审定结果（不签发）。"""
    reading_rows = list_active_readings(conn, win["id"])
    readings = [
        (parse_stored(r["measured_at"]), float(r["temperature"])) for r in reading_rows
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


def window_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    latest = conn.execute(
        "SELECT version, verdict FROM certificates WHERE window_id=? ORDER BY version DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "name": row["name"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "allowed_temp_min": row["allowed_temp_min"],
        "allowed_temp_max": row["allowed_temp_max"],
        "max_continuous_exceed_seconds": row["max_continuous_exceed_seconds"],
        "max_sample_interval_seconds": row["max_sample_interval_seconds"],
        "rule_revision": row["rule_revision"],
        "created_at": row["created_at"],
        "latest_certificate_version": latest["version"] if latest else None,
        "latest_verdict": latest["verdict"] if latest else None,
    }


def window_detail(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    data = window_to_dict(conn, row)
    data["active_readings"] = [reading_to_dict(r) for r in list_active_readings(conn, row["id"])]
    data["current_evaluation"] = compute_evaluation(conn, row)
    return data


# ---------------------------------------------------------------- 签发

def issue_certificate(
    conn: sqlite3.Connection, window_id: int, note: Optional[str]
) -> dict[str, Any]:
    """签发证书：在当前写事务内读取同一已提交状态并固化快照。

    每次签发 version 自增；修订后再次签发形成新版本，旧版本原样保留。
    """
    win = get_window_row(conn, window_id)
    evaluation = compute_evaluation(conn, win)
    reading_rows = list_active_readings(conn, window_id)

    last = conn.execute(
        "SELECT MAX(version) AS v FROM certificates WHERE window_id=?", (window_id,)
    ).fetchone()
    version = (last["v"] or 0) + 1

    snapshot = {
        "window": {
            "id": win["id"],
            "name": win["name"],
            "start_at": win["start_at"],
            "end_at": win["end_at"],
            "allowed_temp_min": win["allowed_temp_min"],
            "allowed_temp_max": win["allowed_temp_max"],
            "max_continuous_exceed_seconds": win["max_continuous_exceed_seconds"],
            "max_sample_interval_seconds": win["max_sample_interval_seconds"],
            "rule_revision": win["rule_revision"],
        },
        "evaluation": evaluation,
        "readings": [reading_to_dict(r) for r in reading_rows],
    }

    cur = conn.execute(
        """
        INSERT INTO certificates (window_id, version, verdict, snapshot_json, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (window_id, version, evaluation["verdict"], json.dumps(snapshot, ensure_ascii=False), note),
    )
    return get_certificate(conn, int(cur.lastrowid))


def get_certificate_row(conn: sqlite3.Connection, certificate_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM certificates WHERE id=?", (certificate_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"证书 {certificate_id} 不存在")
    return row


def get_certificate(conn: sqlite3.Connection, certificate_id: int) -> dict[str, Any]:
    return certificate_to_dict(get_certificate_row(conn, certificate_id))


def list_certificates(conn: sqlite3.Connection, window_id: int) -> list[dict[str, Any]]:
    get_window_row(conn, window_id)
    rows = conn.execute(
        "SELECT * FROM certificates WHERE window_id=? ORDER BY version ASC", (window_id,)
    ).fetchall()
    return [certificate_to_dict(r) for r in rows]


def certificate_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    verdict = row["verdict"]
    return {
        "id": row["id"],
        "window_id": row["window_id"],
        "version": row["version"],
        "verdict": verdict,
        "verdict_label_zh": VERDICT_LABELS_ZH.get(verdict, verdict),
        "issued_at": row["issued_at"],
        "note": row["note"],
        "snapshot": json.loads(row["snapshot_json"]),
    }


# ---------------------------------------------------------------- 修订影响报告

def revision_report(conn: sqlite3.Connection,
                    old_certificate_id: int,
                    new_certificate_id: int) -> dict[str, Any]:
    """对同一审定窗口的两张已签发证书生成修订影响报告。

    只读取证书行的不可变快照，不触碰当前活动读数；窗口不一致或版本未按
    先后排列时以 422 明确拒绝，证书不存在由取行函数返回 404。
    """
    if old_certificate_id == new_certificate_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"old_certificate_id 与 new_certificate_id 相同（均为 {old_certificate_id}）："
                "修订影响报告必须输入新旧两个不同版本的证书。"
            ),
        )

    old_row = get_certificate_row(conn, old_certificate_id)
    new_row = get_certificate_row(conn, new_certificate_id)

    if old_row["window_id"] != new_row["window_id"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "两张证书不属于同一审定窗口，拒绝生成报告："
                f"旧证书 {old_row['id']} 属于窗口 {old_row['window_id']}，"
                f"新证书 {new_row['id']} 属于窗口 {new_row['window_id']}。"
            ),
        )

    # 同一窗口主键相同即视为同一窗口；快照内窗口时间为防御性二次校验。
    old_snap, new_snap = json.loads(old_row["snapshot_json"]), json.loads(new_row["snapshot_json"])
    ow, nw = old_snap["window"], new_snap["window"]
    if ow["start_at"] != nw["start_at"] or ow["end_at"] != nw["end_at"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"证书 {old_row['id']} 与 {new_row['id']} 的审定窗口边界快照不一致，"
                f"拒绝生成报告：[{ow['start_at']}, {ow['end_at']}) vs "
                f"[{nw['start_at']}, {nw['end_at']})。"
            ),
        )

    if old_row["version"] >= new_row["version"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "证书版本未按先后排列，拒绝生成报告："
                f"old_certificate_id={old_row['id']}（窗口 {old_row['window_id']} "
                f"版本 {old_row['version']}），new_certificate_id={new_row['id']}"
                f"（版本 {new_row['version']}）；要求旧版版本号严格小于新版。"
            ),
        )

    old_cert = certificate_to_dict(old_row)
    new_cert = certificate_to_dict(new_row)
    return build_revision_report(old_cert, new_cert)


# ---------------------------------------------------------------- 审定履历（审计日志）

def _next_audit_seq(conn: sqlite3.Connection, window_id: int) -> int:
    """该窗口下一条履历的连续序号（最大 seq +1；无行时为 0）。

    所有写事务经 BEGIN IMMEDIATE 串行化，序号按实际提交顺序连续分配，
    不会因回滚（失败请求）而产生空洞。
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) AS s FROM window_audit_entries WHERE window_id=?",
        (window_id,),
    ).fetchone()
    return int(row["s"]) + 1


def _max_continuous_of(evaluation: dict[str, Any]) -> float:
    return float(evaluation["max_continuous_exceeded_seconds"])


def insert_audit_baseline(
    conn: sqlite3.Connection,
    win: sqlite3.Row,
    *,
    source: str = "created",
    evaluation: Optional[dict[str, Any]] = None,
    created_at: Optional[str] = None,
) -> sqlite3.Row:
    """保存序号 0 的审定基线，返回插入行。调用方需已持有写事务。

    - 建窗路径（source='created'）：以建窗时的规则、空活动读数计算，
      记录时间沿用窗口创建时间；
    - 既有窗口补建（source='backfilled'）：以当前规则和当前活动读数
      （当时的实时状态）补建，不伪造更早的操作时间。
    """
    if evaluation is None:
        evaluation = compute_evaluation(conn, win)
    if created_at is None:
        # 建窗基线沿用窗口创建时间；补建基线取当前时间（不伪造更早操作）
        if source == "created":
            created_at = win["created_at"]
        else:
            created_at = conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS t"
            ).fetchone()["t"]
    conn.execute(
        """
        INSERT INTO window_audit_entries (
            window_id, seq, operation_type, reading_id, reading_revision,
            rule_revision, rule_revision_ref, verdict_before, verdict_after,
            max_exceeded_before, max_exceeded_after, changed_segments_json,
            operation_json, source, created_at)
        VALUES (?, 0, 'baseline', NULL, NULL, ?, NULL, NULL, ?, NULL, ?, '[]',
                ?, ?, ?)
        """,
        (
            win["id"], int(win["rule_revision"]), evaluation["verdict"],
            _max_continuous_of(evaluation),
            json.dumps(
                {"source": source,
                 "rule_revision": int(win["rule_revision"]),
                 "note": (
                     "窗口创建时保存的审定基线"
                     if source == "created"
                     else "既有窗口升级时以当时规则与活动读数补建的审定基线")},
                ensure_ascii=False),
            source, created_at,
        ),
    )
    return conn.execute(
        "SELECT * FROM window_audit_entries WHERE window_id=? AND seq=0",
        (win["id"],),
    ).fetchone()


def ensure_audit_baseline(conn: sqlite3.Connection, win: sqlite3.Row) -> None:
    """确保窗口有序号 0 的基线（防御性兜底）。

    正常路径下建窗已写基线；仅当历史库窗口因异常缺失基线时，按“既有窗口
    补建”语义以当前规则和活动读数补建（不伪造更早操作）。基线已存在则不动。
    """
    exists = conn.execute(
        "SELECT 1 FROM window_audit_entries WHERE window_id=? AND seq=0",
        (win["id"],),
    ).fetchone()
    if exists is None:
        insert_audit_baseline(conn, win, source="backfilled")


def backfill_audit_baselines(conn: sqlite3.Connection) -> int:
    """为全部缺少序号 0 基线的既有窗口补建基线（启动迁移调用，幂等）。

    以补建时刻的当前规则与当前活动读数计算，不伪造更早操作；记录时间取
    当前时间。返回补建窗口数。
    """
    window_ids = [
        int(r["id"])
        for r in conn.execute("SELECT id FROM windows ORDER BY id ASC").fetchall()
    ]
    count = 0
    for window_id in window_ids:
        exists = conn.execute(
            "SELECT 1 FROM window_audit_entries WHERE window_id=? AND seq=0",
            (window_id,),
        ).fetchone()
        if exists is not None:
            continue
        win = get_window_row(conn, window_id)
        insert_audit_baseline(conn, win, source="backfilled")
        count += 1
    return count


def append_audit_entry(
    conn: sqlite3.Connection,
    *,
    window_id: int,
    operation_type: str,
    before_evaluation: dict[str, Any],
    after_evaluation: dict[str, Any],
    rule_revision: int,
    operation: dict[str, Any],
    reading_id: Optional[str] = None,
    reading_revision: Optional[int] = None,
    rule_revision_ref: Optional[int] = None,
) -> sqlite3.Row:
    """在当前写事务内追加一条连续编号履历，返回插入行。

    仅由“实际生效”的操作调用：记录操作类型、涉及读数 ID/规则版本、操作前后
    结论与最长连续超限时长，以及前后状态不同的半开区段。即使结论不变，
    只要操作生效就记录；幂等重放、签发证书与失败请求不得调用本函数。
    """
    seq = _next_audit_seq(conn, window_id)
    segments = changed_segments(before_evaluation, after_evaluation)
    conn.execute(
        """
        INSERT INTO window_audit_entries (
            window_id, seq, operation_type, reading_id, reading_revision,
            rule_revision, rule_revision_ref, verdict_before, verdict_after,
            max_exceeded_before, max_exceeded_after, changed_segments_json,
            operation_json, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'created')
        """,
        (
            window_id, seq, operation_type, reading_id, reading_revision,
            int(rule_revision), rule_revision_ref,
            before_evaluation["verdict"], after_evaluation["verdict"],
            _max_continuous_of(before_evaluation),
            _max_continuous_of(after_evaluation),
            json.dumps(segments, ensure_ascii=False),
            json.dumps(operation, ensure_ascii=False),
        ),
    )
    return conn.execute(
        "SELECT * FROM window_audit_entries WHERE window_id=? AND seq=?",
        (window_id, seq),
    ).fetchone()


def list_audit_entries(
    conn: sqlite3.Connection, window_id: int, from_seq: int = 0, limit: int = 100
) -> list[dict[str, Any]]:
    """按窗口和起始序号顺序读取履历（seq 升序）。窗口不存在由调用方先判 404。"""
    rows = conn.execute(
        "SELECT * FROM window_audit_entries WHERE window_id=? AND seq>=? "
        "ORDER BY seq ASC LIMIT ?",
        (window_id, from_seq, limit),
    ).fetchall()
    return [audit_entry_to_dict(r) for r in rows]


def audit_entry_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """履历行 -> API 输出（含中文操作类型/结论标签与解析后的区段、详情）。"""
    after_verdict = row["verdict_after"]
    before_verdict = row["verdict_before"]
    return {
        "seq": int(row["seq"]),
        "operation_type": row["operation_type"],
        "operation_type_zh": OP_TYPES_ZH.get(row["operation_type"], row["operation_type"]),
        "reading_id": row["reading_id"],
        "reading_revision": row["reading_revision"],
        "rule_revision": int(row["rule_revision"]),
        "rule_revision_ref": row["rule_revision_ref"],
        "verdict_before": before_verdict,
        "verdict_after": after_verdict,
        "verdict_before_label_zh": (
            VERDICT_LABELS_ZH.get(before_verdict, before_verdict)
            if before_verdict is not None else None),
        "verdict_after_label_zh": VERDICT_LABELS_ZH.get(after_verdict, after_verdict),
        "max_continuous_exceeded_seconds_before": row["max_exceeded_before"],
        "max_continuous_exceeded_seconds_after": row["max_exceeded_after"],
        "changed_segments": json.loads(row["changed_segments_json"]),
        "operation": json.loads(row["operation_json"]),
        "source": row["source"],
        "created_at": row["created_at"],
    }
