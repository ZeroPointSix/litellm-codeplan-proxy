from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel

from litellm.product.plans.models import PlanRecord


class PlanRepository:
    table_name = "litellm_codeplantable"
    raw_table_name = "LiteLLM_CodePlanTable"
    raw_update_columns = frozenset(
        field_name for field_name in PlanRecord.model_fields if field_name not in {"plan_id", "created_at"}
    )
    raw_column_casts = {"allowed_models": "::text[]", "metadata": "::jsonb"}

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

    async def create(self, data: dict[str, Any]) -> PlanRecord:
        record = await self.table.create(data=self._serialize(data))
        return self._to_model(record)

    async def get(self, plan_id: str) -> Optional[PlanRecord]:
        record = await self.table.find_unique(where={"plan_id": plan_id})
        if record is None:
            return None
        return self._to_model(record)

    async def list(self, status: Optional[str] = None) -> list[PlanRecord]:
        where = {"status": status} if status else {}
        records = await self.table.find_many(where=where, order={"created_at": "desc"})
        return [self._to_model(record) for record in records]

    async def update(self, plan_id: str, data: dict[str, Any]) -> PlanRecord:
        record = await self.table.update(where={"plan_id": plan_id}, data=self._serialize(data))
        return self._to_model(record)

    async def update_if_version(self, plan_id: str, version: int, data: dict[str, Any]) -> Optional[PlanRecord]:
        record = await self._update_returning_if_version(plan_id=plan_id, version=version, data=data)
        if record is None:
            return None
        return self._to_model(record)

    async def _update_returning_if_version(
        self,
        plan_id: str,
        version: int,
        data: dict[str, Any],
    ) -> Optional[Any]:
        serialized = self._serialize(data)
        columns = list(serialized)
        self._validate_update_columns(columns)

        query = self._build_update_returning_query(columns)
        arguments = [serialized[column] for column in columns]
        arguments.extend([plan_id, version])
        rows = await self.writer_db.query_raw(query, *arguments)
        return self._first_row(rows)

    def _build_update_returning_query(self, columns: list[str]) -> str:
        set_clauses = []
        for index, column in enumerate(columns, start=1):
            cast = self.raw_column_casts.get(column, "")
            set_clauses.append(f'"{column}" = ${index}{cast}')
        if "updated_at" not in columns:
            set_clauses.append('"updated_at" = CURRENT_TIMESTAMP')

        plan_id_index = len(columns) + 1
        version_index = len(columns) + 2
        return (
            f'UPDATE "{self.raw_table_name}" SET {", ".join(set_clauses)} '
            f'WHERE "plan_id" = ${plan_id_index} AND "version" = ${version_index} '
            "RETURNING *"
        )

    def _validate_update_columns(self, columns: list[str]) -> None:
        invalid_columns = [column for column in columns if column not in self.raw_update_columns]
        if invalid_columns:
            raise ValueError(f"Unsupported Code Plan update column(s): {', '.join(sorted(invalid_columns))}")

    def _first_row(self, rows: Any) -> Optional[Any]:
        if rows is None:
            return None
        if isinstance(rows, (list, tuple)):
            return rows[0] if rows else None
        return rows

    def _to_model(self, record: Any) -> PlanRecord:
        if isinstance(record, BaseModel):
            data = record.model_dump(exclude_none=False)
        elif isinstance(record, dict):
            data = dict(record)
        else:
            data = {key: getattr(record, key) for key in PlanRecord.model_fields if hasattr(record, key)}

        metadata = data.get("metadata")
        if isinstance(metadata, str):
            data["metadata"] = json.loads(metadata or "{}")
        elif metadata is None:
            data["metadata"] = {}

        return PlanRecord.model_validate(data)

    def _serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        serialized = dict(data)
        metadata = serialized.get("metadata")
        if isinstance(metadata, dict):
            serialized["metadata"] = json.dumps(metadata)
        return serialized

    def _updated_count(self, result: Any) -> int:
        if isinstance(result, int):
            return result
        if isinstance(result, dict) and "count" in result:
            return int(result["count"])

        count = getattr(result, "count", None)
        if count is not None:
            return int(count)
        return int(result)
