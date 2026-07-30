from __future__ import annotations

import json
from datetime import datetime

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
        records = await self.table.find_many(where=where, order={"created_at": "desc"}, take=limit)
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
