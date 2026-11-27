"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None

    @model_validator(mode="after")
    def merge_extra_into_values(self) -> "EntryPayload":
        extra = getattr(self, "model_extra", None) or {}
        for key, value in extra.items():
            if key not in {"values", "remark"} and key not in self.values:
                self.values[key] = value
        return self


class ActionPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str
    remark: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None
    idempotency_key: str | None = None

    @model_validator(mode="after")
    def merge_extra_into_values(self) -> "ActionPayload":
        """允许 {"action": "...", "主持人": "X"} 这种更直接的调用载荷。"""
        reserved = {"action", "remark", "values", "request_id", "idempotency_key"}
        extra = getattr(self, "model_extra", None) or {}
        for key, value in extra.items():
            if key not in reserved and key not in self.values:
                self.values[key] = value
        if self.remark and "备注说明" not in self.values:
            self.values["备注说明"] = self.remark
        return self
