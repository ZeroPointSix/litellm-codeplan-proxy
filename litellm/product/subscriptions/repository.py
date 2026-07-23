from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel

from litellm.product.subscriptions.models import SubscriptionRecord


class SubscriptionRepository:
    table_name = "litellm_codeplansubscriptiontable"
    raw_table_name = "LiteLLM_CodePlanSubscriptionTable"
    raw_update_columns = frozenset(
        field_name
        for field_name in SubscriptionRecord.model_fields
        if field_name not in {"subscription_id", "created_at"}
    )
    raw_column_casts = {
        "plan_snapshot": "::jsonb",
        "metadata": "::jsonb",
        "litellm_key_ids": "::text[]",
    }

    def __init__(self, prisma_client: Any):
        self._prisma_client = prisma_client

    @property
    def prisma_client(self) -> Any:
        if self._prisma_client is None:
            raise RuntimeError("No DB Connected. See - https://docs.litellm.ai/docs/proxy/virtual_keys")
        return self._prisma_client

    @property
    def table(self) -> Any:
        return getattr(self.prisma_client.db, self.table_name)

    @property
    def writer_db(self) -> Any:
        db = self.prisma_client.db
        return getattr(db, "writer", db)

    async def create(self, data: dict[str, Any]) -> SubscriptionRecord:
        record = await self.table.create(data=self._serialize(data))
        return self._to_model(record)

    async def get(self, subscription_id: str) -> Optional[SubscriptionRecord]:
        record = await self.table.find_unique(where={"subscription_id": subscription_id})
        if record is None:
            return None
        return self._to_model(record)

    async def list(
        self,
        status: Optional[str] = None,
        project_id: Optional[str] = None,
        plan_id: Optional[str] = None,
    ) -> list[SubscriptionRecord]:
        where = {}
        if status:
            where["status"] = status
        if project_id:
            where["project_id"] = project_id
        if plan_id:
            where["plan_id"] = plan_id
        records = await self.table.find_many(where=where, order={"created_at": "desc"})
        return [self._to_model(record) for record in records]

    async def update(self, subscription_id: str, data: dict[str, Any]) -> SubscriptionRecord:
        record = await self.table.update(where={"subscription_id": subscription_id}, data=self._serialize(data))
        return self._to_model(record)

    async def update_if_version(
        self,
        subscription_id: str,
        version: int,
        data: dict[str, Any],
    ) -> Optional[SubscriptionRecord]:
        record = await self._update_returning_if_version(subscription_id=subscription_id, version=version, data=data)
        if record is None:
            return None
        return self._to_model(record)

    async def _update_returning_if_version(
        self,
        subscription_id: str,
        version: int,
        data: dict[str, Any],
    ) -> Optional[Any]:
        serialized = self._serialize(data)
        columns = list(serialized)
        self._validate_update_columns(columns)

        query = self._build_update_returning_query(columns)
        arguments = [serialized[column] for column in columns]
        arguments.extend([subscription_id, version])
        rows = await self.writer_db.query_raw(query, *arguments)
        return self._first_row(rows)

    def _build_update_returning_query(self, columns: list[str]) -> str:
        set_clauses = []
        for index, column in enumerate(columns, start=1):
            cast = self.raw_column_casts.get(column, "")
            set_clauses.append(f'"{column}" = ${index}{cast}')
        if "updated_at" not in columns:
            set_clauses.append('"updated_at" = CURRENT_TIMESTAMP')

        subscription_id_index = len(columns) + 1
        version_index = len(columns) + 2
        return (
            f'UPDATE "{self.raw_table_name}" SET {", ".join(set_clauses)} '
            f'WHERE "subscription_id" = ${subscription_id_index} AND "version" = ${version_index} '
            "RETURNING *"
        )

    def _validate_update_columns(self, columns: list[str]) -> None:
        invalid_columns = [column for column in columns if column not in self.raw_update_columns]
        if invalid_columns:
            raise ValueError(
                f"Unsupported Code Plan Subscription update column(s): {', '.join(sorted(invalid_columns))}"
            )

    def _first_row(self, rows: Any) -> Optional[Any]:
        if rows is None:
            return None
        if isinstance(rows, (list, tuple)):
            return rows[0] if rows else None
        return rows

    def _to_model(self, record: Any) -> SubscriptionRecord:
        if isinstance(record, BaseModel):
            data = record.model_dump(exclude_none=False)
        elif isinstance(record, dict):
            data = dict(record)
        else:
            data = {key: getattr(record, key) for key in SubscriptionRecord.model_fields if hasattr(record, key)}

        self._load_json_field(data, "metadata", {})
        self._load_json_field(data, "plan_snapshot", {})
        if data.get("litellm_key_ids") is None:
            data["litellm_key_ids"] = []

        return SubscriptionRecord.model_validate(data)

    def _serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        serialized = dict(data)
        for field in ("metadata", "plan_snapshot"):
            value = serialized.get(field)
            if isinstance(value, BaseModel):
                serialized[field] = value.model_dump(mode="json")
                value = serialized[field]
            if isinstance(value, dict):
                serialized[field] = json.dumps(value)
        return serialized

    def _load_json_field(self, data: dict[str, Any], field: str, default: Any) -> None:
        value = data.get(field)
        if isinstance(value, str):
            data[field] = json.loads(value or json.dumps(default))
        elif value is None:
            data[field] = default
