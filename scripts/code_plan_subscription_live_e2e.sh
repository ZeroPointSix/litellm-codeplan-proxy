#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:4010}"
ADMIN_BASE_URL="${ADMIN_BASE_URL:-$BASE_URL}"
GATEWAY_BASE_URL="${GATEWAY_BASE_URL:-$BASE_URL}"
MASTER_KEY="${MASTER_KEY:-sk-code-plan-local-test}"
AUTH_HEADER="Authorization: Bearer ${MASTER_KEY}"
SUFFIX="$(date +%s)"

request() {
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" "${ADMIN_BASE_URL}${path}" \
      -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
      -d "$body"
  else
    curl -sS -X "$method" "${ADMIN_BASE_URL}${path}" -H "$AUTH_HEADER"
  fi
}


request_with_token() {
  local token="$1" method="$2" path="$3" body="${4:-}"
  if [[ -n "$body" ]]; then
    curl -sS -w "\n%{http_code}" -X "$method" "${GATEWAY_BASE_URL}${path}" \
      -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
      -d "$body"
  else
    curl -sS -w "\n%{http_code}" -X "$method" "${GATEWAY_BASE_URL}${path}" \
      -H "Authorization: Bearer ${token}"
  fi
}

assert_http_status() {
  local response="$1" expected="$2" label="$3"
  local status="${response##*$'\n'}"
  local body="${response%$'\n'*}"
  if [[ "$status" != "$expected" ]]; then
    echo "${label}: expected HTTP ${expected}, got ${status}" >&2
    echo "$body" >&2
    exit 1
  fi
  printf '%s' "$body"
}

echo "== health =="
request GET /health/liveliness
if [[ "$GATEWAY_BASE_URL" != "$ADMIN_BASE_URL" ]]; then
  GATEWAY_HEALTH=$(request_with_token "$MASTER_KEY" GET /health/liveliness)
  assert_http_status "$GATEWAY_HEALTH" 200 "gateway health" >/dev/null
fi
echo

echo "== create credit rule =="
RULE_JSON=$(request POST /v1/admin/credit-rules "{
  \"name\": \"local-rule-${SUFFIX}\",
  \"input_multiplier\": 1,
  \"output_multiplier\": 1,
  \"cache_read_multiplier\": 0.25,
  \"cache_write_multiplier\": 0.25
}")
RULE_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["credit_rule_id"])' <<<"$RULE_JSON")
request POST "/v1/admin/credit-rules/${RULE_ID}/activate?version=1" >/dev/null
echo "credit_rule_id=${RULE_ID}"

echo "== create + activate plan =="
PLAN_JSON=$(request POST /v1/admin/plans "{
  \"name\": \"Local Plan ${SUFFIX}\",
  \"quota_5h\": 100,
  \"quota_weekly\": 1000,
  \"allowed_models\": [\"gpt-4o-mini\"],
  \"rpm_limit\": 1,
  \"tpm_limit\": 120000,
  \"max_parallel_requests\": 2,
  \"max_keys\": 2,
  \"credit_rule_id\": \"${RULE_ID}\"
}")
PLAN_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["plan_id"])' <<<"$PLAN_JSON")
request POST "/v1/admin/plans/${PLAN_ID}/activate?version=1" >/dev/null
echo "plan_id=${PLAN_ID}"

echo "== create subscription skeleton (issue_key=false) =="
SKELETON_JSON=$(request POST /v1/admin/subscriptions "{
  \"project_id\": \"project-${SUFFIX}\",
  \"plan_id\": \"${PLAN_ID}\",
  \"issue_key\": false
}")
SUB_ID=$(python3 -c 'import json,sys; d=json.load(sys.stdin); s=d["subscription"]; assert s["litellm_team_id"] is None; assert s["litellm_key_ids"]==[]; print(s["subscription_id"])' <<<"$SKELETON_JSON")
echo "subscription_id=${SUB_ID}"

echo "== create subscription with team/key =="
LIVE_JSON=$(request POST /v1/admin/subscriptions "{
  \"project_id\": \"project-live-${SUFFIX}\",
  \"plan_id\": \"${PLAN_ID}\",
  \"issue_key\": true,
  \"key_alias\": \"primary-${SUFFIX}\"
}")
python3 -c 'import json,sys; d=json.load(sys.stdin); s=d["subscription"]; assert d.get("key"); assert s["litellm_team_id"]; assert s["litellm_key_ids"]' <<<"$LIVE_JSON"
LIVE_META=$(python3 -c 'import json,sys; d=json.load(sys.stdin); s=d["subscription"]; print(s["subscription_id"], s["version"], s["litellm_key_ids"][0], d["key"], sep="|")' <<<"$LIVE_JSON")
IFS='|' read -r LIVE_SUB_ID LIVE_VERSION LIVE_KEY_ID LIVE_KEY <<<"$LIVE_META"

echo "== subscription key can call chat completions =="
CHAT_RESPONSE=$(request_with_token "$LIVE_KEY" POST /v1/chat/completions "{
  \"model\": \"gpt-4o-mini\",
  \"messages\": [{\"role\": \"user\", \"content\": \"Return one short word.\"}],
  \"max_tokens\": 8
}")
CHAT_JSON=$(assert_http_status "$CHAT_RESPONSE" 200 "subscription chat completion")
CHAT_REQUEST_ID=$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("id") or "")' <<<"$CHAT_JSON")
test -n "$CHAT_REQUEST_ID"
echo "chat request_id present"

echo "== disallowed model returns 403 =="
DENIED_RESPONSE=$(request_with_token "$LIVE_KEY" POST /v1/chat/completions "{
  \"model\": \"gpt-4o\",
  \"messages\": [{\"role\": \"user\", \"content\": \"hello\"}],
  \"max_tokens\": 8
}")
assert_http_status "$DENIED_RESPONSE" 403 "disallowed model" >/dev/null

echo "== over limit returns 429 with Retry-After =="
OVERLIMIT_HEADERS=$(mktemp)
OVERLIMIT_BODY=$(mktemp)
OVERLIMIT_STATUS=$(curl -sS -D "$OVERLIMIT_HEADERS" -o "$OVERLIMIT_BODY" -w '%{http_code}' -X POST "${GATEWAY_BASE_URL}/v1/chat/completions" \
  -H "Authorization: Bearer ${LIVE_KEY}" -H 'Content-Type: application/json' \
  -d "{\"model\": \"gpt-4o-mini\", \"messages\": [{\"role\": \"user\", \"content\": \"hello\"}], \"max_tokens\": 8}")
if [[ "$OVERLIMIT_STATUS" != "429" ]]; then
  echo "over limit: expected HTTP 429, got ${OVERLIMIT_STATUS}" >&2
  cat "$OVERLIMIT_BODY" >&2
  exit 1
fi
if ! grep -qi '^retry-after:' "$OVERLIMIT_HEADERS"; then
  echo "over limit: missing Retry-After header" >&2
  cat "$OVERLIMIT_HEADERS" >&2
  exit 1
fi
rm -f "$OVERLIMIT_HEADERS" "$OVERLIMIT_BODY"

echo "== spend logs include request_id =="
for _ in {1..20}; do
  SPEND_JSON=$(request GET "/spend/logs?api_key=${LIVE_KEY}")
  if REQUEST_ID="$CHAT_REQUEST_ID" python3 -c 'import json,os,sys; data=json.load(sys.stdin); rows=data.get("data", data) if isinstance(data, dict) else data; target=os.environ["REQUEST_ID"]; sys.exit(0 if any(((row.get("request_id") or row.get("requestId")) == target or (row.get("request_id") or row.get("requestId"))) for row in rows if isinstance(row, dict)) else 1)' <<<"$SPEND_JSON"; then
    echo "spend log request_id present"
    break
  fi
  sleep 1
done
REQUEST_ID="$CHAT_REQUEST_ID" python3 -c 'import json,sys; data=json.load(sys.stdin); rows=data.get("data", data) if isinstance(data, dict) else data; assert any((row.get("request_id") or row.get("requestId")) for row in rows if isinstance(row, dict)), "SpendLogs missing request_id"' <<<"$SPEND_JSON"

echo "== issue second key =="
ISSUE_JSON=$(request POST "/v1/admin/subscriptions/${LIVE_SUB_ID}/keys" "{
  \"version\": ${LIVE_VERSION},
  \"key_alias\": \"secondary-${SUFFIX}\"
}")
LIVE_VERSION=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["subscription"]["version"])' <<<"$ISSUE_JSON")
python3 -c 'import json,sys; s=json.load(sys.stdin)["subscription"]; assert len(s["litellm_key_ids"])==2' <<<"$ISSUE_JSON"
echo "second key issued, version=${LIVE_VERSION}"

echo "== revoke first key =="
REVOKE_JSON=$(request POST "/v1/admin/subscriptions/${LIVE_SUB_ID}/keys/revoke" "{
  \"version\": ${LIVE_VERSION},
  \"key_id\": \"${LIVE_KEY_ID}\"
}")
LIVE_VERSION=$(python3 -c 'import json,sys; s=json.load(sys.stdin); assert len(s["litellm_key_ids"])==1; print(s["version"])' <<<"$REVOKE_JSON")
echo "revoked key, version=${LIVE_VERSION}"
REVOKED_RESPONSE=$(request_with_token "$LIVE_KEY" GET /v1/models)
assert_http_status "$REVOKED_RESPONSE" 401 "revoked key" >/dev/null

echo "== pause subscription =="
PAUSE_JSON=$(request POST "/v1/admin/subscriptions/${LIVE_SUB_ID}/pause" "{\"version\": ${LIVE_VERSION}}")
LIVE_VERSION=$(python3 -c 'import json,sys; s=json.load(sys.stdin); assert s["status"]=="paused"; assert s["litellm_key_ids"]==[]; print(s["version"])' <<<"$PAUSE_JSON")
echo "paused, version=${LIVE_VERSION}"

echo "== renew subscription =="
RENEW_JSON=$(request POST "/v1/admin/subscriptions/${LIVE_SUB_ID}/renew" "{
  \"version\": ${LIVE_VERSION},
  \"issue_key\": true,
  \"key_alias\": \"renewed-${SUFFIX}\"
}")
python3 -c 'import json,sys; s=json.load(sys.stdin)["subscription"]; assert s["status"]=="active"; assert s["litellm_key_ids"]; print(s["version"])' <<<"$RENEW_JSON"
LIVE_VERSION=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["subscription"]["version"])' <<<"$RENEW_JSON")
echo "renewed, version=${LIVE_VERSION}"

echo "== create upgraded plan =="
UP_PLAN_JSON=$(request POST /v1/admin/plans "{
  \"name\": \"Local Pro ${SUFFIX}\",
  \"quota_5h\": 200,
  \"quota_weekly\": 2000,
  \"allowed_models\": [\"gpt-4o-mini\"],
  \"max_keys\": 3,
  \"credit_rule_id\": \"${RULE_ID}\"
}")
UP_PLAN_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["plan_id"])' <<<"$UP_PLAN_JSON")
request POST "/v1/admin/plans/${UP_PLAN_ID}/activate?version=1" >/dev/null

echo "== upgrade subscription =="
UPGRADE_JSON=$(request POST "/v1/admin/subscriptions/${LIVE_SUB_ID}/upgrade" "{
  \"version\": ${LIVE_VERSION},
  \"plan_id\": \"${UP_PLAN_ID}\",
  \"issue_key\": true
}")
python3 -c 'import json,sys; s=json.load(sys.stdin)["subscription"]; assert s["plan_id"]=="'"${UP_PLAN_ID}"'"; assert s["plan_snapshot"]["quota_5h"]==200; print(s["subscription_id"], s["version"], sep="|")' <<<"$UPGRADE_JSON"
UP_META=$(python3 -c 'import json,sys; s=json.load(sys.stdin)["subscription"]; print(s["subscription_id"], s["version"], sep="|")' <<<"$UPGRADE_JSON")
IFS='|' read -r UP_SUB_ID UP_VERSION <<<"$UP_META"

echo "== cancel upgraded subscription =="
CANCEL_JSON=$(request POST "/v1/admin/subscriptions/${UP_SUB_ID}/cancel" "{\"version\": ${UP_VERSION}}")
python3 -c 'import json,sys; s=json.load(sys.stdin); assert s["status"]=="canceled"; assert s["litellm_key_ids"]==[]' <<<"$CANCEL_JSON"

echo "== list subscriptions =="
LIST_JSON=$(request GET "/v1/admin/subscriptions?project_id=project-live-${SUFFIX}")
python3 -c 'import json,sys; data=json.load(sys.stdin)["data"]; print(f"listed {len(data)} canceled/historical rows for project-live")' <<<"$LIST_JSON"

echo
echo "ALL LIVE E2E CHECKS PASSED"
