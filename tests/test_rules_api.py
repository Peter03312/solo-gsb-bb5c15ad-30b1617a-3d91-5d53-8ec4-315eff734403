"""HTTP API 集成测试：窗口审定规则修订（rules revisions）。

覆盖：
- 初始规则视为第 1 版；目标号=当前版+1 生效，留存前后规则/原因/时间并立即重算；
- 等于当前版且四项规则值与原因均一致的重放幂等（不留新记录）；
  同号内容/原因冲突、旧号、跳号均 409 且不留新记录；
- 非法输入 422（温度下限>上限、时长非正、非有限数值、空原因）；窗口不存在 404；
- 窗口起止时间与读数保持不变；后续签发采用新规则，既有证书快照不变；
- 跨规则版本的修订影响报告分别依据两张证书快照计算时间线，
  展示四项规则前后值与变化（即使结论未变也可看出规则变更）；
- 并发修订串行化（同一目标号：一次生效，其余幂等或 409，不出现 500）。

运行：PYTHONPATH=. python -m pytest tests/test_rules_api.py -q
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


def rule_body(target, *, tmin=18.0, tmax=21.0, max_exceed=300.0, max_gap=600.0,
              reason="季度复核：收紧温度上限"):
    return {
        "target_revision": target,
        "allowed_temp_min": tmin,
        "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
        "reason": reason,
    }


def revise(client, wid, body):
    return client.post(f"/api/v1/windows/{wid}/rules/revisions", json=body)


def revise_raw(client, wid, raw_json):
    """发送手工拼接的 JSON（用于 NaN/Infinity 等非标准数值，httpx 的 json= 会拒编）。"""
    return client.post(f"/api/v1/windows/{wid}/rules/revisions",
                       content=raw_json.encode(),
                       headers={"Content-Type": "application/json"})


def fill_normal(client, wid, temps=20.0):
    """每 10 分钟一条正常读数（含两端边界），间隔 600s 恰好合法。"""
    for i, m in enumerate(range(0, 61, 10)):
        r = client.post(f"/api/v1/windows/{wid}/readings",
                        json=reading(f"r{m:02d}", 1, m, temps))
        assert r.status_code == 201, r.text


# ---------------------------------------------------------------- 初始版本与历史

def test_initial_rules_are_version_one(client):
    win = make_window(client)
    wid = win["id"]
    assert win["rule_revision"] == 1

    detail = client.get(f"/api/v1/windows/{wid}").json()
    assert detail["rule_revision"] == 1

    revs = client.get(f"/api/v1/windows/{wid}/rules/revisions").json()
    assert len(revs) == 1
    v1 = revs[0]
    assert v1["revision"] == 1
    assert v1["rules"] == {
        "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
        "max_continuous_exceed_seconds": 300.0,
        "max_sample_interval_seconds": 600.0,
    }
    assert v1["previous_rules"] is None
    assert v1["reason"] is None
    assert v1["created_at"]


def test_rule_revision_history_keeps_before_after_reason_time(client):
    wid = make_window(client)["id"]
    assert revise(client, wid, rule_body(2)).status_code == 201
    assert revise(client, wid, rule_body(3, tmax=21.5, max_exceed=240.0,
                                         reason="再次收紧连续超限时长")).status_code == 201

    revs = client.get(f"/api/v1/windows/{wid}/rules/revisions").json()
    assert [r["revision"] for r in revs] == [1, 2, 3]
    v2, v3 = revs[1], revs[2]
    # v2：留存前后规则、原因与时间
    assert v2["rules"]["allowed_temp_max"] == 21.0
    assert v2["previous_rules"]["allowed_temp_max"] == 22.0
    assert v2["reason"] == "季度复核：收紧温度上限"
    assert v2["created_at"]
    # v3：前一版即 v2 的规则值
    assert v3["rules"]["allowed_temp_max"] == 21.5
    assert v3["rules"]["max_continuous_exceed_seconds"] == 240.0
    assert v3["previous_rules"] == v2["rules"]
    assert v3["reason"] == "再次收紧连续超限时长"


# ---------------------------------------------------------------- 生效与立即重算

def test_revise_success_recomputes_immediately(client):
    wid = make_window(client)["id"]
    fill_normal(client, wid, temps=21.5)  # 旧规则 [18,22] 下全部正常
    before = client.get(f"/api/v1/windows/{wid}/evaluation").json()
    assert before["verdict"] == "pass"

    # 收紧上限到 21.0：全部读数超限，连续 3600s > 300 -> fail
    r = revise(client, wid, rule_body(2, tmax=21.0))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "revised"
    rec = body["rule_revision"]
    assert rec["revision"] == 2
    assert rec["rules"]["allowed_temp_max"] == 21.0
    assert rec["previous_rules"]["allowed_temp_max"] == 22.0
    assert rec["reason"] == "季度复核：收紧温度上限"
    assert rec["created_at"]
    ev = body["current_evaluation"]
    assert ev["verdict"] == "fail"
    assert ev["max_continuous_exceeded_seconds"] == 3600.0

    # 窗口详情反映当前规则版本与新规则值
    detail = client.get(f"/api/v1/windows/{wid}").json()
    assert detail["rule_revision"] == 2
    assert detail["allowed_temp_max"] == 21.0
    assert detail["current_evaluation"]["verdict"] == "fail"


def test_window_times_and_readings_unchanged_after_revision(client):
    wid = make_window(client)["id"]
    fill_normal(client, wid)
    before_detail = client.get(f"/api/v1/windows/{wid}").json()
    before_readings = client.get(f"/api/v1/windows/{wid}/readings").json()

    assert revise(client, wid, rule_body(2)).status_code == 201

    after_detail = client.get(f"/api/v1/windows/{wid}").json()
    assert after_detail["start_at"] == before_detail["start_at"]
    assert after_detail["end_at"] == before_detail["end_at"]
    assert after_detail["created_at"] == before_detail["created_at"]
    assert client.get(f"/api/v1/windows/{wid}/readings").json() == before_readings


# ---------------------------------------------------------------- 幂等与冲突

def test_idempotent_replay_same_target_same_content(client):
    wid = make_window(client)["id"]
    body = rule_body(2)
    r1 = revise(client, wid, body)
    assert r1.status_code == 201 and r1.json()["effect"] == "revised"

    r2 = revise(client, wid, body)
    assert r2.status_code == 201, r2.text
    assert r2.json()["effect"] == "idempotent"
    assert r2.json()["rule_revision"] == r1.json()["rule_revision"]

    # 不留新记录：历史仍只有 v1 + v2
    revs = client.get(f"/api/v1/windows/{wid}/rules/revisions").json()
    assert [x["revision"] for x in revs] == [1, 2]
    assert client.get(f"/api/v1/windows/{wid}").json()["rule_revision"] == 2


def test_same_target_different_values_or_reason_conflict(client):
    wid = make_window(client)["id"]
    assert revise(client, wid, rule_body(2)).status_code == 201

    # 同号不同规则值 -> 409
    r = revise(client, wid, rule_body(2, tmax=20.5))
    assert r.status_code == 409 and "内容不一致" in r.json()["detail"]
    # 同号同规则值但原因不同 -> 409
    r = revise(client, wid, rule_body(2, reason="另一个原因"))
    assert r.status_code == 409
    # 当前版为初始 v1 的窗口：target=1 无法幂等（初始版无修订原因）-> 409
    wid2 = make_window(client)["id"]
    r = revise(client, wid2, rule_body(1, tmax=22.0, reason="任意原因"))
    assert r.status_code == 409

    # 均不留新记录
    revs = client.get(f"/api/v1/windows/{wid}/rules/revisions").json()
    assert [x["revision"] for x in revs] == [1, 2]
    assert client.get(f"/api/v1/windows/{wid}").json()["rule_revision"] == 2
    assert client.get(f"/api/v1/windows/{wid}").json()["allowed_temp_max"] == 21.0


def test_old_and_skip_target_rejected(client):
    wid = make_window(client)["id"]
    assert revise(client, wid, rule_body(2)).status_code == 201

    # 旧号（低于当前版）-> 409
    r = revise(client, wid, rule_body(1, tmax=22.0))
    assert r.status_code == 409 and "旧规则修订号" in r.json()["detail"]
    # 跳号（高于当前版+1）-> 409
    r = revise(client, wid, rule_body(4))
    assert r.status_code == 409 and "跳号" in r.json()["detail"]

    # 均不留新记录；随后正确目标号（当前版+1）仍可生效
    revs = client.get(f"/api/v1/windows/{wid}/rules/revisions").json()
    assert [x["revision"] for x in revs] == [1, 2]
    r = revise(client, wid, rule_body(3, tmax=21.5, reason="继续收紧"))
    assert r.status_code == 201 and r.json()["effect"] == "revised"
    assert [x["revision"] for x in
            client.get(f"/api/v1/windows/{wid}/rules/revisions").json()] == [1, 2, 3]


# ---------------------------------------------------------------- 非法输入与未找到

def test_invalid_rule_values_rejected(client):
    wid = make_window(client)["id"]
    # 温度下限 > 上限
    r = revise(client, wid, rule_body(2, tmin=23.0, tmax=22.0))
    assert r.status_code == 422
    # 两个时长必须为正
    assert revise(client, wid, rule_body(2, max_exceed=0)).status_code == 422
    assert revise(client, wid, rule_body(2, max_exceed=-5)).status_code == 422
    assert revise(client, wid, rule_body(2, max_gap=0)).status_code == 422
    assert revise(client, wid, rule_body(2, max_gap=-1)).status_code == 422
    # 数值须有限（NaN/Infinity 非标准 JSON 数值，用原始请求体发送）
    for bad in ("NaN", "Infinity", "-Infinity"):
        raw = ('{"target_revision":2,"allowed_temp_min":18.0,'
               f'"allowed_temp_max":{bad},"max_continuous_exceed_seconds":300,'
               '"max_sample_interval_seconds":600,"reason":"x"}')
        assert revise_raw(client, wid, raw).status_code == 422, bad
    raw = ('{"target_revision":2,"allowed_temp_min":18.0,"allowed_temp_max":22.0,'
           '"max_continuous_exceed_seconds":Infinity,'
           '"max_sample_interval_seconds":600,"reason":"x"}')
    assert revise_raw(client, wid, raw).status_code == 422
    # 原因为空或只含空白
    assert revise(client, wid, rule_body(2, reason="")).status_code == 422
    assert revise(client, wid, rule_body(2, reason="   ")).status_code == 422
    # 目标号必须 >= 1
    assert revise(client, wid, rule_body(0)).status_code == 422

    # 全部拒绝后规则保持初始第 1 版，历史只有 v1
    detail = client.get(f"/api/v1/windows/{wid}").json()
    assert detail["rule_revision"] == 1 and detail["allowed_temp_max"] == 22.0
    assert len(client.get(f"/api/v1/windows/{wid}/rules/revisions").json()) == 1


def test_revise_missing_window_404(client):
    assert revise(client, 999, rule_body(2)).status_code == 404
    assert client.get("/api/v1/windows/999/rules/revisions").status_code == 404


# ---------------------------------------------------------------- 签发与快照

def test_issue_after_revision_uses_new_rules_old_snapshot_immutable(client):
    wid = make_window(client)["id"]
    fill_normal(client, wid, temps=21.5)

    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue",
                     json={"note": "规则修订前"}).json()
    assert c1["verdict"] == "pass"
    assert c1["snapshot"]["window"]["rule_revision"] == 1
    assert c1["snapshot"]["window"]["allowed_temp_max"] == 22.0

    # 收紧上限 -> 当前审定变 fail；再签发采用新规则
    assert revise(client, wid, rule_body(2, tmax=21.0)).status_code == 201
    c2 = client.post(f"/api/v1/windows/{wid}/certificates/issue",
                     json={"note": "规则修订后"}).json()
    assert c2["version"] == 2 and c2["verdict"] == "fail"
    assert c2["snapshot"]["window"]["rule_revision"] == 2
    assert c2["snapshot"]["window"]["allowed_temp_max"] == 21.0

    # 既有证书快照不变
    old = client.get(f"/api/v1/certificates/{c1['id']}").json()
    assert old["verdict"] == "pass"
    assert old["snapshot"]["window"]["rule_revision"] == 1
    assert old["snapshot"]["window"]["allowed_temp_max"] == 22.0
    assert old["snapshot"]["evaluation"]["verdict"] == "pass"


# ---------------------------------------------------------------- 跨规则版本的修订影响报告

def test_cross_rule_version_report(client):
    wid = make_window(client)["id"]
    fill_normal(client, wid)  # 间隔 600s 恰好合法 -> pass
    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    assert c1["verdict"] == "pass"

    # 收紧最大采样间隔 600 -> 300：相邻读数间隔 600 > 300 -> 未知 -> pending
    r = revise(client, wid, rule_body(2, tmax=22.0, max_gap=300.0,
                                      reason="采样间隔要求收紧"))
    assert r.status_code == 201
    assert r.json()["current_evaluation"]["verdict"] == "pending"
    c2 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    assert c2["verdict"] == "pending"

    rep = client.get("/api/v1/certificates/revision-report"
                     f"?old_certificate_id={c1['id']}&new_certificate_id={c2['id']}").json()

    # 规则前后值与变化
    rules = rep["rules"]
    assert rules["before"] == {
        "rule_revision": 1, "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
        "max_continuous_exceed_seconds": 300.0, "max_sample_interval_seconds": 600.0,
    }
    assert rules["after"]["rule_revision"] == 2
    assert rules["after"]["max_sample_interval_seconds"] == 300.0
    assert rules["changed_fields"] == ["max_sample_interval_seconds"]
    assert rules["rules_changed"] is True

    # 时间线分别依据两张证书快照（各自规则）计算：正常 -> 未知
    changes = rep["timeline"]["changes"]
    assert len(changes) == 6
    assert all(c["before"]["status"] == "normal" for c in changes)
    assert all(c["after"]["status"] == "unknown" for c in changes)
    assert changes[0]["after"]["evidence"]["unknown_reason"] == "sample_gap"

    # 汇总：规则版本与变化可见
    s = rep["summary"]
    assert s["verdict_before"] == "pass" and s["verdict_after"] == "pending"
    assert s["rule_revision_before"] == 1 and s["rule_revision_after"] == 2
    assert s["rules_changed"] is True
    assert "审定规则" in s["conclusion_zh"] and "最大采样间隔" in s["conclusion_zh"]


def test_report_shows_rule_change_even_when_verdict_unchanged(client):
    wid = make_window(client)["id"]
    fill_normal(client, wid)  # 全部正常 -> pass
    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()

    # 只收紧最长连续超限时长（无超限读数，结论保持 pass）
    assert revise(client, wid, rule_body(2, tmax=22.0, max_exceed=240.0,
                                         reason="收紧连续超限时长")).status_code == 201
    c2 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    assert c2["verdict"] == "pass"

    rep = client.get("/api/v1/certificates/revision-report"
                     f"?old_certificate_id={c1['id']}&new_certificate_id={c2['id']}").json()
    s = rep["summary"]
    assert s["verdict_changed"] is False and s["verdict_direction"] == "持平"
    assert rep["timeline"]["changes"] == []
    # 结论未变也可看出规则变更
    assert rep["rules"]["rules_changed"] is True
    assert rep["rules"]["changed_fields"] == ["max_continuous_exceed_seconds"]
    assert rep["rules"]["before"]["max_continuous_exceed_seconds"] == 300.0
    assert rep["rules"]["after"]["max_continuous_exceed_seconds"] == 240.0
    assert s["rules_changed"] is True
    assert "最长连续超限时长 300s → 240s" in s["conclusion_zh"]


def test_report_same_rule_version_rules_unchanged(client):
    wid = make_window(client)["id"]
    fill_normal(client, wid, temps=21.5)
    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    # 不修订规则，仅修订读数后重签
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("r00", 2, 0, 20.0))
    c2 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()

    rep = client.get("/api/v1/certificates/revision-report"
                     f"?old_certificate_id={c1['id']}&new_certificate_id={c2['id']}").json()
    rules = rep["rules"]
    assert rules["rules_changed"] is False and rules["changed_fields"] == []
    assert rules["before"]["rule_revision"] == rules["after"]["rule_revision"] == 1
    assert rep["summary"]["rules_changed"] is False
    assert "审定规则保持一致" in rep["summary"]["conclusion_zh"]
    # 既有报告字段含义保留
    assert rep["readings_diff"]["changed_count"] == 1
    assert "max_sample_interval_seconds" in rep["timeline"]


# ---------------------------------------------------------------- 并发

def test_concurrent_rule_revisions_serialized(client):
    """两个线程同时提交同一目标号（内容不同）：BEGIN IMMEDIATE 串行化——
    一个生效（revised），另一个同号冲突（409）；不出现 500，最终规则版本为 2。"""
    wid = make_window(client)["id"]
    results = []

    def revise(tmax, barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post(f"/api/v1/windows/{wid}/rules/revisions",
                   json=rule_body(2, tmax=tmax))
        results.append((tmax, r.status_code, r.json().get("effect")))

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=revise, args=(21.0, barrier))
    t2 = threading.Thread(target=revise, args=(21.5, barrier))
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(code for _, code, _ in results)
    assert codes == [201, 409], results
    assert all(code < 500 for _, code, _ in results)
    detail = client.get(f"/api/v1/windows/{wid}").json()
    assert detail["rule_revision"] == 2
    revs = client.get(f"/api/v1/windows/{wid}/rules/revisions").json()
    assert [x["revision"] for x in revs] == [1, 2]
