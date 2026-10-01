"""HTTP API 集成测试：审定履历（audit history）。

覆盖：
- 建窗即在同一事务保存序号 0 基线（当时规则/空读数，origin=window_created）；
- 单条提交 created/updated/restored 各追加一条；作废追加一条；
  规则修订实际生效追加一条；批量只记一条（含部分幂等）；
- 幂等重放、签发证书、失败请求不产生履历与序号；
- 即使结论不变也记录生效操作；批量操作只记一条；
- 记录操作类型、涉及读数 ID/规则版本、前后结论与最长连续超限时长、
  前后状态不同的半开区段；
- 按窗口与起始序号顺序读取、分页 next_start_seq、窗口不存在 404、非法序号 422；
- 既有窗口升级补建基线（origin=backfilled，当时规则+活动读数，不补造更早操作）；
- 并发写入按实际提交顺序取得连续序号。

运行：PYTHONPATH=. python -m pytest tests/test_audit_api.py -q
"""
from __future__ import annotations

import sqlite3
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
    # 延迟导入 app，确保 connect() 读取被覆盖后的 settings
    from app.main import app

    with TestClient(app) as c:
        yield c


WINDOW_BASE = "2026-10-01T00:00:00Z"


def make_window(client, start=WINDOW_BASE, minutes=60, tmin=18.0, tmax=22.0,
                max_exceed=300.0, max_gap=600.0, name=None):
    body = {
        "start_at": start,
        "end_at": (datetime.fromisoformat(start.replace("Z", "+00:00"))
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


def history(client, wid, **params):
    return client.get(f"/api/v1/windows/{wid}/audit-history", params=params).json()


def seqs(client, wid):
    return [(e["seq"], e["operation"]) for e in history(client, wid)["entries"]]


# ---------------------------------------------------------------- 基线

def test_baseline_seq0_created_with_window(client):
    win = make_window(client)
    h = history(client, win["id"])
    assert [e["seq"] for e in h["entries"]] == [0]
    base = h["entries"][0]
    assert base["operation"] == "baseline"
    assert base["before"] is None
    assert base["detail"] == {"origin": "window_created"}
    assert base["reading_ids"] == [] and base["rule_revisions"] == []
    assert base["changed_segments"] == []
    after = base["after"]
    # 建窗时无活动读数：整窗未知 -> pending，规则版本为初始第 1 版
    assert after == {
        "verdict": "pending",
        "max_continuous_exceeded_seconds": 0.0,
        "rule_revision": 1,
        "reading_count": 0,
    }
    assert base["occurred_at"]


def test_baseline_one_per_window_independent_numbering(client):
    w1 = make_window(client)
    w2 = make_window(client)
    assert seqs(client, w1["id"]) == [(0, "baseline")]
    assert seqs(client, w2["id"]) == [(0, "baseline")]


# ---------------------------------------------------------------- 单条提交

def test_single_submit_effective_ops_appended(client):
    wid = make_window(client, max_gap=600.0)["id"]
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 23.0))
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("b", 1, 10, 20.0))
    entries = history(client, wid)["entries"]
    assert [(e["seq"], e["operation"]) for e in entries] == [
        (0, "baseline"), (1, "reading_submit"), (2, "reading_submit")]
    e1 = entries[1]
    assert e1["reading_ids"] == ["a"]
    assert e1["detail"]["effect"] == "created"
    assert e1["detail"]["reading"]["reading_id"] == "a"
    assert e1["before"]["reading_count"] == 0
    assert e1["after"]["reading_count"] == 1
    # 单条读数只“覆盖到下一条读数”：仅有 a@00:00 时其自身区段时长为 0，
    # 整窗仍为尾部未知（状态不变、最长连续超限 0）——履历照常记录。
    assert e1["after"]["max_continuous_exceeded_seconds"] == 0.0
    assert e1["changed_segments"] == []
    e2 = entries[2]
    assert e2["reading_ids"] == ["b"]
    assert e2["detail"]["effect"] == "created"
    assert e2["before"]["reading_count"] == 1
    # b 提交后 a 才覆盖 [00:00,00:10) 超限 600s -> fail
    assert e2["after"]["verdict"] == "fail"
    assert e2["after"]["max_continuous_exceeded_seconds"] == 600.0
    pairs = {(s["before"]["status"], s["after"]["status"]) for s in e2["changed_segments"]}
    assert ("unknown", "exceeded") in pairs


def test_idempotent_replay_produces_no_entry(client):
    wid = make_window(client)["id"]
    body = reading("a", 1, 0, 20.0)
    client.post(f"/api/v1/windows/{wid}/readings", json=body)
    r = client.post(f"/api/v1/windows/{wid}/readings", json=body)
    assert r.json()["effect"] == "idempotent"
    assert seqs(client, wid) == [(0, "baseline"), (1, "reading_submit")]


def test_failed_request_produces_no_entry(client):
    wid = make_window(client)["id"]
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))
    # 窗口外 -> 422；旧修订号 -> 409；同刻不同读数 -> 422
    assert client.post(f"/api/v1/windows/{wid}/readings",
                       json=reading("x", 1, 90, 20.0)).status_code == 422
    assert client.post(f"/api/v1/windows/{wid}/readings",
                       json=reading("a", 1, 0, 99.0)).status_code == 409
    assert client.post(f"/api/v1/windows/{wid}/readings",
                       json=reading("b", 1, 0, 20.0)).status_code == 422
    assert seqs(client, wid) == [(0, "baseline"), (1, "reading_submit")]


def test_unchanged_verdict_still_recorded(client):
    # 正常读数补入后窗口仍有边界未知：结论始终为 pending，但生效操作仍记录
    wid = make_window(client, max_gap=600.0)["id"]
    r1 = client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 10, 20.0))
    assert r1.json()["current_evaluation"]["verdict"] == "pending"
    # 首条读数自身覆盖时长为 0：状态无变化的生效操作也留痕
    e1 = history(client, wid)["entries"][-1]
    assert e1["operation"] == "reading_submit" and e1["changed_segments"] == []

    r2 = client.post(f"/api/v1/windows/{wid}/readings", json=reading("b", 1, 20, 20.0))
    assert r2.json()["current_evaluation"]["verdict"] == "pending"
    e2 = history(client, wid)["entries"][-1]
    assert e2["before"]["verdict"] == "pending" and e2["after"]["verdict"] == "pending"
    # 状态不同的半开区段仍被记录（边界未知 -> 正常：a 覆盖 [00:10,00:20)）
    statuses = {(s["before"]["status"], s["after"]["status"])
                for s in e2["changed_segments"]}
    assert ("unknown", "normal") in statuses


def test_updated_revision_recorded(client):
    wid = make_window(client, max_gap=600.0)["id"]
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 23.0))
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("b", 1, 10, 20.0))
    r = client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 2, 0, 20.0))
    assert r.json()["effect"] == "updated"
    entries = history(client, wid)["entries"]
    e = entries[-1]
    assert e["operation"] == "reading_submit"
    assert e["detail"]["effect"] == "updated"
    assert e["detail"]["reading"]["revision"] == 2
    # 超限消失：最长连续超限 600 -> 0，区段 超限 -> 正常
    assert e["before"]["max_continuous_exceeded_seconds"] == 600.0
    assert e["after"]["max_continuous_exceeded_seconds"] == 0.0
    pairs = {(s["before"]["status"], s["after"]["status"]) for s in e["changed_segments"]}
    assert ("exceeded", "normal") in pairs


# ---------------------------------------------------------------- 批量

def test_batch_single_entry_with_all_reading_ids(client):
    wid = make_window(client)["id"]
    r = client.post(f"/api/v1/windows/{wid}/readings/batch", json={"readings": [
        reading("a", 1, 0, 20.0), reading("b", 1, 10, 20.0)]})
    assert r.status_code == 201
    entries = history(client, wid)["entries"]
    assert [(e["seq"], e["operation"]) for e in entries] == [
        (0, "baseline"), (1, "readings_batch")]
    e = entries[1]
    assert e["reading_ids"] == ["a", "b"]  # 按请求顺序
    assert [i["effect"] for i in e["detail"]["results"]] == ["created", "created"]
    assert e["before"]["reading_count"] == 0 and e["after"]["reading_count"] == 2


def test_batch_mixed_effects_one_entry(client):
    wid = make_window(client)["id"]
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))
    r = client.post(f"/api/v1/windows/{wid}/readings/batch", json={"readings": [
        reading("a", 1, 0, 20.0),       # idempotent
        reading("b", 1, 10, 20.0)]})    # created
    assert [x["effect"] for x in r.json()["results"]] == ["idempotent", "created"]
    # 含实际生效项：整批只记一条
    assert seqs(client, wid) == [(0, "baseline"), (1, "reading_submit"),
                                 (2, "readings_batch")]
    e = history(client, wid)["entries"][-1]
    assert [i["effect"] for i in e["detail"]["results"]] == ["idempotent", "created"]
    assert e["reading_ids"] == ["a", "b"]


def test_batch_all_idempotent_no_entry(client):
    wid = make_window(client)["id"]
    batch = {"readings": [reading("a", 1, 0, 20.0), reading("b", 1, 10, 20.0)]}
    client.post(f"/api/v1/windows/{wid}/readings/batch", json=batch)
    r = client.post(f"/api/v1/windows/{wid}/readings/batch", json=batch)
    assert all(x["effect"] == "idempotent" for x in r.json()["results"])
    assert seqs(client, wid) == [(0, "baseline"), (1, "readings_batch")]


def test_batch_rolled_back_no_entry(client):
    wid = make_window(client)["id"]
    r = client.post(f"/api/v1/windows/{wid}/readings/batch", json={"readings": [
        reading("b", 1, 0, 20.0),      # 将创建
        reading("x", 1, 0, 21.0)]})    # 最终状态同刻 -> 422 整批回滚
    assert r.status_code == 422
    assert seqs(client, wid) == [(0, "baseline")]


# ---------------------------------------------------------------- 作废 / 恢复

def test_void_and_restore_entries(client):
    wid = make_window(client, max_gap=600.0)["id"]
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 23.0))
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("b", 1, 10, 20.0))
    r = client.post(f"/api/v1/windows/{wid}/readings/b/void",
                    json={"revision": 2, "reason": "误采：未校准"})
    assert r.status_code == 201 and r.json()["effect"] == "voided"
    entries = history(client, wid)["entries"]
    ev = entries[-1]
    assert ev["operation"] == "reading_void"
    assert ev["reading_ids"] == ["b"]
    assert ev["detail"] == {
        "effect": "voided",
        "revision": 2,
        "reason": "误采：未校准",
        "previous": {"revision": 1,
                     "measured_at": "2026-10-01T00:10:00Z",
                     "temperature": 20.0},
    }
    assert ev["before"]["reading_count"] == 2 and ev["after"]["reading_count"] == 1
    # a 与被剔除的 b 间隔 600s 等于上限仍有效；b 之后的尾部未知前移
    assert ev["changed_segments"]

    # 幂等作废重放不留痕
    r2 = client.post(f"/api/v1/windows/{wid}/readings/b/void",
                     json={"revision": 2, "reason": "误采：未校准"})
    assert r2.json()["effect"] == "idempotent"
    assert len(history(client, wid)["entries"]) == len(entries)

    # 恢复走提交入口，effect=restored
    r3 = client.post(f"/api/v1/windows/{wid}/readings",
                     json=reading("b", 3, 10, 20.0))
    assert r3.json()["effect"] == "restored"
    last = history(client, wid)["entries"][-1]
    assert last["operation"] == "reading_submit"
    assert last["detail"]["effect"] == "restored"
    assert last["reading_ids"] == ["b"]


def test_void_failure_no_entry(client):
    wid = make_window(client)["id"]
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))
    assert client.post(f"/api/v1/windows/{wid}/readings/a/void",
                       json={"revision": 1, "reason": "x"}).status_code == 409
    assert client.post(f"/api/v1/windows/{wid}/readings/ghost/void",
                       json={"revision": 1, "reason": "x"}).status_code == 404
    assert client.post(f"/api/v1/windows/{wid}/readings/a/void",
                       json={"revision": 2, "reason": "  "}).status_code == 422
    assert seqs(client, wid) == [(0, "baseline"), (1, "reading_submit")]


# ---------------------------------------------------------------- 规则修订

def test_rules_revision_entry(client):
    wid = make_window(client, max_gap=600.0)["id"]
    for m in (0, 10, 20):
        client.post(f"/api/v1/windows/{wid}/readings",
                    json=reading(f"r{m}", 1, m, 20.0))
    r = client.post(f"/api/v1/windows/{wid}/rules/revisions", json={
        "target_revision": 2, "allowed_temp_min": 18.0, "allowed_temp_max": 21.5,
        "max_continuous_exceed_seconds": 240, "max_sample_interval_seconds": 600,
        "reason": "季度复核"})
    assert r.status_code == 201 and r.json()["effect"] == "revised"
    e = history(client, wid)["entries"][-1]
    assert e["operation"] == "rules_revision"
    assert e["reading_ids"] == []
    assert e["rule_revisions"] == [2]
    assert e["detail"]["target_revision"] == 2
    assert e["detail"]["previous_revision"] == 1
    assert e["detail"]["reason"] == "季度复核"
    assert e["detail"]["rules"]["allowed_temp_max"] == 21.5
    assert e["detail"]["previous_rules"]["allowed_temp_max"] == 22.0
    assert e["before"]["rule_revision"] == 1
    assert e["after"]["rule_revision"] == 2
    # 读数温度均 20.0，结论保持 pass（末尾未知? 末读 00:20 < 01:00 -> pending）。
    # 无论结论是否变化，规则修订生效都记录。
    assert e["after"]["verdict"] in ("pass", "pending")


def test_rules_revision_changes_timeline_segments(client):
    # 收紧最大采样间隔：原本正常的相邻区间变为未知 -> changed_segments 记录
    wid = make_window(client, max_gap=600.0)["id"]
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("b", 1, 10, 20.0))
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("c", 1, 60, 20.0))
    r = client.post(f"/api/v1/windows/{wid}/rules/revisions", json={
        "target_revision": 2, "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
        "max_continuous_exceed_seconds": 300, "max_sample_interval_seconds": 300,
        "reason": "收紧采样间隔"})
    assert r.status_code == 201
    e = history(client, wid)["entries"][-1]
    pairs = {(s["before"]["status"], s["after"]["status"]) for s in e["changed_segments"]}
    # 00:10 -> 01:00 间隔 3000s 原本正常（b 保持），新规 300s 下未知
    assert ("normal", "unknown") in pairs
    seg = next(s for s in e["changed_segments"]
               if s["before"]["status"] == "normal" and s["after"]["status"] == "unknown")
    assert seg["after"]["evidence"]["unknown_reason"] == "sample_gap"


def test_rules_revision_idempotent_and_failures_no_entry(client):
    wid = make_window(client)["id"]
    payload = {"target_revision": 2, "allowed_temp_min": 18.0, "allowed_temp_max": 21.5,
               "max_continuous_exceed_seconds": 240, "max_sample_interval_seconds": 600,
               "reason": "季度复核"}
    assert client.post(f"/api/v1/windows/{wid}/rules/revisions", json=payload).status_code == 201
    # 同号同内容重放 -> 幂等，不留痕
    assert client.post(f"/api/v1/windows/{wid}/rules/revisions",
                       json=payload).json()["effect"] == "idempotent"
    # 跳号 409、空原因 422
    bad = dict(payload, target_revision=4)
    assert client.post(f"/api/v1/windows/{wid}/rules/revisions", json=bad).status_code == 409
    blank = dict(payload, target_revision=3, reason="  ")
    assert client.post(f"/api/v1/windows/{wid}/rules/revisions", json=blank).status_code == 422
    assert seqs(client, wid) == [(0, "baseline"), (1, "rules_revision")]


# ---------------------------------------------------------------- 签发不产生履历

def test_certificate_issue_no_entry(client):
    wid = make_window(client)["id"]
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))
    client.post(f"/api/v1/windows/{wid}/certificates/issue", json={"note": "首签"})
    client.post(f"/api/v1/windows/{wid}/certificates/issue", json={})
    assert seqs(client, wid) == [(0, "baseline"), (1, "reading_submit")]


# ---------------------------------------------------------------- 读取 / 分页 / 错误

def test_read_ordered_by_seq_and_start_seq(client):
    wid = make_window(client)["id"]
    for i, m in enumerate((0, 10, 20), start=1):
        client.post(f"/api/v1/windows/{wid}/readings",
                    json=reading(f"r{m}", 1, m, 20.0))
    h = history(client, wid, start_seq=2)
    assert [e["seq"] for e in h["entries"]] == [2, 3]
    assert h["window_id"] == wid and h["start_seq"] == 2 and h["limit"] == 100
    # 从中间序号读，不含基线
    assert all(e["operation"] != "baseline" for e in h["entries"])


def test_pagination_next_start_seq(client):
    wid = make_window(client)["id"]
    for m in (0, 10, 20):
        client.post(f"/api/v1/windows/{wid}/readings",
                    json=reading(f"r{m}", 1, m, 20.0))
    # 共 4 条（0..3）
    p1 = history(client, wid, start_seq=0, limit=2)
    assert [e["seq"] for e in p1["entries"]] == [0, 1]
    assert p1["next_start_seq"] == 2
    p2 = history(client, wid, start_seq=2, limit=2)
    assert [e["seq"] for e in p2["entries"]] == [2, 3]
    assert p2["next_start_seq"] is None
    # 超出末尾
    p3 = history(client, wid, start_seq=99)
    assert p3["entries"] == [] and p3["next_start_seq"] is None
    # 翻页走完全部序号连续
    seq, seen = 0, []
    while True:
        pg = history(client, wid, start_seq=seq, limit=1)
        seen += [e["seq"] for e in pg["entries"]]
        if pg["next_start_seq"] is None:
            break
        seq = pg["next_start_seq"]
    assert seen == [0, 1, 2, 3]


def test_history_window_not_found_404(client):
    r = client.get("/api/v1/windows/999/audit-history")
    assert r.status_code == 404


def test_history_invalid_params_422(client):
    wid = make_window(client)["id"]
    assert client.get(f"/api/v1/windows/{wid}/audit-history?start_seq=-1").status_code == 422
    assert client.get(f"/api/v1/windows/{wid}/audit-history?start_seq=x").status_code == 422
    assert client.get(f"/api/v1/windows/{wid}/audit-history?limit=0").status_code == 422
    assert client.get(f"/api/v1/windows/{wid}/audit-history?limit=501").status_code == 422
    assert client.get("/api/v1/windows/notint/audit-history").status_code == 422


# ---------------------------------------------------------------- 升级补建基线

def test_existing_window_backfilled_baseline(tmp_path, monkeypatch):
    db_file = tmp_path / "old.db"
    # 手工构造旧版库（无 window_audit_history 表；windows 无 rule_revision 列）
    raw = sqlite3.connect(db_file)
    raw.executescript(
        """
        CREATE TABLE windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
            start_at TEXT NOT NULL, end_at TEXT NOT NULL,
            allowed_temp_min REAL NOT NULL, allowed_temp_max REAL NOT NULL,
            max_continuous_exceed_seconds REAL NOT NULL,
            max_sample_interval_seconds REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')));
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, window_id INTEGER NOT NULL,
            reading_id TEXT NOT NULL, revision INTEGER NOT NULL,
            measured_at TEXT NOT NULL, temperature REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE(window_id, reading_id), UNIQUE(window_id, measured_at));
        """
    )
    raw.execute(
        "INSERT INTO windows (name,start_at,end_at,allowed_temp_min,allowed_temp_max,"
        "max_continuous_exceed_seconds,max_sample_interval_seconds,created_at) "
        "VALUES ('旧柜','2026-10-01T00:00:00Z','2026-10-01T01:00:00Z',18,22,300,600,"
        "'2026-09-01T00:00:00Z')")
    raw.execute(
        "INSERT INTO readings (window_id,reading_id,revision,measured_at,temperature,"
        "created_at) VALUES (1,'a',1,'2026-10-01T00:00:00Z',23.0,"
        "'2026-09-01T00:00:00Z')")
    raw.execute(
        "INSERT INTO readings (window_id,reading_id,revision,measured_at,temperature,"
        "created_at) VALUES (1,'b',1,'2026-10-01T00:10:00Z',20.0,"
        "'2026-09-01T00:00:00Z')")
    raw.commit()
    raw.close()

    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.main import app

    with TestClient(app) as c:
        h = history(c, 1)
        assert [e["seq"] for e in h["entries"]] == [0]
        base = h["entries"][0]
        assert base["operation"] == "baseline"
        assert base["detail"] == {"origin": "backfilled"}
        # 以当时规则与活动读数重算：a 在 [00:00,00:10) 超限 600s > 300 -> fail
        assert base["after"]["verdict"] == "fail"
        assert base["after"]["max_continuous_exceeded_seconds"] == 600.0
        assert base["after"]["reading_count"] == 2
        assert base["after"]["rule_revision"] == 1
        assert base["before"] is None
        # 补建时间不早于旧数据时间（不伪造更早操作）
        assert base["occurred_at"] >= "2026-09-01T00:00:00Z"

        # 补建后的新生效操作从序号 1 连续编号
        r = c.post("/api/v1/windows/1/readings",
                   json=reading("c", 1, 20, 20.0))
        assert r.status_code == 201
        assert [(e["seq"], e["operation"]) for e in history(c, 1)["entries"]] == [
            (0, "baseline"), (1, "reading_submit")]

        # 再次启动不重复补建
    with TestClient(app) as c:
        assert [e["seq"] for e in history(c, 1)["entries"]] == [0, 1]
        assert history(c, 1)["entries"][0]["detail"] == {"origin": "backfilled"}


# ---------------------------------------------------------------- 并发

def test_concurrent_writes_contiguous_seq_in_commit_order(client):
    wid = make_window(client)["id"]

    results = []

    def submit(rid, barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post(f"/api/v1/windows/{wid}/readings",
                   json=reading(rid, 1, 0 if rid == "a" else 10, 20.0))
        results.append((rid, r.status_code))

    barrier = threading.Barrier(2)
    threads = [threading.Thread(target=submit, args=(rid, barrier))
               for rid in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(code == 201 for _, code in results), results

    entries = history(client, wid)["entries"]
    # 基线 + 两条提交，序号 0/1/2 连续无缺号
    assert [e["seq"] for e in entries] == [0, 1, 2]
    assert [e["operation"] for e in entries] == [
        "baseline", "reading_submit", "reading_submit"]
    # 序号顺序即实际提交顺序（两条不同读数，各占一条）
    assert {tuple(e["reading_ids"]) for e in entries[1:]} == {("a",), ("b",)}
    assert entries[1]["occurred_at"] <= entries[2]["occurred_at"]
