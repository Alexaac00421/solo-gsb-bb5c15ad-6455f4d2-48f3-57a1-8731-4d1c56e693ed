#!/usr/bin/env bash
# 指定知悉时刻状态查询示例：
# 1) 早于批次受理时间（即使业务 occurred_at 更早）看不到记录；
# 2) 补正只在 digest_corrections.created_at 后改变 current_voucher_digest；
# 3) 撤回后有效列表回退，但原链长度/链头仍保留撤回批次。
# 用法：BASE=http://localhost:8000 ./examples/state-at.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
JQ="$(command -v jq || true)"
SUFFIX="$(date +%s)"
C_ID="EX-PT-CORR-${SUFFIX}"
W_ID="EX-PT-WD-${SUFFIX}"

D1="sha256:$(printf 'ab%.0s' {1..32})"
D2="sha256:$(printf 'cd%.0s' {1..32})"

pp() { if [ -n "${JQ}" ]; then jq .; else cat; echo; fi; }
json_field() {
  local field="$1"
  if [ -n "${JQ}" ]; then jq -r --arg f "${field}" '.[$f]'; else
    grep -oE "\"${field}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -1 \
      | sed -E "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*/\1/"; fi
}
create_exhibit() {
  local id="$1"
  curl -s -X POST "${BASE}/api/v1/exhibits" \
    -H 'Content-Type: application/json' \
    -d "{\"exhibit_id\":\"${id}\",\"name\":\"时点查询示例\",\"home_location\":\"库房A\",\"custodian\":\"张伟\",\"initial_seal\":\"SEAL-1\"}"
}
submit_one() {
  local id="$1" from="$2" to="$3" digest="$4" when="$5" key="$6"
  curl -s -X POST "${BASE}/api/v1/handover-batches" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: ${key}" \
    -d "{\"exhibit_id\":\"${id}\",\"records\":[{\"location\":\"库房A\",\"from_person\":\"${from}\",\"to_person\":\"${to}\",\"seal_no\":\"SEAL-1\",\"occurred_at\":\"${when}\",\"voucher_digest\":\"${digest}\"}]}"
}
state_at() {
  curl -s --get "${BASE}/api/v1/exhibits/$1/state-at" --data-urlencode "as_of=$2"
}

echo "== 登记两件示例展品 =="
C_REGISTERED_AT="$(create_exhibit "${C_ID}" | json_field created_at)"
W_REGISTERED_AT="$(create_exhibit "${W_ID}" | json_field created_at)"
echo "补正示例：${C_ID}，登记时刻 ${C_REGISTERED_AT}"
echo "撤回示例：${W_ID}，登记时刻 ${W_REGISTERED_AT}"

echo; echo "== 登记时刻：空链返回登记保管人和初始封签 =="
state_at "${C_ID}" "${C_REGISTERED_AT}" | pp

echo; echo "== 提交一笔业务时间很早、但服务端此刻才受理的交接 =="
submit_one "${C_ID}" "张伟" "李静" "${D1}" "2026-09-01T09:00:00+08:00" "state-at-c-${SUFFIX}" | pp
submit_one "${W_ID}" "张伟" "李静" "${D1}" "2026-09-01T09:00:00+08:00" "state-at-w-${SUFFIX}" >/dev/null

echo; echo "== 仍用登记时刻查询：历史交接尚未受理，不得提前出现 =="
state_at "${C_ID}" "${C_REGISTERED_AT}" | pp

echo; echo "== 当前时刻查询：交接已可见，业务时间显示为 occurred_at，受理边界显示为 accepted_at =="
NOW="$(date --iso-8601=seconds)"
state_at "${C_ID}" "${NOW}" | pp

echo; echo "== 对第 1 环提交摘要补正，再查当前时点：原 voucher_digest 不变，current_voucher_digest 更新 =="
curl -s -X POST "${BASE}/api/v1/exhibits/${C_ID}/handovers/1/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: state-at-corr-${SUFFIX}" \
  -d "{\"expected_digest\":\"${D1}\",\"new_digest\":\"${D2}\",\"reason\":\"时点查询示例补正\"}" | pp
state_at "${C_ID}" "$(date --iso-8601=seconds)" | pp

echo; echo "== 撤回另一展品唯一批次：有效列表为空并回到登记状态，原链长度和哈希仍为 1 =="
BATCH_ID="$(curl -s "${BASE}/api/v1/exhibits/${W_ID}/handovers" | json_field batch_id)"
curl -s -X POST "${BASE}/api/v1/exhibits/${W_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: state-at-wd-${SUFFIX}" \
  -d "{\"batch_id\":\"${BATCH_ID}\",\"reason\":\"时点查询示例撤回\"}" | pp
state_at "${W_ID}" "$(date --iso-8601=seconds)" | pp

echo; echo "== 参数错误：无时区 / 未来时刻均为 422 =="
curl -s -o /dev/null -w '无时区 as_of -> HTTP %{http_code}\n' \
  --get "${BASE}/api/v1/exhibits/${C_ID}/state-at" --data-urlencode 'as_of=2026-09-01T09:00:00'
curl -s -o /dev/null -w '未来 as_of -> HTTP %{http_code}\n' \
  --get "${BASE}/api/v1/exhibits/${C_ID}/state-at" --data-urlencode 'as_of=2099-01-01T00:00:00+00:00'
