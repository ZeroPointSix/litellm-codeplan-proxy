from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import BaseModel

from litellm.product.usage_ledger.models import (
    UsageLedgerAggregateRecord,
    UsageLedgerCreate,
    UsageLedgerGroupBy,
    UsageLedgerRecord,
)


class UsageLedgerRepository:
    table_name = "litellm_codeplanusageledgertable"
    raw_table_name = "LiteLLM_CodePlanUsageLedgerTable"
    subscription_raw_table_name = "LiteLLM_CodePlanSubscriptionTable"

    def __init__(self, prisma_client: object):
        self._prisma_client = prisma_client

    @property
    def prisma_client(self) -> object:
        if self._prisma_client is None:
            raise RuntimeError("No DB Connected. See - https://docs.litellm.ai/docs/proxy/virtual_keys")
        return self._prisma_client

    @property
    def table(self) -> object:
        return getattr(self.prisma_client.db, self.table_name)

    @property
    def writer_db(self) -> object:
        db = self.prisma_client.db
        return getattr(db, "writer", db)

    async def create(self, data: UsageLedgerCreate) -> UsageLedgerRecord:
        record = await self.table.create(data=self._serialize(data.model_dump(mode="json")))
        return self._to_model(record)

    async def list(
        self,
        *,
        request_id: str | None = None,
        subscription_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        model: str | None = None,
        event_type: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[UsageLedgerRecord]:
        where = self._where(
            request_id=request_id,
            subscription_id=subscription_id,
            project_id=project_id,
            user_id=user_id,
            model=model,
            event_type=event_type,
            start_time=start_time,
            end_time=end_time,
        )
        records = await self.table.find_many(
            where=where,
            order={"created_at": "desc"},
            take=limit,
            skip=max(offset, 0),
        )
        return [self._to_model(record) for record in records]

    async def aggregate(
        self,
        *,
        group_by: UsageLedgerGroupBy,
        request_id: str | None = None,
        subscription_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        model: str | None = None,
        event_type: str | list[str] | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[UsageLedgerAggregateRecord]:
        group_expr = self._group_expression(group_by)
        where_sql, arguments = self._where_sql(
            request_id=request_id,
            subscription_id=subscription_id,
            project_id=project_id,
            user_id=user_id,
            model=model,
            event_type=event_type,
            start_time=start_time,
            end_time=end_time,
        )
        limit_index = len(arguments) + 1
        query = f'''
            SELECT
                {group_expr} AS "group",
                COALESCE(SUM("credits"), 0)::double precision AS "credits",
                COALESCE(SUM("input_tokens"), 0)::integer AS "input_tokens",
                COALESCE(SUM("output_tokens"), 0)::integer AS "output_tokens",
                COALESCE(SUM("cache_read_tokens"), 0)::integer AS "cache_read_tokens",
                COALESCE(SUM("cache_write_tokens"), 0)::integer AS "cache_write_tokens",
                COUNT(DISTINCT "request_id")::integer AS "request_count",
                COUNT(*)::integer AS "event_count"
            FROM "{self.raw_table_name}"
            {where_sql}
            GROUP BY 1
            ORDER BY 1 DESC
            LIMIT ${limit_index}
        '''
        rows = await self.writer_db.query_raw(query, *arguments, limit)
        return [UsageLedgerAggregateRecord.model_validate(dict(row)) for row in rows]

    async def subscription_quota_metadata(self, subscription_id: str) -> dict[str, object]:
        query = f"""
            SELECT
                "subscription_id",
                "project_id",
                "metadata",
                "plan_snapshot",
                "renewed_at",
                "created_at",
                "window_5h_start"
            FROM "{self.subscription_raw_table_name}"
            WHERE "subscription_id" = $1
            LIMIT 1
        """
        rows = await self.writer_db.query_raw(query, subscription_id)
        if not rows:
            raise ValueError("subscription not found")
        row = dict(rows[0])
        self._load_json_field(row, "metadata", {})
        self._load_json_field(row, "plan_snapshot", {})
        plan_snapshot = row.get("plan_snapshot")
        if not isinstance(plan_snapshot, dict):
            raise ValueError("subscription plan snapshot is required")
        metadata = dict(row["metadata"]) if isinstance(row.get("metadata"), dict) else {}
        metadata["code_plan_subscription_id"] = str(row["subscription_id"])
        metadata["code_plan_project_id"] = str(row["project_id"])
        metadata["code_plan_quota_5h"] = float(plan_snapshot["quota_5h"])
        metadata["code_plan_quota_weekly"] = float(plan_snapshot["quota_weekly"])
        plan_metadata = plan_snapshot.get("metadata")
        if isinstance(plan_metadata, dict) and plan_metadata.get("quota_monthly") is not None:
            metadata["code_plan_quota_monthly"] = float(plan_metadata["quota_monthly"])
        week_anchor = row.get("renewed_at") or row.get("created_at")
        if isinstance(week_anchor, datetime):
            metadata["code_plan_week_anchor_epoch"] = self._epoch(week_anchor)
        window_5h_start = row.get("window_5h_start")
        if isinstance(window_5h_start, datetime):
            metadata["code_plan_5h_anchor_epoch"] = self._epoch(window_5h_start)
        return metadata

    async def update_subscription_windows(
        self,
        *,
        subscription_id: str,
        window_5h_start: datetime | None,
        window_week_start: datetime | None,
    ) -> None:
        columns: list[tuple[str, datetime | None]] = []
        if window_5h_start is not None:
            columns.append(("window_5h_start", window_5h_start))
        if window_week_start is not None:
            columns.append(("window_week_start", window_week_start))
        if not columns:
            return
        set_clauses = [
            f'"{column}" = ${index}::timestamp(3)' for index, (column, _value) in enumerate(columns, start=1)
        ]
        subscription_id_index = len(columns) + 1
        query = (
            f'UPDATE "{self.subscription_raw_table_name}" SET {", ".join(set_clauses)}, '
            f'"updated_at" = CURRENT_TIMESTAMP WHERE "subscription_id" = ${subscription_id_index}'
        )
        arguments = [value for _column, value in columns]
        await self.writer_db.query_raw(query, *arguments, subscription_id)

    def _where(
        self,
        *,
        request_id: str | None,
        subscription_id: str | None,
        project_id: str | None,
        user_id: str | None,
        model: str | None,
        event_type: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> dict[str, object]:
        where: dict[str, object] = {}
        for field_name, value in (
            ("request_id", request_id),
            ("subscription_id", subscription_id),
            ("project_id", project_id),
            ("user_id", user_id),
            ("model", model),
            ("event_type", event_type),
        ):
            if value:
                where[field_name] = value
        if start_time is not None or end_time is not None:
            created_at: dict[str, datetime] = {}
            if start_time is not None:
                created_at["gte"] = start_time
            if end_time is not None:
                created_at["lte"] = end_time
            where["created_at"] = created_at
        return where

    def _where_sql(self, **filters: object) -> tuple[str, list[object]]:
        clauses: list[str] = []
        arguments: list[object] = []
        column_map = {
            "request_id": "request_id",
            "subscription_id": "subscription_id",
            "project_id": "project_id",
            "user_id": "user_id",
            "model": "model",
            "event_type": "event_type",
        }
        for filter_name, column_name in column_map.items():
            value = filters.get(filter_name)
            if isinstance(value, list):
                if value:
                    placeholders = []
                    for item in value:
                        arguments.append(item)
                        placeholders.append(f"${len(arguments)}")
                    clauses.append(f'"{column_name}" IN ({", ".join(placeholders)})')
            elif value:
                arguments.append(value)
                clauses.append(f'"{column_name}" = ${len(arguments)}')
        if filters.get("start_time") is not None:
            arguments.append(filters["start_time"])
            clauses.append(f'"created_at" >= ${len(arguments)}::timestamp(3)')
        if filters.get("end_time") is not None:
            arguments.append(filters["end_time"])
            clauses.append(f'"created_at" <= ${len(arguments)}::timestamp(3)')
        return ("WHERE " + " AND ".join(clauses), arguments) if clauses else ("", arguments)

    def _group_expression(self, group_by: UsageLedgerGroupBy) -> str:
        if group_by == UsageLedgerGroupBy.HOUR:
            return "DATE_TRUNC('hour', \"created_at\")::text"
        if group_by == UsageLedgerGroupBy.DAY:
            return "DATE_TRUNC('day', \"created_at\")::text"
        if group_by == UsageLedgerGroupBy.MODEL:
            return "COALESCE(\"model\", '')"
        return "COALESCE(\"user_id\", '')"

    def _epoch(self, value: datetime) -> int:
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(aware.timestamp())

    def _to_model(self, record: object) -> UsageLedgerRecord:
        if isinstance(record, BaseModel):
            data = record.model_dump(exclude_none=False)
        elif isinstance(record, dict):
            data = dict(record)
        else:
            data = {key: getattr(record, key) for key in UsageLedgerRecord.model_fields if hasattr(record, key)}
        self._load_json_field(data, "metadata", {})
        return UsageLedgerRecord.model_validate(data)

    def _serialize(self, data: dict[str, object]) -> dict[str, object]:
        serialized = dict(data)
        if isinstance(serialized.get("metadata"), dict):
            serialized["metadata"] = json.dumps(serialized["metadata"])
        return serialized

    def _load_json_field(self, data: dict[str, object], field: str, default: object) -> None:
        value = data.get(field)
        if isinstance(value, str):
            data[field] = json.loads(value or json.dumps(default))
        elif value is None:
            data[field] = default
