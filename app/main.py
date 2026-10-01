"""FastAPI 入口：博物馆展柜温度暴露审定服务。"""
from __future__ import annotations

import math
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import services
from .audit import (
    OP_READING_RESTORE,
    OP_READING_SUBMIT,
    OP_READING_VOID,
    OP_READINGS_BATCH,
    OP_RULE_REVISION,
)
from .database import connect, get_conn, init_db, write_transaction
from .models import VERDICT_LABELS_ZH, Verdict
from .schemas import (
    AuditEntryOut,
    CertificateOut,
    EvaluationOut,
    IssueRequest,
    ReadingBatchSubmit,
    ReadingBatchSubmitResponse,
    ReadingOut,
    ReadingRevisionOut,
    ReadingSubmit,
    ReadingSubmitResponse,
    ReadingVoidResponse,
    ReadingVoidSubmit,
    RevisionReportOut,
    WindowCreate,
    WindowOut,
    WindowRuleRevisionOut,
    WindowRuleRevise,
    WindowRuleReviseResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时幂等建表并初始化 WAL
    conn = connect()
    try:
        init_db(conn)
        # 既有窗口补建序号 0 的审定基线（以当时规则与活动读数，不伪造更早操作）
        services.backfill_audit_baselines(conn)
        conn.commit()
    finally:
        conn.close()
    yield


app = FastAPI(
    title="博物馆展柜温度暴露审定服务",
    version="1.5.0",
    description=(
        "对展柜在给定审定窗口内的温度读数进行暴露审定："
        "输出正常/超限/未知区段与最长连续超限时长，"
        "支持读数修订、幂等提交（单条与批量补传）、误采作废与恢复、"
        "窗口审定规则版本化修订（目标规则修订号 + 四项完整新值 + 非空原因）、"
        "证书版本化签发，"
        "并可对同一窗口两张已签发证书的不可变快照生成修订影响报告"
        "（含跨规则版本的规则前后值对照）。"
        "窗口创建即保存序号 0 的审定基线，此后每次实际生效的写操作"
        "（单条/批量读数提交、作废/恢复、规则修订）在同一写事务内追加"
        "连续编号的审定履历（前后结论、最长连续超限时长与状态变化半开区段），"
        "可按窗口与起始序号顺序读取（/audit-entries）。"
    ),
    lifespan=lifespan,
)


def _sanitize_non_finite(obj: Any) -> Any:
    """递归替换非有限浮点（NaN/±Inf）为其字符串形式。

    请求校验失败的错误体会回显原始输入；若输入含 NaN/±Inf，默认序列化
    （allow_nan=False）会抛错导致 500。这里统一替换，保证非法数值得到 422。
    """
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_non_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_non_finite(v) for v in obj]
    return obj


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # 与 FastAPI 默认处理一致（{"detail": [...]}），仅额外清洗非有限浮点
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": _sanitize_non_finite(jsonable_encoder(exc.errors()))},
    )


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "service": "museum-case-temperature-audit",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "verdicts": {v.value: VERDICT_LABELS_ZH[v.value] for v in Verdict},
    }


# ---------------------------------------------------------------- 窗口

@app.post("/api/v1/windows", response_model=WindowOut, status_code=status.HTTP_201_CREATED,
          tags=["windows"])
def create_window(
    payload: WindowCreate, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    with write_transaction(conn):
        return services.create_window(conn, payload)


@app.get("/api/v1/windows", response_model=list[WindowOut], tags=["windows"])
def list_windows(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    return services.list_windows(conn, limit=limit, offset=offset)


@app.get("/api/v1/windows/{window_id}", response_model=WindowOut, tags=["windows"])
def get_window(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    row = services.get_window_row(conn, window_id)
    return services.window_detail(conn, row)


@app.get("/api/v1/windows/{window_id}/evaluation", response_model=EvaluationOut,
         tags=["windows"])
def get_evaluation(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    row = services.get_window_row(conn, window_id)
    return services.compute_evaluation(conn, row)


# ---------------------------------------------------------------- 窗口规则修订

@app.post(
    "/api/v1/windows/{window_id}/rules/revisions",
    response_model=WindowRuleReviseResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["windows"],
)
def revise_window_rules(
    window_id: int,
    payload: WindowRuleRevise,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """馆员修订窗口审定规则：目标规则修订号 + 四项完整新值 + 非空原因。

    - 初始规则视为第 1 版；target_revision 为当前版本 +1 才生效（effect=revised），
      留存前后规则、原因与时间，并按当前活动读数立即重算；
    - target 等于当前版本且四项规则值与原因均一致 -> 幂等（effect=idempotent，
      不留新记录）；同号内容/原因不一致、旧号、跳号 -> 409；
    - 数值须有限、温度下限不大于上限、两个时长为正 -> 否则 422；
      窗口不存在 -> 404；
    - 窗口起止时间与读数保持不变；后续签发采用新规则，既有证书快照不变。
    """
    # 单个写事务：校验 + 规则落库 + 基于同一状态立即重算
    with write_transaction(conn):
        win_before = services.get_window_row(conn, window_id)
        services.ensure_audit_baseline(conn, win_before)
        before_evaluation = services.compute_evaluation(conn, win_before)
        effect, record = services.revise_window_rules(conn, window_id, payload)
        win = services.get_window_row(conn, window_id)  # 重取更新后的当前规则
        evaluation = services.compute_evaluation(conn, win)
        # 修订实际生效才追加履历；同号同内容的幂等重放不追加。
        if effect == "revised":
            services.append_audit_entry(
                conn,
                window_id=window_id,
                operation_type=OP_RULE_REVISION,
                before_evaluation=before_evaluation,
                after_evaluation=evaluation,
                rule_revision=record["revision"],
                rule_revision_ref=record["revision"],
                operation={
                    "effect": "revised",
                    "rule_revision": record["revision"],
                    "previous_rule_revision": record["revision"] - 1,
                    "rules": record["rules"],
                    "previous_rules": record["previous_rules"],
                    "reason": record["reason"],
                },
            )
        return {"effect": effect, "rule_revision": record,
                "current_evaluation": evaluation}


@app.get(
    "/api/v1/windows/{window_id}/rules/revisions",
    response_model=list[WindowRuleRevisionOut],
    tags=["windows"],
)
def list_window_rule_revisions(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    """该窗口的全部规则修订历史（含初始第 1 版），按规则版本升序。"""
    services.get_window_row(conn, window_id)
    return services.list_window_rule_revisions(conn, window_id)


# ---------------------------------------------------------------- 审定履历（审计日志）

@app.get(
    "/api/v1/windows/{window_id}/audit-entries",
    response_model=list[AuditEntryOut],
    tags=["audit"],
)
def list_audit_entries(
    window_id: int,
    from_seq: int = Query(
        0, ge=0,
        description="起始序号（含）；默认 0，即从序号 0 的审定基线开始顺序读取"),
    limit: int = Query(100, ge=1, le=1000, description="本页最多返回条数（1~1000）"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    """按窗口和起始序号顺序（seq 升序）读取审定履历。

    - 序号 0 为窗口创建时（或既有窗口升级时补建）的审定基线；
    - 其后每次实际生效的写操作（单条/批量读数提交、作废/恢复、规则修订）
      在同一写事务内追加一条连续编号履历，幂等重放、签发证书与失败请求不占号；
    - 每条给出操作类型、涉及的读数 ID 或规则版本、操作前后结论与最长连续
      超限时长，以及前后状态不同的半开区段；
    - 窗口不存在 -> 404；from_seq 为负等非法序号 -> 422（参数错误）；
      from_seq 超过当前最大序号时返回空列表（不报错）。
    """
    services.get_window_row(conn, window_id)
    return services.list_audit_entries(conn, window_id, from_seq=from_seq, limit=limit)


# ---------------------------------------------------------------- 读数

@app.get("/api/v1/windows/{window_id}/readings", response_model=list[ReadingOut],
         tags=["readings"])
def list_readings(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    services.get_window_row(conn, window_id)
    return [services.reading_to_dict(r) for r in services.list_active_readings(conn, window_id)]


@app.post(
    "/api/v1/windows/{window_id}/readings",
    response_model=ReadingSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["readings"],
)
def submit_reading(
    window_id: int,
    payload: ReadingSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    # 单个写事务：校验 + 替换/插入 + 基于同一状态立即重算；
    # 实际生效（created/updated/restored）时在同一事务内追加一条履历，
    # 幂等重放不产生履历。
    with write_transaction(conn):
        win = services.get_window_row(conn, window_id)
        services.ensure_audit_baseline(conn, win)
        before_evaluation = services.compute_evaluation(conn, win)
        effect, reading = services.submit_reading(conn, window_id, payload)
        evaluation = services.compute_evaluation(conn, win)
        if effect != "idempotent":
            operation_type = (
                OP_READING_RESTORE if effect == "restored" else OP_READING_SUBMIT
            )
            services.append_audit_entry(
                conn,
                window_id=window_id,
                operation_type=operation_type,
                before_evaluation=before_evaluation,
                after_evaluation=evaluation,
                rule_revision=win["rule_revision"],
                reading_id=payload.reading_id,
                reading_revision=reading["revision"],
                operation={
                    "effect": effect,
                    "reading_id": reading["reading_id"],
                    "revision": reading["revision"],
                    "measured_at": reading["measured_at"],
                    "temperature": reading["temperature"],
                },
            )
        return {"effect": effect, "reading": reading, "current_evaluation": evaluation}


@app.post(
    "/api/v1/windows/{window_id}/readings/batch",
    response_model=ReadingBatchSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["readings"],
)
def submit_reading_batch(
    window_id: int,
    payload: ReadingBatchSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """采样仪器补传：在一个写事务内整批提交非空读数批次。

    - 每项沿用单条提交的 reading_id/revision/measured_at/temperature 语义，
      以及时区、窗口闭区间边界、幂等/修订/冲突规则；同批 reading_id 不得重复。
    - 同刻唯一按批次应用后的**最终状态**校验：允许两条读数都以更高修订号
      互换采样时刻，不按写入顺序误拒。
    - 任一项冲突（409）或无效（422）整批拒收，活动读数与修订历史均不改变。
    - 成功时一次提交全部变化，逐项返回 created/updated/idempotent 及读数，
      并返回按最终状态一次性重算的审定结果；同批重放不产生新修订。
    """
    # 单个写事务：整批校验 + 整批落库 + 基于最终状态一次重算
    with write_transaction(conn):
        win = services.get_window_row(conn, window_id)
        services.ensure_audit_baseline(conn, win)
        before_evaluation = services.compute_evaluation(conn, win)
        results = services.submit_reading_batch(conn, window_id, payload)
        evaluation = services.compute_evaluation(conn, win)
        effects = [effect for effect, _ in results]
        # 批量只记一条履历：至少一项实际生效才追加；整批幂等重放不追加。
        if any(effect != "idempotent" for effect in effects):
            affected = [
                {"reading_id": reading["reading_id"], "revision": reading["revision"],
                 "measured_at": reading["measured_at"], "temperature": reading["temperature"],
                 "effect": effect}
                for effect, reading in results
            ]
            services.append_audit_entry(
                conn,
                window_id=window_id,
                operation_type=OP_READINGS_BATCH,
                before_evaluation=before_evaluation,
                after_evaluation=evaluation,
                rule_revision=win["rule_revision"],
                operation={
                    "effects": effects,
                    "item_count": len(results),
                    "affected_count": sum(1 for e in effects if e != "idempotent"),
                    "items": affected,
                },
            )
        return {
            "results": [{"effect": effect, "reading": reading}
                        for effect, reading in results],
            "current_evaluation": evaluation,
        }


@app.post(
    "/api/v1/windows/{window_id}/readings/{reading_id}/void",
    response_model=ReadingVoidResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["readings"],
)
def void_reading(
    window_id: int,
    reading_id: str,
    payload: ReadingVoidSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """作废一条误采读数：凭递增修订号 + 非空原因。

    - 作废作为该 ID 的一条修订留存（原因、操作时间、前一版本），
      当前审定立即剔除该读数并在同一事务内重算；
    - 相同作废修订号 + 相同原因重放 -> 幂等（effect=idempotent）；
      相同修订号原因不同、或修订号不高于当前 -> 409；
    - 读数从未存在 -> 404；已作废的读数不能再次以新修订号作废 -> 409；
    - 恢复：单条或批量提交更高修订号的读数（effect=restored）。
    """
    # 单个写事务：校验 + 作废落库 + 基于同一状态立即重算
    with write_transaction(conn):
        win = services.get_window_row(conn, window_id)
        services.ensure_audit_baseline(conn, win)
        before_evaluation = services.compute_evaluation(conn, win)
        effect, record = services.void_reading(conn, window_id, reading_id, payload)
        evaluation = services.compute_evaluation(conn, win)
        # 作废生效才追加履历；相同作废修订号+相同原因的幂等重放不追加。
        if effect == "voided":
            services.append_audit_entry(
                conn,
                window_id=window_id,
                operation_type=OP_READING_VOID,
                before_evaluation=before_evaluation,
                after_evaluation=evaluation,
                rule_revision=win["rule_revision"],
                reading_id=record["reading_id"],
                reading_revision=record["revision"],
                operation={
                    "effect": "voided",
                    "reading_id": record["reading_id"],
                    "revision": record["revision"],
                    "reason": record["reason"],
                    "voided_at": record["voided_at"],
                    "previous": record["previous"],
                },
            )
        return {"effect": effect, "void": record, "current_evaluation": evaluation}


@app.get(
    "/api/v1/windows/{window_id}/readings/{reading_id}/revisions",
    response_model=list[ReadingRevisionOut],
    tags=["readings"],
)
def list_reading_revisions(
    window_id: int,
    reading_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    """该读数 ID 的全部修订历史（含作废记录），按修订号升序。"""
    services.get_window_row(conn, window_id)
    return services.list_reading_revisions(conn, window_id, reading_id)


# ---------------------------------------------------------------- 签发

@app.post(
    "/api/v1/windows/{window_id}/certificates/issue",
    response_model=CertificateOut,
    status_code=status.HTTP_201_CREATED,
    tags=["certificates"],
)
def issue_certificate(
    window_id: int,
    payload: Optional[IssueRequest] = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    note = payload.note if payload is not None else None
    # BEGIN IMMEDIATE：与读数修订并发时，要么修订已提交（纳入本次签发），
    # 要么修订等待锁（在签发提交后生效，不改变本次快照）。
    with write_transaction(conn):
        return services.issue_certificate(conn, window_id, note)


@app.get("/api/v1/windows/{window_id}/certificates", response_model=list[CertificateOut],
         tags=["certificates"])
def list_certificates(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    return services.list_certificates(conn, window_id)


# 注意：本静态路径必须注册在 /api/v1/certificates/{certificate_id} 之前，
# 否则 "revision-report" 会先被 {certificate_id:int} 匹配并触发 422。
@app.get("/api/v1/certificates/revision-report",
         response_model=RevisionReportOut, tags=["certificates"])
def get_revision_report(
    old_certificate_id: int = Query(..., ge=1, description="旧版（较早签发）证书主键 ID"),
    new_certificate_id: int = Query(..., ge=1, description="新版（较晚签发）证书主键 ID"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """对同一审定窗口两张已签发证书的不可变快照生成修订影响报告。

    只依据所选快照比较，不读取当前活动读数：
    - 规则差异：两版快照各自留存的审定规则（规则版本 + 温度上下限、
      最长连续超限时长、最大采样间隔）前后值与变化，结论未变也可看出规则变更；
    - 读数差异（新增/变更读数，比较 revision、采样时间、温度）；
    - 两版审定时间线分别按各自快照的规则与读数重建、按全部区段边界对齐后，
      状态发生变化的半开区间，含变化前后状态、各侧读数 ID/修订号，未知侧区分
      “采样间隔超限（sample_gap）”与“边界空白（boundary_gap）”；
    - 汇总结论与最长连续超限时长的变化。
    窗口不一致、版本未按先后排列（旧版版本号须严格小于新版）、
    两 ID 相同均返回 422；证书不存在返回 404。
    """
    return services.revision_report(conn, old_certificate_id, new_certificate_id)


@app.get("/api/v1/certificates/{certificate_id}", response_model=CertificateOut,
         tags=["certificates"])
def get_certificate(
    certificate_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    return services.get_certificate(conn, certificate_id)
