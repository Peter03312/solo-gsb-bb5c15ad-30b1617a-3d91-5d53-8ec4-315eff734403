#!/usr/bin/env bash
# 博物馆展柜温度暴露审定服务 —— curl 调用示例
# 用法：BASE_URL=http://localhost:8000 bash examples/curl-examples.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
api() { # method path [json]
  local method="$1" path="$2" json="${3:-}"
  if [[ -n "$json" ]]; then
    curl -sS -X "$method" "$BASE_URL$path" -H 'Content-Type: application/json' -d "$json"
  else
    curl -sS -X "$method" "$BASE_URL$path"
  fi
  echo
}

echo "== 健康检查 =="
api GET /health

echo "== 1. 创建审定窗口（1 小时；允许 18~22°C；最长连续超限 300s；最大采样间隔 600s）=="
WINDOW=$(api POST /api/v1/windows '{
  "name": "展柜A-2026-10-01",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}')
echo "$WINDOW" | python3 -m json.tool
WID=$(echo "$WINDOW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "== 2. 提交读数（reading_id + revision）=="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":22.4}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-02","revision":1,"measured_at":"2026-10-01T00:20:00Z","temperature":23.1}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-03","revision":1,"measured_at":"2026-10-01T00:30:00Z","temperature":20.5}' >/dev/null

echo "== 3. 相同 ID + 修订号重复提交（幂等，内容一致）=="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1}'

echo "== 4. 相同 ID + 修订号但温度冲突 -> 409 =="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":99.9}' || true

echo "== 5. 旧修订号 -> 409；更高修订号替换重算 =="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":22.4}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":20.0}' || true
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":2,"measured_at":"2026-10-01T00:10:00Z","temperature":20.0}'

echo "== 6. 补齐整窗读数（间隔 <= 600s；末尾含窗口终点读数作为覆盖终止采样）=="
for min in 40 50 60; do
  if [[ "$min" -eq 60 ]]; then ts="2026-10-01T01:00:00Z"; else ts="2026-10-01T00:$min:00Z"; fi
  api POST "/api/v1/windows/$WID/readings" \
    "{\"reading_id\":\"r-$min\",\"revision\":1,\"measured_at\":\"$ts\",\"temperature\":20.0}" >/dev/null
done

echo "== 7. 查看当前审定（区段 + 最长连续超限 + 结论）=="
api GET "/api/v1/windows/$WID/evaluation" | python3 -m json.tool

echo "== 8. 首次签发（v1）=="
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"首次签发"}' | python3 -m json.tool

echo "== 9. 修订 r-02 为正常值后再次签发（v2；v1 快照不变）=="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-02","revision":2,"measured_at":"2026-10-01T00:20:00Z","temperature":20.2}' >/dev/null
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"修订后重新签发"}' | python3 -m json.tool

echo "== 10. 列出全部证书版本 =="
api GET "/api/v1/windows/$WID/certificates" | python3 -m json.tool

echo "== 11. 修订影响报告：比对 v1/v2 两张已签发证书的不可变快照 =="
# 从版本列表取两个证书主键 ID（响应只解析一次，避免 api 尾部换行拆词）
CIDS=$(api GET "/api/v1/windows/$WID/certificates" \
  | python3 -c "import sys,json;print(' '.join(str(c['id']) for c in json.load(sys.stdin)))")
read -r OLD_ID NEW_ID <<< "$CIDS"
api GET "/api/v1/certificates/revision-report?old_certificate_id=$OLD_ID&new_certificate_id=$NEW_ID" \
  | python3 -m json.tool

echo "== 12. 报告拒绝：版本倒序 / 跨窗口 / 同 ID（均 422），不存在 404 =="
api GET "/api/v1/certificates/revision-report?old_certificate_id=$NEW_ID&new_certificate_id=$OLD_ID" || true
api GET "/api/v1/certificates/revision-report?old_certificate_id=$OLD_ID&new_certificate_id=99999" || true

echo "== 13. 采样仪器补传：单独建一个窗口演示批量提交（整批事务）=="
BW=$(api POST /api/v1/windows '{
  "name": "展柜B-补传",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}')
BID=$(echo "$BW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "-- 13.1 一次批量创建多条读数（逐项返回 created，并按最终状态重算审定）--"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"a","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1},
    {"reading_id":"b","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":23.0},
    {"reading_id":"c","revision":1,"measured_at":"2026-10-01T00:20:00Z","temperature":20.2}
  ]
}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('effects:',[x['effect'] for x in d['results']]);print('verdict:',d['current_evaluation']['verdict'])"

echo "-- 13.2 同批重放：全部 idempotent，不产生新修订 --"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"a","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1},
    {"reading_id":"b","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":23.0},
    {"reading_id":"c","revision":1,"measured_at":"2026-10-01T00:20:00Z","temperature":20.2}
  ]
}' | python3 -c "import sys,json;print('effects:',[x['effect'] for x in json.load(sys.stdin)['results']])"

echo "-- 13.3 两条读数以更高修订号互换采样时刻（按最终状态判同刻，顺序无关，均 updated）--"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"a","revision":2,"measured_at":"2026-10-01T00:10:00Z","temperature":20.1},
    {"reading_id":"b","revision":2,"measured_at":"2026-10-01T00:00:00Z","temperature":20.0}
  ]
}' | python3 -c "import sys,json;print([(x['reading']['reading_id'],x['effect'],x['reading']['measured_at']) for x in json.load(sys.stdin)['results']])"

echo "-- 13.4 整批拒收：一项 409（b 用旧修订号 rev1）则整批不变（c 不会被创建）--"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"b","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.0},
    {"reading_id":"x","revision":1,"measured_at":"2026-10-01T00:30:00Z","temperature":20.0}
  ]
}' || true
echo "活动读数（应仍为 a/b/c 三条，无 x）："
api GET "/api/v1/windows/$BID/readings" \
  | python3 -c "import sys,json;print(sorted(r['reading_id'] for r in json.load(sys.stdin)))"

echo "-- 13.5 整批拒收：同批 reading_id 重复（422）；空批次也是 422 --"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"a","revision":3,"measured_at":"2026-10-01T00:30:00Z","temperature":20.0},
    {"reading_id":"a","revision":3,"measured_at":"2026-10-01T00:40:00Z","temperature":20.0}
  ]
}' || true
api POST "/api/v1/windows/$BID/readings/batch" '{"readings":[]}' || true

echo "== 14. 误采作废与恢复（窗口 $WID 上演示）=="
echo "-- 14.1 作废 r-03（当前 rev1）：更高修订号 rev2 + 非空原因；当前审定立即剔除并重算 --"
api POST "/api/v1/windows/$WID/readings/r-03/void" \
  '{"revision":2,"reason":"误采：该时刻传感器未校准"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('effect:',d['effect']);print('void:',json.dumps(d['void'],ensure_ascii=False));print('verdict:',d['current_evaluation']['verdict'],'reading_count:',d['current_evaluation']['reading_count'])"

echo "-- 14.2 相同作废修订号+相同原因重放 -> 幂等（不产生新修订）--"
api POST "/api/v1/windows/$WID/readings/r-03/void" \
  '{"revision":2,"reason":"误采：该时刻传感器未校准"}' \
  | python3 -c "import sys,json;print('effect:',json.load(sys.stdin)['effect'])"

echo "-- 14.3 拒绝路径：同修订号不同原因 409 / 低修订号 409 / 从未存在 404 / 已作废再以新修订号作废 409 / 空原因 422 --"
api POST "/api/v1/windows/$WID/readings/r-03/void" '{"revision":2,"reason":"另一个原因"}' || true
api POST "/api/v1/windows/$WID/readings/r-03/void" '{"revision":1,"reason":"x"}' || true
api POST "/api/v1/windows/$WID/readings/ghost/void" '{"revision":1,"reason":"x"}' || true
api POST "/api/v1/windows/$WID/readings/r-03/void" '{"revision":3,"reason":"再次作废"}' || true
api POST "/api/v1/windows/$WID/readings/r-03/void" '{"revision":3,"reason":"   "}' || true

echo "-- 14.4 修订历史：作废作为该 ID 的修订留存（原因/操作时间/前一版本）--"
api GET "/api/v1/windows/$WID/readings/r-03/revisions" | python3 -m json.tool

echo "-- 14.5 签发 v3 反映作废；修订影响报告 v2->v3 呈现 r-03 移除（removed）--"
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"作废 r-03 后签发"}' >/dev/null
CV=$(api GET "/api/v1/windows/$WID/certificates" \
  | python3 -c "import sys,json;cs=json.load(sys.stdin);print(cs[-2]['id'],cs[-1]['id'])")
read -r V2_ID V3_ID <<< "$CV"
api GET "/api/v1/certificates/revision-report?old_certificate_id=$V2_ID&new_certificate_id=$V3_ID" \
  | python3 -c "import sys,json;d=json.load(sys.stdin)['readings_diff'];print('removed:',[r['reading_id'] for r in d['removed']],'added:',[r['reading_id'] for r in d['added']])"

echo "-- 14.6 旧版提交不得复活：重放作废前 rev1 -> 409 --"
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-03","revision":1,"measured_at":"2026-10-01T00:30:00Z","temperature":20.5}' || true

echo "-- 14.7 恢复：提交更高修订号 rev3（effect=restored，立即重算）--"
api POST "/api/v1/windows/$WID/readings" \
  '{"reading_id":"r-03","revision":3,"measured_at":"2026-10-01T00:30:00Z","temperature":20.4}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('effect:',d['effect'],'rev:',d['reading']['revision'],'verdict:',d['current_evaluation']['verdict'])"

echo "-- 14.8 签发 v4 反映恢复；报告 v3->v4 呈现 r-03 再出现（added）；v3 快照保持原样 --"
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"恢复 r-03 后签发"}' >/dev/null
CV=$(api GET "/api/v1/windows/$WID/certificates" \
  | python3 -c "import sys,json;cs=json.load(sys.stdin);print(cs[-2]['id'],cs[-1]['id'])")
read -r V3_ID V4_ID <<< "$CV"
api GET "/api/v1/certificates/revision-report?old_certificate_id=$V3_ID&new_certificate_id=$V4_ID" \
  | python3 -c "import sys,json;d=json.load(sys.stdin)['readings_diff'];print('added:',[r['reading_id'] for r in d['added']],'removed:',[r['reading_id'] for r in d['removed']])"
api GET "/api/v1/certificates/$V3_ID" \
  | python3 -c "import sys,json;print('v3 快照读数（仍无 r-03）:',[r['reading_id'] for r in json.load(sys.stdin)['snapshot']['readings']])"

echo "-- 14.9 批量恢复：作废 r-40/r-50 后整批以更高修订号恢复（均 restored）--"
api POST "/api/v1/windows/$WID/readings/r-40/void" '{"revision":2,"reason":"误采"}' >/dev/null
api POST "/api/v1/windows/$WID/readings/r-50/void" '{"revision":2,"reason":"误采"}' >/dev/null
api POST "/api/v1/windows/$WID/readings/batch" '{
  "readings": [
    {"reading_id":"r-40","revision":3,"measured_at":"2026-10-01T00:40:00Z","temperature":20.0},
    {"reading_id":"r-50","revision":3,"measured_at":"2026-10-01T00:50:00Z","temperature":20.0}
  ]
}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('effects:',[x['effect'] for x in d['results']],'reading_count:',d['current_evaluation']['reading_count'])"

echo "== 15. 窗口审定规则修订（温度上下限/最长连续超限/最大采样间隔）=="
echo "-- 15.1 初始规则视为第 1 版；查看规则修订历史 --"
api GET "/api/v1/windows/$WID/rules/revisions" \
  | python3 -c "import sys,json;print([(r['revision'],r['rules']['allowed_temp_max'],r['reason']) for r in json.load(sys.stdin)])"

echo "-- 15.2 提交修订：目标号=当前版+1（2），四项完整新值+非空原因；立即按当前读数重算 --"
api POST "/api/v1/windows/$WID/rules/revisions" '{
  "target_revision": 2,
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 21.5,
  "max_continuous_exceed_seconds": 240,
  "max_sample_interval_seconds": 600,
  "reason": "季度复核：收紧温度上限与最长连续超限时长"
}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('effect:',d['effect']);print('revision:',d['rule_revision']['revision']);print('rules:',d['rule_revision']['rules']);print('previous:',d['rule_revision']['previous_rules']);print('verdict:',d['current_evaluation']['verdict'])"

echo "-- 15.3 相同目标号+相同内容重放 -> 幂等（不留新记录）--"
api POST "/api/v1/windows/$WID/rules/revisions" '{
  "target_revision": 2,
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 21.5,
  "max_continuous_exceed_seconds": 240,
  "max_sample_interval_seconds": 600,
  "reason": "季度复核：收紧温度上限与最长连续超限时长"
}' | python3 -c "import sys,json;print('effect:',json.load(sys.stdin)['effect'])"

echo "-- 15.4 拒绝路径：同号不同内容/原因 409；旧号 409；跳号 409；非法值/空原因 422；窗口不存在 404 --"
api POST "/api/v1/windows/$WID/rules/revisions" '{"target_revision":2,"allowed_temp_min":18.0,"allowed_temp_max":21.0,"max_continuous_exceed_seconds":240,"max_sample_interval_seconds":600,"reason":"季度复核：收紧温度上限与最长连续超限时长"}' || true
api POST "/api/v1/windows/$WID/rules/revisions" '{"target_revision":2,"allowed_temp_min":18.0,"allowed_temp_max":21.5,"max_continuous_exceed_seconds":240,"max_sample_interval_seconds":600,"reason":"另一个原因"}' || true
api POST "/api/v1/windows/$WID/rules/revisions" '{"target_revision":1,"allowed_temp_min":18.0,"allowed_temp_max":22.0,"max_continuous_exceed_seconds":300,"max_sample_interval_seconds":600,"reason":"x"}' || true
api POST "/api/v1/windows/$WID/rules/revisions" '{"target_revision":5,"allowed_temp_min":18.0,"allowed_temp_max":21.5,"max_continuous_exceed_seconds":240,"max_sample_interval_seconds":600,"reason":"x"}' || true
api POST "/api/v1/windows/$WID/rules/revisions" '{"target_revision":3,"allowed_temp_min":23.0,"allowed_temp_max":22.0,"max_continuous_exceed_seconds":240,"max_sample_interval_seconds":600,"reason":"x"}' || true
api POST "/api/v1/windows/$WID/rules/revisions" '{"target_revision":3,"allowed_temp_min":18.0,"allowed_temp_max":21.5,"max_continuous_exceed_seconds":240,"max_sample_interval_seconds":600,"reason":"   "}' || true
api POST "/api/v1/windows/99999/rules/revisions" '{"target_revision":2,"allowed_temp_min":18.0,"allowed_temp_max":21.5,"max_continuous_exceed_seconds":240,"max_sample_interval_seconds":600,"reason":"x"}' || true

echo "-- 15.5 规则修订历史：留存前后规则、原因与时间 --"
api GET "/api/v1/windows/$WID/rules/revisions" | python3 -m json.tool

echo "-- 15.6 签发采用新规则；跨规则版本报告显示四项规则前后值与变化（结论未变也可见）--"
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"规则修订后签发"}' >/dev/null
CV=$(api GET "/api/v1/windows/$WID/certificates" \
  | python3 -c "import sys,json;cs=json.load(sys.stdin);print(cs[-2]['id'],cs[-1]['id'])")
read -r VOLD_ID VNEW_ID <<< "$CV"
api GET "/api/v1/certificates/revision-report?old_certificate_id=$VOLD_ID&new_certificate_id=$VNEW_ID" \
  | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('rules:',json.dumps(d['rules'],ensure_ascii=False))
print('verdict:',d['summary']['verdict_before'],'->',d['summary']['verdict_after'])
print('conclusion:',d['summary']['conclusion_zh'])"

echo "== 16. 审定履历（按窗口、按时间追溯；序号 0 为建窗基线，此后连续编号）=="
echo "-- 16.1 全量读取（升序；每条含操作类型/涉及读数 ID 或规则版本/前后结论与最长连续超限/状态变化区段）--"
api GET "/api/v1/windows/$WID/audit-history" \
  | python3 -c "
import sys,json
for e in json.load(sys.stdin)['entries']:
    b=e['before']['verdict'] if e['before'] else '-'
    print(e['seq'], e['operation'], e['reading_ids'], e['rule_revisions'],
          b, '->', e['after']['verdict'],
          'max', e['after']['max_continuous_exceeded_seconds'],
          'changed', len(e['changed_segments']))"

echo "-- 16.2 从指定序号分页读取（start_seq 含；limit；next_start_seq 给出下一页起点）--"
api GET "/api/v1/windows/$WID/audit-history?start_seq=1&limit=3" | python3 -m json.tool

echo "-- 16.3 幂等重放与签发证书不产生履历：先记当前条数，再做幂等作废重放 + 签发 --"
SEQ_BEFORE=$(api GET "/api/v1/windows/$WID/audit-history" \
  | python3 -c "import sys,json;print(len(json.load(sys.stdin)['entries']))")
api POST "/api/v1/windows/$WID/rules/revisions" '{
  "target_revision": 2,
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 21.5,
  "max_continuous_exceed_seconds": 240,
  "max_sample_interval_seconds": 600,
  "reason": "季度复核：收紧温度上限与最长连续超限时长"
}' >/dev/null
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"履历演示签发（不留痕）"}' >/dev/null
SEQ_AFTER=$(api GET "/api/v1/windows/$WID/audit-history" \
  | python3 -c "import sys,json;print(len(json.load(sys.stdin)['entries']))")
echo "幂等规则重放 + 签发前后履历条数：$SEQ_BEFORE -> $SEQ_AFTER（应相同；失败请求同理不留痕）"

echo "-- 16.4 窗口不存在 -> 404；非法序号 -> 422 --"
api GET "/api/v1/windows/99999/audit-history" || true
api GET "/api/v1/windows/$WID/audit-history?start_seq=-1" || true
api GET "/api/v1/windows/$WID/audit-history?start_seq=abc" || true

echo "-- 16.5 新窗口基线：建窗即有序号 0（origin=window_created，空读数按当时规则重算 pending）--"
NW=$(api POST /api/v1/windows '{
  "name": "展柜G-履历基线",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}')
NID=$(echo "$NW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
api GET "/api/v1/windows/$NID/audit-history" | python3 -m json.tool
