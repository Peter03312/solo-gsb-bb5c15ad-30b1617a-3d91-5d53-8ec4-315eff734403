"""审定履历（审计日志）HTTP 集成测试。

覆盖：
- 建窗即有序号 0 基线（pending 空窗）；
- 单条 created/updated/restored 追加履历，幂等重放不追加、不占号；
- 批量提交整批只记一条（含逐项 effect），纯重放不追加；
- 作废/恢复、规则修订追加履历；签发证书不追加；失败请求（409/422）不追加；
- 每条记录操作前后结论、最长连续超限时长与状态变化半开区段；
- 结论不变的有效操作仍记录；
- 按窗口 + 起始序号顺序读取；窗口不存在 404；非法序号/分页 422；
- 既有窗口（缺基线）启动时按当时状态补建、编号从 0 续接，重启幂等；
- 并发写入按实际提交顺序连续留痕。

运行：PYTHONPATH=. python -m pytest tests/test_audit_api.py -q
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.main import app

    with TestClient(app) as c:
        yield c


WINDOW_BASE = "2026-10-01T00:00:00Z"


def make_window(client, minutes=60, tmin=18.0, tmax=22.0,
                max_exceed=300.0, max_gap=600.0, name=None):
    body = {
        "start_at": WINDOW_BASE,
        "end_at": (datetime.fromisoformat(WINDOW_BASE.replace("Z", "+00:00"))
                   + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
        "allowed_temp_min": tmin,
        "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    }
    if name:
        body["name"] = name
    r = client.post("/api/v1/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def reading(rid, rev, minute, temp):
    ts = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return {
        "reading_id": rid, "revision": rev,
        "measured_at": ts.isoformat().replace("+00:00", "Z"),
        "temperature": temp,
    }


def audit(client, wid, **params):
    return client.get(f"/api/v1/windows/{wid}/audit-entries", params=params).json()


def seqs(client, wid, **params):
    return [e["seq"] for e in audit(client, wid, **params)]


def submit(client, wid, body):
    return client.post(f"/api/v1/windows/{wid}/readings", json=body)


# ---------------------------------------------------------------- 基线

def test_window_creation_stores_seq0_baseline(client):
    win = make_window(client)
    entries = audit(client, win["id"])
    assert len(entries) == 1
    base = entries[0]
    assert base["seq"] == 0
    assert base["operation_type"] == "baseline"
    assert base["operation_type_zh"] == "审定基线"
    assert base["source"] == "created"
    assert base["verdict_before"] is None
    assert base["verdict_after"] == "pending"  # 空窗：整窗未知
    assert base["verdict_before_label_zh"] is None
    assert base["verdict_after_label_zh"] == "待定"
    assert base["max_continuous_exceeded_seconds_before"] is None
    assert base["max_continuous_exceeded_seconds_after"] == 0.0
    assert base["changed_segments"] == []
    assert base["rule_revision"] == 1
    assert base["reading_id"] is None and base["rule_revision_ref"] is None


# ---------------------------------------------------------------- 单条提交/幂等/失败

def test_single_submit_appends_entry_with_before_after(client):
    wid = make_window(client)["id"]
    r = submit(client, wid, reading("a", 1, 0, 23.0))
    assert r.status_code == 201
    entry = audit(client, wid)[-1]
    assert entry["seq"] == 1
    assert entry["operation_type"] == "reading_submit"
    assert entry["reading_id"] == "a" and entry["reading_revision"] == 1
    assert entry["verdict_before"] == "pending"
    assert entry["operation"]["effect"] == "created"
    assert entry["operation"]["temperature"] == 23.0
    # 读数 a 恰在窗口起点：其自身覆盖时长为 0，尚无后续读数，时间线状态不变
    # （整窗仍未知）——但有效操作照样记录
    assert entry["verdict_after"] == "pending"
    assert entry["changed_segments"] == []

    # 再提交一条 10 分钟后的读数：[00:00,00:10) 由未知变超限，结论仍 pending
    r = submit(client, wid, reading("b", 1, 10, 20.0))
    assert r.status_code == 201
    entry = audit(client, wid)[-1]
    seg = entry["changed_segments"]
    assert len(seg) == 1
    assert seg[0]["status_before"] == "unknown" and seg[0]["status_after"] == "exceeded"
    assert seg[0]["start"].endswith("00:00:00Z")
    assert seg[0]["duration_seconds"] == 600.0
    # 结论未变（pending），最长连续超限仍为 0（600s 未超 300s 限值？600>300 -> fail）
    # 600s 连续超限严格大于 300s 限值 -> fail
    assert entry["verdict_before"] == "pending" and entry["verdict_after"] == "fail"


def test_idempotent_replay_creates_no_entry(client):
    wid = make_window(client)["id"]
    body = reading("a", 1, 0, 20.0)
    submit(client, wid, body)
    submit(client, wid, body)  # 幂等重放
    assert seqs(client, wid) == [0, 1]


def test_updated_effect_recorded(client):
    wid = make_window(client)["id"]
    # a 正常、b 超限（00:10->00:20 连续 600s > 300 -> fail）、c 正常
    submit(client, wid, reading("a", 1, 0, 20.0))
    submit(client, wid, reading("b", 1, 10, 23.0))
    submit(client, wid, reading("c", 1, 20, 20.0))
    assert audit(client, wid)[-1]["verdict_after"] == "fail"
    r = submit(client, wid, reading("b", 2, 10, 20.0))
    assert r.status_code == 201 and r.json()["effect"] == "updated"
    entry = audit(client, wid)[-1]
    assert entry["operation_type"] == "reading_submit"
    assert entry["operation"]["effect"] == "updated"
    assert entry["reading_revision"] == 2
    assert entry["verdict_before"] == "fail"
    assert entry["verdict_after"] == "pending"
    assert entry["max_continuous_exceeded_seconds_before"] == 600.0
    assert entry["max_continuous_exceeded_seconds_after"] == 0.0
    assert [(s["status_before"], s["status_after"])
            for s in entry["changed_segments"]] == [("exceeded", "normal")]


def test_failed_requests_create_no_entry(client):
    wid = make_window(client)["id"]
    submit(client, wid, reading("a", 1, 0, 20.0))
    # 409 旧修订号、409 内容冲突、422 窗口外
    assert submit(client, wid, reading("a", 0, 0, 20.0)).status_code == 422  # schema ge=1
    assert submit(client, wid, reading("a", 1, 0, 99.0)).status_code == 409
    assert submit(client, wid, reading("x", 1, 99, 20.0)).status_code == 422
    assert seqs(client, wid) == [0, 1]


# ---------------------------------------------------------------- 批量

def test_batch_appends_single_entry(client):
    wid = make_window(client)["id"]
    r = client.post(f"/api/v1/windows/{wid}/readings/batch", json={"readings": [
        reading("a", 1, 0, 20.0), reading("b", 1, 10, 23.0)]})
    assert r.status_code == 201, r.text
    entries = audit(client, wid)
    assert seqs(client, wid) == [0, 1]
    e = entries[-1]
    assert e["operation_type"] == "readings_batch"
    assert e["operation"]["effects"] == ["created", "created"]
    assert e["operation"]["item_count"] == 2
    assert e["operation"]["affected_count"] == 2
    assert [i["reading_id"] for i in e["operation"]["items"]] == ["a", "b"]
    assert e["reading_id"] is None  # 批量不在列上绑定单个读数


def test_batch_pure_replay_no_entry(client):
    wid = make_window(client)["id"]
    body = {"readings": [reading("a", 1, 0, 20.0)]}
    client.post(f"/api/v1/windows/{wid}/readings/batch", json=body)
    client.post(f"/api/v1/windows/{wid}/readings/batch", json=body)
    assert seqs(client, wid) == [0, 1]


def test_batch_partial_effect_still_one_entry(client):
    # 批内一项新建、一项幂等：整批仍只记一条
    wid = make_window(client)["id"]
    submit(client, wid, reading("a", 1, 0, 20.0))
    client.post(f"/api/v1/windows/{wid}/readings/batch", json={"readings": [
        reading("a", 1, 0, 20.0), reading("b", 1, 10, 20.0)]})
    assert seqs(client, wid) == [0, 1, 2]
    e = audit(client, wid)[-1]
    assert e["operation"]["effects"] == ["idempotent", "created"]
    assert e["operation"]["affected_count"] == 1


def test_batch_failed_rollback_no_entry(client):
    wid = make_window(client)["id"]
    r = client.post(f"/api/v1/windows/{wid}/readings/batch", json={"readings": [
        reading("a", 1, 0, 20.0),
        reading("a", 2, 5, 20.0)]})  # 批内重复 ID -> 422 整批回滚
    assert r.status_code == 422
    assert seqs(client, wid) == [0]


# ---------------------------------------------------------------- 作废 / 恢复

def test_void_and_restore_entries(client):
    wid = make_window(client)["id"]
    submit(client, wid, reading("a", 1, 0, 23.0))
    submit(client, wid, reading("b", 1, 10, 20.0))
    r = client.post(f"/api/v1/windows/{wid}/readings/a/void",
                    json={"revision": 2, "reason": "误采：未校准"})
    assert r.status_code == 201, r.text
    e = audit(client, wid)[-1]
    assert e["operation_type"] == "reading_void"
    assert e["reading_id"] == "a" and e["reading_revision"] == 2
    assert e["operation"]["reason"] == "误采：未校准"
    assert e["operation"]["previous"] == {
        "revision": 1, "measured_at": "2026-10-01T00:00:00Z", "temperature": 23.0}
    assert any(s["status_after"] == "unknown" for s in e["changed_segments"])

    # 作废重放幂等 -> 无新条目
    client.post(f"/api/v1/windows/{wid}/readings/a/void",
                json={"revision": 2, "reason": "误采：未校准"})
    assert audit(client, wid)[-1]["seq"] == 3

    # 恢复 -> reading_restore
    r = submit(client, wid, reading("a", 3, 0, 20.0))
    assert r.status_code == 201 and r.json()["effect"] == "restored"
    e = audit(client, wid)[-1]
    assert e["operation_type"] == "reading_restore"
    assert e["operation"]["effect"] == "restored"
    assert e["reading_revision"] == 3
    assert any(s["status_before"] == "unknown" and s["status_after"] == "normal"
               for s in e["changed_segments"])


def test_void_failures_no_entry(client):
    wid = make_window(client)["id"]
    submit(client, wid, reading("a", 1, 0, 20.0))
    # 低修订号 409、空原因 422、读数不存在 404
    assert client.post(f"/api/v1/windows/{wid}/readings/a/void",
                       json={"revision": 1, "reason": "x"}).status_code == 409
    assert client.post(f"/api/v1/windows/{wid}/readings/a/void",
                       json={"revision": 2, "reason": "  "}).status_code == 422
    assert client.post(f"/api/v1/windows/{wid}/readings/ghost/void",
                       json={"revision": 1, "reason": "x"}).status_code == 404
    assert seqs(client, wid) == [0, 1]


# ---------------------------------------------------------------- 规则修订 / 签发

def test_rule_revision_entry(client):
    wid = make_window(client, max_gap=600.0)["id"]
    submit(client, wid, reading("a", 1, 0, 20.0))
    submit(client, wid, reading("b", 1, 10, 20.0))
    r = client.post(f"/api/v1/windows/{wid}/rules/revisions", json={
        "target_revision": 2,
        "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
        "max_continuous_exceed_seconds": 300, "max_sample_interval_seconds": 300,
        "reason": "收紧采样间隔"})
    assert r.status_code == 201, r.text
    e = audit(client, wid)[-1]
    assert e["operation_type"] == "rule_revision"
    assert e["rule_revision"] == 2 and e["rule_revision_ref"] == 2
    assert e["operation"]["rule_revision"] == 2
    assert e["operation"]["previous_rule_revision"] == 1
    assert e["operation"]["rules"]["max_sample_interval_seconds"] == 300.0
    assert e["operation"]["previous_rules"]["max_sample_interval_seconds"] == 600.0
    assert e["operation"]["reason"] == "收紧采样间隔"
    # 间隔 600s 在新规则下超限 -> normal 变 unknown（结论可能不变 pending）
    assert e["verdict_before"] == e["verdict_after"] == "pending"
    seg = e["changed_segments"]
    assert len(seg) == 1
    assert seg[0]["status_before"] == "normal" and seg[0]["status_after"] == "unknown"
    assert seg[0]["duration_seconds"] == 600.0


def test_rule_revision_idempotent_and_failures_no_entry(client):
    wid = make_window(client)["id"]
    body = {"target_revision": 2, "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
            "max_continuous_exceed_seconds": 300, "max_sample_interval_seconds": 600,
            "reason": "x"}
    assert client.post(f"/api/v1/windows/{wid}/rules/revisions", json=body).status_code == 201
    assert client.post(f"/api/v1/windows/{wid}/rules/revisions", json=body).status_code == 201
    # 跳号 409
    assert client.post(f"/api/v1/windows/{wid}/rules/revisions", json={
        **body, "target_revision": 4}).status_code == 409
    assert seqs(client, wid) == [0, 1]


def test_certificate_issue_no_entry(client):
    wid = make_window(client)["id"]
    submit(client, wid, reading("a", 1, 0, 20.0))
    client.post(f"/api/v1/windows/{wid}/certificates/issue", json={"note": "n"})
    client.post(f"/api/v1/windows/{wid}/certificates/issue", json={})
    assert seqs(client, wid) == [0, 1]


# ---------------------------------------------------------------- 读取约定

def test_audit_read_order_paging_and_filters(client):
    wid = make_window(client)["id"]
    for i in range(3):
        submit(client, wid, reading(f"r{i}", 1, i, 20.0))
    assert seqs(client, wid) == [0, 1, 2, 3]
    # 默认从基线开始
    assert seqs(client, wid, from_seq=0) == [0, 1, 2, 3]
    # 起始序号（含）
    assert seqs(client, wid, from_seq=2) == [2, 3]
    # 分页
    assert seqs(client, wid, limit=2) == [0, 1]
    assert seqs(client, wid, from_seq=2, limit=2) == [2, 3]
    # 超出最大序号返回空列表（不报错）
    assert audit(client, wid, from_seq=999) == []


def test_audit_window_missing_404_and_bad_params_422(client):
    assert client.get("/api/v1/windows/999/audit-entries").status_code == 404
    assert client.get("/api/v1/windows/1/audit-entries",
                      params={"from_seq": -1}).status_code == 422
    assert client.get("/api/v1/windows/1/audit-entries",
                      params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/windows/1/audit-entries",
                      params={"limit": 1001}).status_code == 422


def test_audit_scoped_per_window(client):
    w1 = make_window(client)["id"]
    w2 = make_window(client)["id"]
    submit(client, w1, reading("a", 1, 0, 20.0))
    # 两个窗口各自从 seq 0 独立编号
    assert seqs(client, w1) == [0, 1]
    assert seqs(client, w2) == [0]


# ---------------------------------------------------------------- 既有窗口补建

def test_legacy_window_baseline_backfilled_on_startup(client, tmp_path, monkeypatch):
    from app.database import connect, init_db, write_transaction
    from app import services
    from app.schemas import ReadingSubmit, WindowCreate

    # 先在“新版本”下建窗并写读数，再模拟旧库：删除履历表
    conn = connect()
    init_db(conn)
    try:
        with write_transaction(conn):
            w = services.create_window(conn, WindowCreate(
                start_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                end_at=datetime(2026, 10, 1, 1, tzinfo=timezone.utc),
                allowed_temp_min=18, allowed_temp_max=22,
                max_continuous_exceed_seconds=300, max_sample_interval_seconds=600))
        with write_transaction(conn):
            services.submit_reading(conn, w["id"], ReadingSubmit(
                reading_id="a", revision=1,
                measured_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                temperature=20.0))
        conn.execute("DROP TABLE window_audit_entries")
        conn.commit()
    finally:
        conn.close()

    # 重新走一次 lifespan（新 TestClient 触发 init_db + backfill）
    from app.main import app
    with TestClient(app) as c:
        entries = c.get("/api/v1/windows/1/audit-entries").json()
        base = entries[0]
        assert base["seq"] == 0
        assert base["source"] == "backfilled"
        assert base["operation_type"] == "baseline"
        assert base["operation"]["source"] == "backfilled"
        # 补建不得伪造更早操作：记录时间不早于窗口创建时间
        win = c.get("/api/v1/windows/1").json()
        assert base["created_at"] >= win["created_at"]
        # 补建后生效操作从序号 1 续接
        r = c.post("/api/v1/windows/1/readings",
                   json=reading("b", 1, 10, 20.0))
        assert r.status_code == 201, r.text
        assert [e["seq"] for e in c.get("/api/v1/windows/1/audit-entries").json()] == [0, 1]


# ---------------------------------------------------------------- 并发

def test_concurrent_writes_numbered_in_commit_order(client):
    wid = make_window(client)["id"]
    seqs_seen = []

    def submit_temp(temp, barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post(f"/api/v1/windows/{wid}/readings",
                   json=reading(f"r{temp}", 1, int(temp), 20.0))
        assert r.status_code == 201, r.text

    n = 6
    barrier = threading.Barrier(n)
    threads = [threading.Thread(target=submit_temp, args=(i, barrier)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = audit(client, wid, limit=1000)
    # 基线 + n 条，序号连续 0..n，无重复无空洞，按提交顺序
    assert [e["seq"] for e in entries] == list(range(n + 1))
    assert [e["operation_type"] for e in entries[1:]] == ["reading_submit"] * n
    # 每条读数恰好被记录一次
    assert sorted(e["reading_id"] for e in entries[1:]) == sorted(f"r{i}" for i in range(n))
