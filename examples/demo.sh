#!/usr/bin/env bash
# 端到端调用示例：登记展品 -> 预演 -> 凭令牌提交 -> 幂等重试 -> 冲突检测 ->
# 提交违规批次（不落记录）-> 令牌失效与重新预演 -> 查询交接链 -> 哈希链审计校验 ->
# 凭证摘要补正（拒绝/冲突/幂等/独立补正链）-> 双链审计 ->
# 已受理批次撤回（拒绝/幂等/独立撤回链/有效状态回退）-> 三链审计 ->
# 车次整车交接（整单确认/拒绝/幂等/冲突/与单件接口共存）。
# 用法：BASE=http://localhost:8000 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; echo; fi; }
# 从响应中抽取状态令牌（有 jq 用 jq，无 jq 退化为 sed）
extract_token() {
  if [ -n "$JQ" ]; then jq -r '.state_token.token'; else
    sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\(stv1\.[^"]*\)".*/\1/p' | head -1; fi
}

EXHIBIT_ID="EX-DEMO-$(date +%s)"
KEY_OK="demo-ok-${EXHIBIT_ID}"
KEY_BAD="demo-bad-${EXHIBIT_ID}"
KEY_RH="demo-rh-${EXHIBIT_ID}"

# 补正用摘要（均为合法 sha256:<64hex>）
D_ORIG="sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"
D_NEW1="sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f00918273645564738291a0b1c2d3e4f50"
D_NEW2="sha256:5566778899aabbccddeeff00112233445566778899aabbccddeeff0011223344"
KEY_C1="demo-corr1-${EXHIBIT_ID}"
KEY_C2="demo-corr2-${EXHIBIT_ID}"

echo "== 0) 健康检查 =="
curl -s "${BASE}/health" | pp

echo; echo "== 1) 登记展品 ${EXHIBIT_ID}（馆藏地/保管人/初始封签为首站校验基准）=="
curl -s -X POST "${BASE}/api/v1/exhibits" \
  -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"name\":\"宋代青瓷莲花尊\",\"home_location\":\"南京博物院库房B区\",\"custodian\":\"张伟\",\"initial_seal\":\"SEAL-NJ-0001\"}" | pp

echo; echo "== 2) 预演合规批次（只读核验：不落报告、不落记录、不推进链；返回状态令牌）=="
TOKEN="$(curl -s -X POST "${BASE}/api/v1/handover-batches/rehearse" \
  -H 'Content-Type: application/json' \
  -d @- <<JSON | extract_token
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
     "seal_no":"SEAL-NJ-0001","occurred_at":"2026-09-01T09:00:00+08:00",
     "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"},
    {"location":"上海博物馆交接区","from_person":"李静","to_person":"王强",
     "seal_no":"SEAL-SH-1002","seal_change_reason":"原封签运输途中磨损，双方馆长书面授权换签（批文号 SH-2026-091）",
     "occurred_at":"2026-09-03T14:30:00+08:00",
     "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"},
    {"location":"上海博物馆临时展厅","from_person":"王强","to_person":"陈晨",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-04T10:00:00+08:00",
     "voucher_digest":"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"}
  ]
}
JSON
)"
[ -n "${TOKEN}" ] || { echo "未取到预演令牌，终止"; exit 1; }
echo "已取得状态令牌：${TOKEN:0:40}..."

echo; echo "== 2b) 预演未知展品 -> 404 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches/rehearse" \
  -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"EX-NOT-EXIST","records":[{"location":"x","from_person":"a","to_person":"b","seal_no":"S","occurred_at":"2026-09-01T09:00:00+08:00","voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"}]}'

echo; echo "== 3) 携带 Handover-Token 正式提交合规批次（链状态未变，正常受理）=="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -H "Handover-Token: ${TOKEN}" \
  -d @- <<JSON | pp
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
     "seal_no":"SEAL-NJ-0001","occurred_at":"2026-09-01T09:00:00+08:00",
     "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"},
    {"location":"上海博物馆交接区","from_person":"李静","to_person":"王强",
     "seal_no":"SEAL-SH-1002","seal_change_reason":"原封签运输途中磨损，双方馆长书面授权换签（批文号 SH-2026-091）",
     "occurred_at":"2026-09-03T14:30:00+08:00",
     "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"},
    {"location":"上海博物馆临时展厅","from_person":"王强","to_person":"陈晨",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-04T10:00:00+08:00",
     "voucher_digest":"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"}
  ]
}
JSON

echo; echo "== 4) 同键同载荷（含令牌）重试：链已推进但仍返回原报告，idempotent_replay=true =="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -H "Handover-Token: ${TOKEN}" \
  -d @- <<JSON | pp
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
     "seal_no":"SEAL-NJ-0001","occurred_at":"2026-09-01T09:00:00+08:00",
     "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"},
    {"location":"上海博物馆交接区","from_person":"李静","to_person":"王强",
     "seal_no":"SEAL-SH-1002","seal_change_reason":"原封签运输途中磨损，双方馆长书面授权换签（批文号 SH-2026-091）",
     "occurred_at":"2026-09-03T14:30:00+08:00",
     "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"},
    {"location":"上海博物馆临时展厅","from_person":"王强","to_person":"陈晨",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-04T10:00:00+08:00",
     "voucher_digest":"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"}
  ]
}
JSON

echo; echo "== 5) 过期令牌再提交（链已推进）-> 409，需重新预演 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_RH}" \
  -H "Handover-Token: ${TOKEN}" \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"西安博物院交接区","from_person":"陈晨","to_person":"赵磊","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-05T09:00:00+08:00","voucher_digest":"sha256:aa11bb22cc33dd44ee55ff660011223344556677889900aabbccddeeff00112233"}]}'

echo; echo "== 6) 令牌格式无效 -> 400 参数错误 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_RH}" \
  -H 'Handover-Token: not-a-valid-token' \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"西安博物院交接区","from_person":"陈晨","to_person":"赵磊","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-05T09:00:00+08:00","voucher_digest":"sha256:aa11bb22cc33dd44ee55ff660011223344556677889900aabbccddeeff00112233"}]}'

echo; echo "== 7) 同幂等键但载荷不同 -> 409（令牌计入载荷身份）=="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"records\":[{\"location\":\"x\",\"from_person\":\"a\",\"to_person\":\"b\",\"seal_no\":\"S\",\"occurred_at\":\"2026-09-05T09:00:00+08:00\",\"voucher_digest\":\"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9\"}]}"

echo; echo "== 8) 提交违规批次（不带令牌，人员断裂+时间倒退+无授权换签+凭证非法）-> REJECTED =="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_BAD}" \
  -d @- <<JSON | pp
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"上海博物馆临时展厅","from_person":"陈晨","to_person":"赵磊",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-05T09:00:00+08:00",
     "voucher_digest":"sha256:aa11bb22cc33dd44ee55ff660011223344556677889900aabbccddeeff00112233"},
    {"location":"西安博物院交接区","from_person":"孙倩","to_person":"周明",
     "seal_no":"SEAL-XA-2001","occurred_at":"2026-09-05T08:30:00+08:00",
     "voucher_digest":"not-a-valid-digest"}
  ]
}
JSON

echo; echo "== 9) 查询交接链（应有 3 环：409/400/REJECTED 均未写入任何记录）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers" | pp

echo; echo "== 10) 哈希链审计校验 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/audit/verify" | pp

echo; echo "== 11) 批次历史（含被驳回批次，供审计追溯；409/400 不产生报告）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batches" | pp

echo; echo "== 12) 补正第 3 环凭证摘要（原摘要 -> 新摘要）：201，锚定原记录 record_hash =="
curl -s -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_C1}" \
  -d "{\"expected_digest\":\"${D_ORIG}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"凭证摘要录入笔误，据馆方复核原件更正（复核单 CR-2026-1001）\"}" | pp

echo; echo "== 13) 同键同载荷重试：返回原补正单（200, idempotent_replay=true）=="
curl -s -D /tmp/corr13.hdr -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_C1}" \
  -d "{\"expected_digest\":\"${D_ORIG}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"凭证摘要录入笔误，据馆方复核原件更正（复核单 CR-2026-1001）\"}" | pp
grep -i '^HTTP/' /tmp/corr13.hdr

echo; echo "== 14) 旧预期值再补 -> 409（当前有效摘要已是 ${D_NEW1:0:18}...）=="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-corr-stale-${EXHIBIT_ID}" \
  -d "{\"expected_digest\":\"${D_ORIG}\",\"new_digest\":\"${D_NEW2}\",\"reason\":\"再次补正\"}"

echo; echo "== 15) 以当前有效摘要为预期继续补正：201（补正链第 2 环，prev_hash 接第 1 环）=="
curl -s -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_C2}" \
  -d "{\"expected_digest\":\"${D_NEW1}\",\"new_digest\":\"${D_NEW2}\",\"reason\":\"二次复核再更正（复核单 CR-2026-1002）\"}" | pp

echo; echo "== 16) 拒绝用例：目标不存在(404) / 原因空缺(422) / 新旧相同(422) / 同键异载荷(409) =="
curl -s -o /dev/null -w '目标链序号 999 不存在        -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/999/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-corr-miss-${EXHIBIT_ID}" \
  -d "{\"expected_digest\":\"${D_NEW2}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"x\"}"
curl -s -o /dev/null -w '原因空缺                     -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-corr-empty-${EXHIBIT_ID}" \
  -d "{\"expected_digest\":\"${D_NEW2}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"   \"}"
curl -s -o /dev/null -w '新旧摘要相同                 -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-corr-same-${EXHIBIT_ID}" \
  -d "{\"expected_digest\":\"${D_NEW2}\",\"new_digest\":\"${D_NEW2}\",\"reason\":\"x\"}"
curl -s -o /dev/null -w '同键异载荷                   -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: ${KEY_C1}" \
  -d "{\"expected_digest\":\"${D_NEW2}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"换一个载荷\"}"

echo; echo "== 17) 交接链查询：原摘要 voucher_digest 不变，current_voucher_digest 为最新，补正轨迹两环 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers" | pp

echo; echo "== 18) 单环节查询（原摘要 / 当前有效摘要 / 补正轨迹）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3" | pp

echo; echo "== 19) 补正链列表（可按链序号过滤）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/digest-corrections" | pp

echo; echo "== 20) 双链审计复算（交接链 + 补正链；补正不推进交接链，预演令牌不受影响）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/audit/verify" | pp

echo; echo "== 21) 幂等优先于现状：即便当前有效摘要已是 ${D_NEW2:0:14}... ，用 ${KEY_C1} 原载荷仍重放原单 =="
curl -s -D /tmp/corr21.hdr -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_C1}" \
  -d "{\"expected_digest\":\"${D_ORIG}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"凭证摘要录入笔误，据馆方复核原件更正（复核单 CR-2026-1001）\"}" | pp
grep -i '^HTTP/' /tmp/corr21.hdr

# 从 JSON 响应抽取字段（有 jq 用 jq，无 jq 退化为 grep/sed）
json_field() {
  local field="$1"
  if [ -n "${JQ}" ]; then jq -r --arg f "${field}" '.[$f]'; else
    grep -oE "\"${field}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -1 \
      | sed -E "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*/\1/"; fi
}

KEY_W1="demo-wd1-${EXHIBIT_ID}"
KEY_W2="demo-wd2-${EXHIBIT_ID}"

echo; echo "== 22) 再提交一笔已受理批次（陈晨 -> 周敏），作为待撤回的最新批次 =="
BATCH_WD="$(curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-wd-batch-${EXHIBIT_ID}" \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"西安博物院库房","from_person":"陈晨","to_person":"周敏","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-06T09:00:00+08:00","voucher_digest":"sha256:3344556677889900112233445566778899001122334455667788990011223344"}]}' \
  | json_field batch_id)"
echo "待撤回批次：${BATCH_WD}"
[ -n "${BATCH_WD}" ] || { echo "未取到批次号，终止"; exit 1; }

echo; echo "== 23) 撤回该批次：201，返回撤回后的有效状态（保管人回退为陈晨，原链历史长度不变）=="
curl -s -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_W1}" \
  -d "{\"batch_id\":\"${BATCH_WD}\",\"reason\":\"交接批次误登记，撤回后重新核对凭证再提交（复核单 WD-2026-1001）\"}" | pp

echo; echo "== 24) 同键同载荷重试：返回首次撤回结果（200, idempotent_replay=true）=="
curl -s -D /tmp/wd24.hdr -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_W1}" \
  -d "{\"batch_id\":\"${BATCH_WD}\",\"reason\":\"交接批次误登记，撤回后重新核对凭证再提交（复核单 WD-2026-1001）\"}" | pp
grep -i '^HTTP/' /tmp/wd24.hdr

echo; echo "== 25) 展品当前状态：保管人/封签按撤回后计算，chain_length（原链历史长度）仍为 4，有效链长 3 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}" | pp

echo; echo "== 26) 撤回拒绝用例：更晚批次(409) / 补正引用(409) / 批次被驳回(409) / 原因空(422) / 同键异载荷(409) =="
# 26a) 被补正引用：第 3 环属于最初的 3 记录批次（已被两笔补正引用），先重新提交一笔再制造更晚批次
BATCH_FRESH="$(curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-wd-fresh-${EXHIBIT_ID}" \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"西安博物院库房","from_person":"陈晨","to_person":"吴迪","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-07T09:00:00+08:00","voucher_digest":"sha256:4455667788990011223344556677889900aabbccddeeff001122334455667788"}]}' \
  | json_field batch_id)"
# 取最初 3 记录批次号（第 3 环所在批次）
BATCH_CORR="$(curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3" | json_field batch_id)"
curl -s -o /dev/null -w '存在更晚已受理批次时撤旧批次 -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-wd-older-${EXHIBIT_ID}" \
  -d "{\"batch_id\":\"${BATCH_CORR}\",\"reason\":\"试图撤回旧批次\"}"
# 26b) 目标批次被补正引用：先撤回最新批次，再撤被补正引用的旧批次
curl -s -o /dev/null -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: ${KEY_W2}" \
  -d "{\"batch_id\":\"${BATCH_FRESH}\",\"reason\":\"为演示补正引用拦截先撤回最新批次\"}"
curl -s -o /dev/null -w '批次已被补正单引用           -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-wd-corr-${EXHIBIT_ID}" \
  -d "{\"batch_id\":\"${BATCH_CORR}\",\"reason\":\"该批次已有补正，应被拒绝\"}"
# 26c) 批次被驳回
REJECTED_BATCH="$(curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-wd-rejb-${EXHIBIT_ID}" \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"x","from_person":"陌生人","to_person":"b","seal_no":"S","occurred_at":"2026-09-08T09:00:00+08:00","voucher_digest":"bad"}]}' \
  | json_field batch_id)"
curl -s -o /dev/null -w '撤回被驳回批次               -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-wd-rej-${EXHIBIT_ID}" \
  -d "{\"batch_id\":\"${REJECTED_BATCH}\",\"reason\":\"x\"}"
# 26d) 原因为空
curl -s -o /dev/null -w '撤回原因为空                 -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-wd-empty-${EXHIBIT_ID}" \
  -d "{\"batch_id\":\"${BATCH_CORR}\",\"reason\":\"   \"}"
# 26e) 目标不存在 / 批次号格式非法
curl -s -o /dev/null -w '撤回不存在批次               -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-wd-miss-${EXHIBIT_ID}" \
  -d '{"batch_id":"9f9c646e-0000-0000-0000-000000000000","reason":"x"}'
curl -s -o /dev/null -w '批次号格式非法               -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-wd-badid-${EXHIBIT_ID}" \
  -d '{"batch_id":"not-a-uuid","reason":"x"}'
# 26f) 同键异载荷
curl -s -o /dev/null -w '同键异载荷                   -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: ${KEY_W1}" \
  -d "{\"batch_id\":\"${BATCH_WD}\",\"reason\":\"换一个撤回原因\"}"

echo; echo "== 27) 交接链查询：被撤回批次记录仍完整保留（withdrawn=true），原链历史长度 4、有效链长 3 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers" | pp
echo "--- 仅有效交接记录（?effective_only=true）---"
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers?effective_only=true" | pp

echo; echo "== 28) 撤回链列表（撤回时序，可按批次过滤）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" | pp

echo; echo "== 29) 撤回后继续提交：业务校验以撤回后有效状态（陈晨持有）为基准，原链仍物理追加 =="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-wd-cont-${EXHIBIT_ID}" \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"南京博物院回程交接区","from_person":"陈晨","to_person":"周敏","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-09T09:00:00+08:00","voucher_digest":"sha256:556677889900aabbccddeeff0011223344556677889900aabbccddeeff00112233"}]}' | pp

echo; echo "== 30) 三链审计复算（交接链 + 补正链 + 撤回链；原链历史不删除，有效状态按撤回后计算）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/audit/verify" | pp

echo; echo "== 31) 批次历史：已撤回批次带 withdrawn 标记与撤回引用 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batches" | pp

# ---------------------------------------------------------------- 车次整车交接
V1="EX-V1-$(date +%s)"
V2="EX-V2-$(date +%s)"
KEY_VEH="demo-veh-${V1}"
KEY_VEH_BAD="demo-veh-bad-${V1}"

echo; echo "== 32) 登记两件展品 ${V1} / ${V2}，准备同一车次整车交接 =="
curl -s -X POST "${BASE}/api/v1/exhibits" -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${V1}\",\"name\":\"明代青花缠枝莲纹瓶\",\"home_location\":\"南京博物院库房B区\",\"custodian\":\"张伟\",\"initial_seal\":\"SEAL-NJ-1001\"}" | pp
curl -s -X POST "${BASE}/api/v1/exhibits" -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${V2}\",\"name\":\"清代掐丝珐琅香炉\",\"home_location\":\"南京博物院库房B区\",\"custodian\":\"王芳\",\"initial_seal\":\"SEAL-NJ-1002\"}" | pp

echo; echo "== 33) 车次整车交接：同一车次两件展品一次确认（全数通过 -> 同一事务各件入链）=="
curl -s -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_VEH}" \
  -d @- <<JSON | pp
{
  "vehicle_id": "TRAIN-2026-1001",
  "items": [
    {"exhibit_id": "${V1}", "records": [
      {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
       "seal_no":"SEAL-NJ-1001","occurred_at":"2026-09-10T09:00:00+08:00",
       "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"}
    ]},
    {"exhibit_id": "${V2}", "records": [
      {"location":"南京博物院库房B区","from_person":"王芳","to_person":"赵磊",
       "seal_no":"SEAL-NJ-1002","occurred_at":"2026-09-10T09:30:00+08:00",
       "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"}
    ]}
  ]
}
JSON

echo; echo "== 34) 同键同载荷重试：重放整单原报告（idempotent_replay=true，不重复入链）=="
curl -s -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_VEH}" \
  -d @- <<JSON | pp
{
  "vehicle_id": "TRAIN-2026-1001",
  "items": [
    {"exhibit_id": "${V1}", "records": [
      {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
       "seal_no":"SEAL-NJ-1001","occurred_at":"2026-09-10T09:00:00+08:00",
       "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"}
    ]},
    {"exhibit_id": "${V2}", "records": [
      {"location":"南京博物院库房B区","from_person":"王芳","to_person":"赵磊",
       "seal_no":"SEAL-NJ-1002","occurred_at":"2026-09-10T09:30:00+08:00",
       "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"}
    ]}
  ]
}
JSON

echo; echo "== 35) 同幂等键但载荷不同 -> 409 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_VEH}" \
  -d "{\"vehicle_id\":\"TRAIN-2026-1001\",\"items\":[{\"exhibit_id\":\"${V1}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"张伟\",\"to_person\":\"李静\",\"seal_no\":\"SEAL-NJ-1001\",\"occurred_at\":\"2026-09-10T09:00:00+08:00\",\"voucher_digest\":\"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9\"}]},{\"exhibit_id\":\"${V2}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"王芳\",\"to_person\":\"钱进\",\"seal_no\":\"SEAL-NJ-1002\",\"occurred_at\":\"2026-09-10T09:30:00+08:00\",\"voucher_digest\":\"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809\"}]}]}"

echo; echo "== 36) 任一件核验失败 -> 整单 REJECTED：保存整单拒绝报告，两件都不入链 =="
curl -s -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_VEH_BAD}" \
  -d @- <<JSON | pp
{
  "vehicle_id": "TRAIN-2026-1002",
  "items": [
    {"exhibit_id": "${V1}", "records": [
      {"location":"上海博物馆交接区","from_person":"李静","to_person":"王强",
       "seal_no":"SEAL-NJ-1001","occurred_at":"2026-09-11T09:00:00+08:00",
       "voucher_digest":"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"}
    ]},
    {"exhibit_id": "${V2}", "records": [
      {"location":"上海博物馆交接区","from_person":"孙倩","to_person":"周明",
       "seal_no":"SEAL-XA-2001","occurred_at":"2026-09-11T09:30:00+08:00",
       "voucher_digest":"not-a-valid-digest"}
    ]}
  ]
}
JSON

echo; echo "== 37) 拒绝后两件展品链状态均未推进（整单要么全入链、要么全不入）=="
curl -s "${BASE}/api/v1/exhibits/${V1}" | pp
curl -s "${BASE}/api/v1/exhibits/${V2}" | pp

echo; echo "== 38) 整车单含不存在展品 -> 404，整单不写入 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-veh-miss-${V1}" \
  -d "{\"vehicle_id\":\"TRAIN-2026-1003\",\"items\":[{\"exhibit_id\":\"${V1}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"李静\",\"to_person\":\"王强\",\"seal_no\":\"SEAL-NJ-1001\",\"occurred_at\":\"2026-09-12T09:00:00+08:00\",\"voucher_digest\":\"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506\"}]},{\"exhibit_id\":\"EX-NOT-EXIST\",\"records\":[{\"location\":\"x\",\"from_person\":\"a\",\"to_person\":\"b\",\"seal_no\":\"S\",\"occurred_at\":\"2026-09-12T09:30:00+08:00\",\"voucher_digest\":\"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809\"}]}]}"

echo; echo "== 39) 格式不合法 -> 422（展品重复 / 件数越界），整单不写入 =="
curl -s -o /dev/null -w '同一车次展品重复   -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-veh-dup-${V1}" \
  -d "{\"vehicle_id\":\"TRAIN-2026-1004\",\"items\":[{\"exhibit_id\":\"${V1}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"李静\",\"to_person\":\"王强\",\"seal_no\":\"SEAL-NJ-1001\",\"occurred_at\":\"2026-09-12T09:00:00+08:00\",\"voucher_digest\":\"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506\"}]},{\"exhibit_id\":\"${V1}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"李静\",\"to_person\":\"王强\",\"seal_no\":\"SEAL-NJ-1001\",\"occurred_at\":\"2026-09-12T10:00:00+08:00\",\"voucher_digest\":\"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506\"}]}]}"
curl -s -o /dev/null -w '仅 1 件（不足 2 件）-> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-veh-one-${V1}" \
  -d "{\"vehicle_id\":\"TRAIN-2026-1005\",\"items\":[{\"exhibit_id\":\"${V1}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"李静\",\"to_person\":\"王强\",\"seal_no\":\"SEAL-NJ-1001\",\"occurred_at\":\"2026-09-12T09:00:00+08:00\",\"voucher_digest\":\"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506\"}]}]}"

echo; echo "== 40) 整车入链后单件接口继续提交（既有接口完全兼容），并做三链审计 =="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-veh-cont-${V1}" \
  -d "{\"exhibit_id\":\"${V1}\",\"records\":[{\"location\":\"上海博物馆交接区\",\"from_person\":\"李静\",\"to_person\":\"王强\",\"seal_no\":\"SEAL-NJ-1001\",\"occurred_at\":\"2026-09-11T09:00:00+08:00\",\"voucher_digest\":\"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506\"}]}" | pp
curl -s "${BASE}/api/v1/exhibits/${V1}/audit/verify" | pp
curl -s "${BASE}/api/v1/exhibits/${V2}/audit/verify" | pp

# ---------------------------------------------------------------- 知悉时刻历史快照
# 独立演示展品：用服务端受理时间作为知悉时刻边界（业务 occurred_at 只用于呈现）。
AID="EX-ASOF-$(date +%s)"
echo; echo "== 41) 登记知悉时刻演示展品 ${AID} =="
REG_JSON="$(curl -s -X POST "${BASE}/api/v1/exhibits" -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${AID}\",\"name\":\"时点快照演示展品\",\"home_location\":\"库房A\",\"custodian\":\"张伟\",\"initial_seal\":\"SEAL-1\"}")"
echo "${REG_JSON}" | pp
REG_AT="$(echo "${REG_JSON}" | json_field created_at)"
echo "登记时间：${REG_AT}"

# 一个稍后用于「早于登记 -> 404」的时刻（2020 年）
echo; echo "== 42) 知悉时刻早于登记 -> 404；未知展品 -> 404 =="
curl -s -o /dev/null -w '早于登记      -> HTTP %{http_code}\n' \
  "${BASE}/api/v1/exhibits/${AID}/state-at?at=2020-01-01T00:00:00Z"
curl -s -o /dev/null -w '未知展品      -> HTTP %{http_code}\n' \
  "${BASE}/api/v1/exhibits/EX-NOT-EXIST/state-at?at=2020-01-01T00:00:00Z"

echo; echo "== 43) 参数校验：缺 at / 无时区 / 非法时间 / 未来时刻 -> 422 =="
curl -s -o /dev/null -w '缺少 at       -> HTTP %{http_code}\n' "${BASE}/api/v1/exhibits/${AID}/state-at"
curl -s -o /dev/null -w '无时区        -> HTTP %{http_code}\n' "${BASE}/api/v1/exhibits/${AID}/state-at?at=2026-09-10T09:00:00"
curl -s -o /dev/null -w '非法时间      -> HTTP %{http_code}\n' "${BASE}/api/v1/exhibits/${AID}/state-at?at=not-a-time"
FUTURE="$(date -u -d '+2 days' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+2d +%Y-%m-%dT%H:%M:%SZ)"
curl -s -o /dev/null -w '未来时刻      -> HTTP %{http_code}\n' "${BASE}/api/v1/exhibits/${AID}/state-at?at=${FUTURE}"

# 业务时间写在 2026-09-01，但批次此刻才受理；先停 2 秒，使「受理前 1 秒」晚于登记时刻
sleep 2
echo; echo "== 44) 提交批次（业务发生时间 2026-09-01，服务端此刻受理）=="
B1="$(curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-asof-b1-${AID}" \
  -d '{"exhibit_id":"'"${AID}"'","records":[{"location":"库房A","from_person":"张伟","to_person":"李静","seal_no":"SEAL-1","occurred_at":"2026-09-01T09:00:00+08:00","voucher_digest":"sha256:ababababababababababababababababababababababababababababababab"}]}' \
  | json_field batch_id)"
B1_AT="$(curl -s "${BASE}/api/v1/handover-batches/${B1}" | json_field created_at)"
echo "批次 ${B1} 受理时间：${B1_AT}（业务时间虽在 9 月 1 日，但此刻才进入可见链）"

# 由服务端受理时间派生「受理前 1 秒」的知悉时刻（证明业务时间不提前让记录出现）
B1_BEFORE="$(python3 - "${B1_AT}" <<'PY'
import sys, urllib.parse
from datetime import datetime, timedelta, timezone
t = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")) - timedelta(seconds=1)
print(urllib.parse.quote(t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"))
PY
)"
echo; echo "== 45) 受理前 1 秒查询：历史交接虽已发生但尚未受理，空链回退登记状态 =="
curl -s "${BASE}/api/v1/exhibits/${AID}/state-at?at=${B1_BEFORE}" | pp

REG_AT_ENC="$(python3 - "${REG_AT}" <<'PY'
import sys, urllib.parse
print(urllib.parse.quote(sys.argv[1]))
PY
)"
echo; echo "== 46) 以登记时刻查询：空链，保管人=张伟、封签=SEAL-1、最近交接时间=null =="
curl -s "${BASE}/api/v1/exhibits/${AID}/state-at?at=${REG_AT_ENC}" | pp

# 第二、三环（第二环随后撤回），再对第一环补正，演示「有效记录 + 当时有效摘要 + 撤回排除」
curl -s -o /dev/null -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-asof-b2-${AID}" \
  -d '{"exhibit_id":"'"${AID}"'","records":[{"location":"展厅B","from_person":"李静","to_person":"王强","seal_no":"SEAL-1","occurred_at":"2026-09-02T09:00:00+08:00","voucher_digest":"sha256:cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"}]}'
curl -s -o /dev/null -X POST "${BASE}/api/v1/exhibits/${AID}/handovers/1/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-asof-c1-${AID}" \
  -d '{"expected_digest":"sha256:ababababababababababababababababababababababababababababababab","new_digest":"sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f00918273645564738291a0b1c2d3e4f50","reason":"凭证摘要录入笔误更正"}'
# 撤回最新批次（第 2 环）
B2="$(curl -s "${BASE}/api/v1/exhibits/${AID}/handovers/2" | json_field batch_id)"
curl -s -o /dev/null -X POST "${BASE}/api/v1/exhibits/${AID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-asof-w1-${AID}" \
  -d "{\"batch_id\":\"${B2}\",\"reason\":\"第二环批次误登记，撤回\"}"

echo; echo "== 47) 当前知悉时刻快照：原链长度/链头仍含被撤回的第 2 环；有效列表仅第 1 环（带补正后摘要）；保管人回退为李静 =="
NOW_ENC="$(python3 - <<'PY'
import urllib.parse
from datetime import datetime, timezone
print(urllib.parse.quote(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"))
PY
)"
curl -s "${BASE}/api/v1/exhibits/${AID}/state-at?at=${NOW_ENC}" | pp


