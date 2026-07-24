#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:4010}"
MASTER_KEY="${MASTER_KEY:-sk-code-plan-local-test}"
AUTH_HEADER="Authorization: Bearer ${MASTER_KEY}"
SUFFIX="$(date +%s)"

request() {
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" "${BASE_URL}${path}" \
      -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
      -d "$body"
  else
    curl -sS -X "$method" "${BASE_URL}${path}" -H "$AUTH_HEADER"
  fi
}

echo "== health =="
request GET /health/liveliness
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
LIVE_META=$(python3 -c 'import json,sys; d=json.load(sys.stdin); s=d["subscription"]; print(s["subscription_id"], s["version"], s["litellm_key_ids"][0], sep="|")' <<<"$LIVE_JSON")
IFS='|' read -r LIVE_SUB_ID LIVE_VERSION LIVE_KEY_ID <<<"$LIVE_META"

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
