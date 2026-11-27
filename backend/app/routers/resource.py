"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import inspect
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.get("/review_meeting/host-inbox/{host}")
def host_inbox(host: str) -> dict[str, Any]:
    """主持人冲突澄清视图：可处理安排、已结束申请和历史纪要分组返回。"""
    service = require_service("review_meeting")
    workbench = getattr(service, "host_workbench", None)
    if workbench is None:
        raise HTTPException(status_code=404, detail="主持人工作台不可用")
    return workbench(host)


@router.get("/quality_report/receipt/{report_ref}")
def sign_receipt(report_ref: str) -> dict[str, Any]:
    """按数字 ID 或 Q-9 一类简报编号恢复签收回执和原申请。"""
    service = require_service("quality_report")
    recover = getattr(service, "recover_sign_receipt", None)
    if recover is None:
        raise HTTPException(status_code=404, detail="签收回执恢复能力不可用")
    result = recover(report_ref)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    try:
        entry, missing = service.create_entry(payload.values)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc), entry=None)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.patch("/{module}/{entry_id}", response_model=ActionResult)
def update_entry(module: str, entry_id: int, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message="；".join(missing) or "更新失败")
    message = f"{service.ENTITY}已更新"
    if missing:
        message += f"；仍缺少：{'、'.join(missing)}"
    return ActionResult(ok=True, message=message, entry=entry)


@router.put("/{module}/{entry_id}", response_model=ActionResult)
def replace_entry(module: str, entry_id: int, payload: EntryPayload) -> ActionResult:
    return update_entry(module, entry_id, payload)


def _dispatch_action(service: Any, entry_id: int, payload: ActionPayload):
    """把扩展载荷传给支持失败恢复的服务，同时兼容旧的两参 run_action。"""
    signature = inspect.signature(service.run_action)
    if "payload" in signature.parameters:
        return service.run_action(entry_id, payload.action.strip(), payload)
    return service.run_action(entry_id, payload.action.strip())


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    try:
        entry, message = _dispatch_action(service, entry_id, payload)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc), entry=None)
    return ActionResult(ok=entry is not None, message=message, entry=entry)
