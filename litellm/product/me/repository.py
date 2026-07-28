from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from litellm.product.me.models import PortalKeyRecord
from litellm.product.subscriptions.models import SubscriptionRecord


class PortalRepository:
    def __init__(self, prisma_client: Any):
        self.prisma_client = prisma_client
        self.db = prisma_client.db

    async def list_keys(self, subscription: SubscriptionRecord) -> list[PortalKeyRecord]:
        rows = await self.db.query_raw(
            """
            SELECT
                token AS key_id,
                key_alias,
                models,
                allowed_routes,
                blocked,
                expires AS expires_at,
                created_at,
                updated_at,
                last_active
            FROM "LiteLLM_VerificationToken"
            WHERE
                token = ANY($1::text[])
                OR (metadata::jsonb ->> 'code_plan_subscription_id') = $2
            ORDER BY created_at DESC
            """,
            subscription.litellm_key_ids,
            subscription.subscription_id,
        )
        return [self._key_record(row) for row in rows]

    async def usage_buckets(
        self,
        subscription: SubscriptionRecord,
        start_time: datetime,
        end_time: datetime,
        timezone_name: str,
    ) -> list[dict[str, object]]:
        scope_sql, scope_args = self._scope_sql(subscription, 4)
        rows = await self.db.query_raw(
            f"""
            SELECT
                to_char(date_trunc('day', "startTime" AT TIME ZONE $3), 'YYYY-MM-DD') AS date,
                COUNT(*)::int AS request_count,
                COALESCE(SUM(prompt_tokens), 0)::bigint AS input_tokens,
                COALESCE(SUM(completion_tokens), 0)::bigint AS output_tokens,
                COALESCE(SUM(total_tokens), 0)::bigint AS total_tokens
            FROM "LiteLLM_SpendLogs"
            WHERE
                "startTime" >= $1
                AND "startTime" < $2
                AND {scope_sql}
            GROUP BY 1
            ORDER BY 1 ASC
            """,
            start_time,
            end_time,
            timezone_name,
            *scope_args,
        )
        return [self._row_dict(row) for row in rows]

    async def request_count(
        self,
        subscription: SubscriptionRecord,
        start_time: datetime,
        end_time: datetime,
    ) -> int:
        scope_sql, scope_args = self._scope_sql(subscription, 3)
        rows = await self.db.query_raw(
            f"""
            SELECT COUNT(*)::int AS total
            FROM "LiteLLM_SpendLogs"
            WHERE
                "startTime" >= $1
                AND "startTime" < $2
                AND {scope_sql}
            """,
            start_time,
            end_time,
            *scope_args,
        )
        if not rows:
            return 0
        return self._int(self._row_dict(rows[0]).get("total"))

    async def request_rows(
        self,
        subscription: SubscriptionRecord,
        start_time: datetime,
        end_time: datetime,
        page: int,
        page_size: int,
    ) -> list[dict[str, object]]:
        scope_sql, scope_args = self._scope_sql(subscription, 3)
        limit_index = 3 + len(scope_args)
        offset_index = limit_index + 1
        offset = (page - 1) * page_size
        rows = await self.db.query_raw(
            f"""
            SELECT
                request_id,
                "startTime" AS start_time,
                "endTime" AS end_time,
                request_duration_ms,
                model,
                api_base,
                call_type,
                status,
                cache_hit,
                COALESCE(prompt_tokens, 0)::bigint AS input_tokens,
                COALESCE(completion_tokens, 0)::bigint AS output_tokens,
                COALESCE(total_tokens, 0)::bigint AS total_tokens
            FROM "LiteLLM_SpendLogs"
            WHERE
                "startTime" >= $1
                AND "startTime" < $2
                AND {scope_sql}
            ORDER BY "startTime" DESC
            LIMIT ${limit_index}
            OFFSET ${offset_index}
            """,
            start_time,
            end_time,
            *scope_args,
            page_size,
            offset,
        )
        return [self._row_dict(row) for row in rows]

    def _scope_sql(
        self,
        subscription: SubscriptionRecord,
        start_index: int,
    ) -> tuple[str, list[object]]:
        clauses: list[str] = []
        args: list[object] = []
        index = start_index

        if subscription.litellm_team_id:
            clauses.append(f'team_id = ${index}')
            args.append(subscription.litellm_team_id)
            index += 1

        if subscription.litellm_key_ids:
            clauses.append(f'api_key = ANY(${index}::text[])')
            args.append(subscription.litellm_key_ids)
            index += 1

        clauses.append(f"(metadata::jsonb ->> 'code_plan_subscription_id') = ${index}")
        args.append(subscription.subscription_id)
        index += 1

        clauses.append(f"(metadata::jsonb ->> 'code_plan_project_id') = ${index}")
        args.append(subscription.project_id)

        return "(" + " OR ".join(clauses) + ")", args

    def _key_record(self, row: object) -> PortalKeyRecord:
        data = self._row_dict(row)
        return PortalKeyRecord(
            key_id=str(data.get("key_id") or ""),
            key_alias=self._optional_str(data.get("key_alias")),
            models=self._string_list(data.get("models")),
            allowed_routes=self._string_list(data.get("allowed_routes")),
            blocked=bool(data.get("blocked") or False),
            expires_at=self._optional_datetime(data.get("expires_at")),
            created_at=self._optional_datetime(data.get("created_at")),
            updated_at=self._optional_datetime(data.get("updated_at")),
            last_active=self._optional_datetime(data.get("last_active")),
        )

    def _row_dict(self, row: object) -> dict[str, object]:
        if isinstance(row, dict):
            return dict(row)
        model_dump = getattr(row, "model_dump", None)
        if callable(model_dump):
            return model_dump()
        dict_method = getattr(row, "dict", None)
        if callable(dict_method):
            return dict_method()
        return {
            key: getattr(row, key)
            for key in dir(row)
            if not key.startswith("_") and not callable(getattr(row, key))
        }

    def _string_list(self, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        if isinstance(value, tuple):
            return [item for item in value if isinstance(item, str)]
        return []

    def _optional_str(self, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _optional_datetime(self, value: object) -> datetime | None:
        if isinstance(value, datetime):
            return value
        return None

    def _int(self, value: object) -> int:
        if value is None:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
