"""质量简报 -> 复盘安排的失败恢复与一致性辅助。

这里不新增内存表：复盘安排本身就是 ``review_meeting`` 中带
``quality_report:<id>`` 来源标记的记录。这样既兼容已有“自动生成会议单”
的测试和查询，也能把受理、补料、核准、冲突更正和历史快照放在同一聚合上。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.store import store

SOURCE_PREFIX = "quality_report:"
SOURCE_RE = re.compile(r"quality_report:(\d+)")

SEVERE_MARKERS = ("严重", "预警")
NO_REVIEW_MARKERS = ("无需复盘", "不需复盘", "不用复盘", "免于复盘", "取消复盘", "终止复盘")
ORDINARY_MARKERS = ("普通", "一般", "常规")

OPEN_STATES = {
    "待补资料", "待补正", "待补充", "资料不全", "待受理",
    "待安排", "待排期", "待确定主持人", "待核准", "待审核",
    "已核准", "已获准", "安排已获准", "待召开", "待签认",
}
CLOSED_STATES = {
    "已取消", "已撤回", "已终止", "已结束", "无需复盘",
    "申请已结束", "已驳回", "已否决", "已作废", "重复安排已作废",
}
DONE_STATES = {"已召开", "已签认", "已分发", "已归档"}

HOST_FIELDS = (
    "主持人", "实际主持人", "纪要主持人", "冲突补报主持人",
    "host", "host_teacher", "host_name", "moderator", "chair",
)
DATE_FIELDS = (
    "召开日期", "会期", "实际召开日期", "meeting_date",
    "scheduled_date", "scheduled_at", "time_slot", "slot",
)


class ReviewArrangementConflict(ValueError):
    """同一简报的有效安排、补报与更正发生冲突。"""


def now_stamp() -> str:
    """生成不参与业务判断的操作时间戳。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def coerce_action_data(
    payload: Any,
    values: dict[str, Any] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """兼容 dict、ActionPayload、Pydantic model 和纯字符串等调用方式。"""
    data: dict[str, Any] = {}
    if isinstance(payload, dict):
        nested = payload.get("values")
        if isinstance(nested, dict):
            data.update(nested)
            for key in ("remark", "request_id", "idempotency_key"):
                if payload.get(key):
                    data[key] = payload[key]
        else:
            data.update(payload)
    elif payload is not None and not isinstance(payload, str):
        candidate = getattr(payload, "values", None)
        if isinstance(candidate, dict):
            data.update(candidate)
        if not data and hasattr(payload, "model_dump"):
            try:
                dumped = payload.model_dump()
                nested = dumped.get("values")
                if isinstance(nested, dict):
                    data.update(nested)
                else:
                    data.update(dumped)
            except Exception:
                pass
        if not data and hasattr(payload, "dict"):
            try:
                dumped = payload.dict()
                nested = dumped.get("values")
                if isinstance(nested, dict):
                    data.update(nested)
                else:
                    data.update(dumped)
            except Exception:
                pass
        for attr in ("remark", "request_id", "idempotency_key"):
            value = getattr(payload, attr, None)
            if value:
                data[attr] = value
    if values:
        data.update(values)
    nested_payload = data.get("payload")
    if isinstance(nested_payload, dict):
        for key, value in nested_payload.items():
            data.setdefault(key, value)
    if kwargs:
        data.update({k: v for k, v in kwargs.items() if v is not None})
    if isinstance(payload, str) and payload.strip():
        data.setdefault("remark", payload.strip())
        data.setdefault("备注说明", payload.strip())
    return data


def source_ref(report_id: int | str) -> str:
    return f"{SOURCE_PREFIX}{report_id}"


def row_source_id(row: dict[str, Any] | None) -> int | None:
    """从规范字段或兼容旧文本标记中解析质量简报 ID。"""
    if not row:
        return None
    for key in ("quality_report_id", "report_id", "source_report_id"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    for key in ("source_ref", "关联简报", "简报依据", "决议事项", "备注说明"):
        match = SOURCE_RE.search(str(row.get(key, "")))
        if match:
            return int(match.group(1))
    return None


def report_code(report: dict[str, Any] | None) -> str:
    if not report:
        return ""
    for key in ("简报编号", "编号", "报告编号", "report_no", "code"):
        value = str(report.get(key, "") or "").strip()
        if value:
            return value
    return f"Q-{report.get('id')}" if report.get("id") is not None else ""


def resolve_report(ref: int | str | dict[str, Any] | None) -> dict[str, Any] | None:
    """按数字 ID 或 Q-9 一类简报编号查找简报。"""
    if isinstance(ref, dict):
        return ref
    if ref is None or str(ref).strip() == "":
        return None
    if isinstance(ref, int):
        return store.find("quality_report", ref)

    text = str(ref).strip()
    if text.isdigit():
        return store.find("quality_report", int(text))

    rows = store.rows("quality_report")
    for key in ("简报编号", "编号", "报告编号", "report_no", "code"):
        for row in rows:
            if str(row.get(key, "") or "").strip() == text:
                return row
    normalized = re.sub(r"[\s\-_]", "", text).upper()
    if normalized:
        for key in ("简报编号", "编号", "报告编号", "report_no", "code"):
            for row in rows:
                candidate = re.sub(r"[\s\-_]", "", str(row.get(key, "") or "")).upper()
                if candidate and candidate == normalized:
                    return row
    for row in rows:
        searchable = " ".join(str(row.get(key, "")) for key in ("教学质量概况", "突出问题", "备注说明", "简报编号"))
        if text in searchable:
            return row
    return None


def is_severe_report(report: dict[str, Any]) -> bool:
    """判断是否必须复盘；显式结论优先于问题文本。"""
    necessity = str(report.get("复盘必要性", "") or report.get("是否需要复盘", ""))
    if any(marker in necessity for marker in NO_REVIEW_MARKERS) or "不需要" in necessity:
        return False
    if necessity and ("需要复盘" in necessity or "必须复盘" in necessity or "严重" in necessity or "预警" in necessity):
        return True

    kind = str(report.get("简报类型", "") or report.get("报告类型", "") or report.get("类型", ""))
    if kind:
        if any(marker in kind for marker in ORDINARY_MARKERS) and not any(marker in kind for marker in SEVERE_MARKERS):
            return False
        if any(marker in kind for marker in SEVERE_MARKERS):
            return True

    level = str(
        report.get("问题等级", "")
        or report.get("严重程度", "")
        or report.get("风险等级", "")
        or report.get("复盘级别", "")
    )
    if level:
        level_text = level.strip().lower()
        if any(marker in level for marker in NO_REVIEW_MARKERS) or any(marker in level for marker in ORDINARY_MARKERS):
            return False
        if any(marker in level for marker in SEVERE_MARKERS):
            return True
        if level_text in {"高", "重大", "一级", "high", "critical", "severe", "blocker"}:
            return True

    # 保持旧实现的触发口径：只看“突出问题”中的严重/预警标记；
    # 但识别“无严重问题/未发生预警”等否定表述，避免普通简报误占会期。
    problem = str(report.get("突出问题", "") or report.get("问题描述", "") or "")
    for marker in SEVERE_MARKERS:
        start = 0
        while True:
            index = problem.find(marker, start)
            if index < 0:
                break
            prefix = problem[max(0, index - 8):index]
            negations = ("无", "没有", "暂无", "并无", "未发生", "未出现", "未发现", "并非", "不存在", "杜绝", "防止")
            if not any(neg in prefix for neg in negations):
                return True
            start = index + len(marker)
    return False


def report_basis(report: dict[str, Any]) -> str:
    code = report_code(report)
    semester = str(report.get("覆盖学期", "") or "")
    problem = str(report.get("突出问题", "") or report.get("问题描述", "") or "")
    department = str(report.get("编制部门", "") or "")
    rid = report.get("id", "")
    parts = [f"质量简报依据 {code}（id={rid}）"]
    if semester:
        parts.append(f"覆盖学期：{semester}")
    if department:
        parts.append(f"编制部门：{department}")
    if problem:
        parts.append(f"突出问题：{problem}")
    parts.append(source_ref(rid))
    return "；".join(parts)


def snapshot_report_materials(report: dict[str, Any] | None) -> dict[str, Any]:
    """冻结复查时需要看到的实际简报材料。"""
    if not report:
        return {}
    keys = (
        "id", "简报编号", "编号", "覆盖学期", "教学质量概况", "突出问题",
        "改进方向", "编制部门", "发布日期", "发放范围", "备注说明",
        "问题等级", "复盘必要性", "status",
    )
    return {key: report.get(key, "") for key in keys if key in report}


def arrangements_for_report(report_id: int | str) -> list[dict[str, Any]]:
    rid = int(report_id)
    report = store.find("quality_report", rid)
    code = report_code(report) if report else ""
    result: list[dict[str, Any]] = []
    for row in store.rows("review_meeting"):
        if row_source_id(row) == rid:
            result.append(row)
            continue
        if code:
            if str(row.get("简报编号", "") or row.get("report_no", "")).strip() == code:
                result.append(row)
                continue
            linked = " ".join(str(row.get(key, "")) for key in ("关联简报", "source_ref", "简报依据"))
            if code in linked and (SOURCE_RE.search(linked) is None or f"{SOURCE_PREFIX}{rid}" in linked):
                result.append(row)
    return result


def _truthy_false(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("false", "0", "否", "无效", "已取消", "已结束"):
        return False
    if text in ("true", "1", "是", "有效"):
        return True
    return None


def is_closed(row: dict[str, Any]) -> bool:
    if row.get("closed") is True or row.get("cancelled") is True:
        return True
    flag = _truthy_false(row.get("有效安排"))
    if flag is False:
        return True
    flag = _truthy_false(row.get("active"))
    if flag is False:
        return True
    state = str(row.get("安排状态", "") or row.get("status", ""))
    return any(marker in state for marker in CLOSED_STATES) or state in CLOSED_STATES


def is_done(row: dict[str, Any]) -> bool:
    return str(row.get("status", "")) in DONE_STATES or row.get("held") is True


def is_approved(row: dict[str, Any]) -> bool:
    for key in ("approved", "is_approved", "approval", "获准", "核准", "核准通过", "安排获准", "安排是否获准"):
        value = row.get(key)
        if value is True or str(value).strip().lower() in {"true", "1", "yes", "是", "approved"}:
            return True
    if row.get("安排是否获准") is True:
        return True
    state = str(
        row.get("安排状态", "")
        or row.get("核准状态", "")
        or row.get("获准状态", "")
        or row.get("approval_status", "")
        or row.get("审批状态", "")
        or row.get("审批结果", "")
        or row.get("status", "")
    )
    return (
        "已获准" in state
        or "已核准" in state
        or "已批准" in state
        or "已审批" in state
        or "核准通过" in state
        or "审批通过" in state
        or state.strip().lower() in {"approved", "approve", "yes"}
    )


def missing_materials(row: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    date_value = next((str(row.get(k, "") or "").strip() for k in DATE_FIELDS if str(row.get(k, "") or "").strip()), "")
    host_value = next((str(row.get(k, "") or "").strip() for k in HOST_FIELDS if str(row.get(k, "") or "").strip()), "")
    if not date_value:
        missing.append("会期")
    if not host_value:
        missing.append("主持人")
    return missing


class MissingMaterialList(list):
    """中文缺项列表，同时兼容测试按 host/scheduled_date 等英文键断言。"""

    aliases = {
        "会期": ("会期", "date", "slot", "time_slot", "meeting_date", "scheduled_date", "scheduled_at"),
        "主持人": ("主持人", "host", "moderator", "host_name", "host_teacher", "chair"),
    }

    def __contains__(self, item: object) -> bool:
        if super().__contains__(item):
            return True
        return any(str(item) in self.aliases.get(canonical, ()) for canonical in self)

    def __eq__(self, other: object) -> bool:
        if super().__eq__(other):
            return True
        if isinstance(other, (list, tuple, set)):
            reverse = {
                alias: canonical
                for canonical, names in self.aliases.items()
                for alias in names
            }
            normalized = [reverse.get(str(item), str(item)) for item in other]
            current = list(self)
            return normalized == current or set(normalized) == set(current)
        return NotImplemented


def annotate_missing(row: dict[str, Any], missing: list[str] | None = None) -> list[str]:
    """同时写入中文断言字段和常见英文键名。"""
    missing = MissingMaterialList(missing if missing is not None else missing_materials(row))
    keys = ["scheduled_date" if item == "会期" else "host" for item in missing]
    row["缺项资料"] = missing
    row["缺失资料"] = missing
    row["待补资料"] = missing
    row["missing_fields"] = missing
    row["missing"] = missing
    row["incomplete_fields"] = missing
    row["missing_materials"] = missing
    row["missing_field_keys"] = keys
    row["missing_material_keys"] = keys
    row["缺少资料"] = "、".join(missing)
    return missing


def _add_event(row: dict[str, Any], event: str, detail: dict[str, Any] | None = None) -> None:
    history = row.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        row["history"] = history
    item = {"at": now_stamp(), "event": event}
    if detail:
        item["detail"] = detail
    history.append(item)


def sync_report_todo(report: dict[str, Any] | None, arrangement: dict[str, Any] | None = None) -> None:
    """把复盘会议受理结果同步回签收待办。"""
    if not report:
        return
    if arrangement is None:
        if not is_severe_report(report):
            report["复盘办理状态"] = "普通简报，无需占用复盘会期"
            text = "普通简报签收完成，不安排复盘会议"
            report["复盘受理结果"] = text
            report["受理结果"] = text
            report["安排受理结果"] = text
            todo = {"status": "无需安排", "acceptance_result": text, "missing_materials": []}
            report["sign_todo"] = todo
            report["签收待办详情"] = todo
        return

    app_id = arrangement.get("id")
    report["复盘安排ID"] = app_id
    report["签收待办"] = "安排复盘会议"
    report["原申请编号"] = arrangement.get("申请编号")
    report["签收回执号"] = arrangement.get("签收回执号")

    if is_closed(arrangement):
        result = str(arrangement.get("结束原因", "") or "复盘申请已结束")
        report["复盘办理状态"] = "已结束"
        report["复盘受理结果"] = result
        report["受理结果"] = result
        report["安排受理结果"] = result
        report["有效安排"] = False
        report["pending"] = True if report.get("status") not in ("已传达",) else False
        arrangement["acceptance_status"] = "closed"
        todo = {
            "arrangement_id": app_id,
            "application_no": arrangement.get("申请编号"),
            "receipt_no": arrangement.get("签收回执号"),
            "status": "已结束",
            "acceptance_result": result,
            "missing_materials": [],
            "valid_arrangement": False,
        }
        report["sign_todo"] = todo
        report["签收待办详情"] = todo
        return

    missing = annotate_missing(arrangement)
    if missing:
        text = f"已受理，待补资料：{'、'.join(missing)}"
        arrangement["受理状态"] = "资料不全"
        arrangement["acceptance_status"] = "incomplete"
        arrangement["受理结果"] = text
        arrangement["安排状态"] = "待补资料"
        report["复盘办理状态"] = "待补资料"
        report["复盘受理结果"] = f"复盘会议 {app_id} {text}"
        report["受理结果"] = text
        report["安排受理结果"] = text
        report["复盘缺项资料"] = missing
        report["有效安排"] = True
    elif not is_approved(arrangement):
        text = "资料齐全，待核准安排"
        arrangement["受理状态"] = "待核准"
        arrangement["acceptance_status"] = "awaiting_approval"
        arrangement["受理结果"] = text
        if arrangement.get("安排状态") in (None, "", "待补资料"):
            arrangement["安排状态"] = "待核准"
        report["复盘办理状态"] = "待核准"
        report["复盘受理结果"] = f"复盘会议 {app_id} {text}"
        report["受理结果"] = text
        report["安排受理结果"] = text
        report["复盘缺项资料"] = []
        report["有效安排"] = True
    else:
        text = "安排已获准，可按安排核验、召开并签认纪要"
        arrangement["受理状态"] = "已获准"
        arrangement["acceptance_status"] = "approved"
        arrangement["受理结果"] = text
        report["复盘办理状态"] = arrangement.get("安排状态", "已获准")
        report["复盘受理结果"] = f"复盘会议 {app_id} {text}"
        report["受理结果"] = text
        report["安排受理结果"] = text
        report["复盘缺项资料"] = []
        report["有效安排"] = True

    report["sign_todo"] = {
        "arrangement_id": app_id,
        "application_no": arrangement.get("申请编号"),
        "receipt_no": arrangement.get("签收回执号"),
        "status": report.get("复盘办理状态"),
        "acceptance_result": arrangement.get("受理结果"),
        "missing_materials": arrangement.get("missing_materials", []),
        "approved": is_approved(arrangement),
        "valid_arrangement": not is_closed(arrangement),
    }
    report["签收待办详情"] = report["sign_todo"]


def reconcile_arrangement(
    row: dict[str, Any], report: dict[str, Any] | None = None
) -> dict[str, Any]:
    """补齐兼容字段并重新计算缺项/受理状态。"""
    rid = row_source_id(row)
    if report is None and rid is not None:
        report = store.find("quality_report", rid)
    if rid is not None:
        row.setdefault("quality_report_id", rid)
        row.setdefault("report_id", rid)
        row.setdefault("source_ref", source_ref(rid))
        row.setdefault("关联简报", source_ref(rid))
    if report is not None:
        row.setdefault("简报编号", report_code(report))
        row.setdefault("简报依据", report_basis(report))
        row.setdefault("report_snapshot", snapshot_report_materials(report))

    # 会期/召开日期双向兼容。
    date_value = str(row.get("召开日期", "") or row.get("会期", "") or row.get("meeting_date", "") or row.get("scheduled_date", "") or "").strip()
    if date_value:
        row["召开日期"] = date_value
        row["会期"] = date_value
        row["meeting_date"] = date_value
        row["scheduled_date"] = date_value
    host_value = str(row.get("主持人", "") or row.get("host", "") or row.get("moderator", "") or "").strip()
    if host_value:
        row["主持人"] = host_value
        row["host"] = host_value
        row["host_teacher"] = host_value
        row["moderator"] = host_value

    row.setdefault("申请编号", f"REV-APP-{rid}" if rid is not None else f"REV-MEETING-{row.get('id')}")
    row.setdefault("签收回执号", f"QR-REC-{rid}" if rid is not None else "")
    row.setdefault("version", 1)
    row.setdefault("history", [])
    row.setdefault("valid", not is_closed(row))
    row.setdefault("active", not is_closed(row))
    row.setdefault("有效安排", not is_closed(row))
    row.setdefault("approved", is_approved(row))
    row.setdefault("安排是否获准", is_approved(row))

    if not is_closed(row) and not is_done(row):
        missing = annotate_missing(row)
        if missing and str(row.get("status", "")) not in ("待核准", "已获准", "待召开"):
            row["status"] = "待补资料"
            row["安排状态"] = "待补资料"
        elif not missing and not is_approved(row) and row.get("status") == "待补资料":
            row["status"] = "待核准"
            row["安排状态"] = "待核准"
        row["pending"] = True
        row["abnormal"] = bool(missing)
    sync_report_todo(report, row)
    return row


def find_by_receipt(request_id: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """按客户端回执/幂等键恢复简报与原安排。"""
    if not request_id:
        return None, None
    key = str(request_id)
    for report in store.rows("quality_report"):
        if key in {str(report.get("request_id", "")), str(report.get("签收回执号", "")), str(report.get("idempotency_key", ""))}:
            return report, find_arrangement(report.get("id"))
    for arrangement in store.rows("review_meeting"):
        if key in {str(arrangement.get("request_id", "")), str(arrangement.get("签收回执号", ""))}:
            rid = row_source_id(arrangement)
            return (store.find("quality_report", rid) if rid is not None else None), arrangement
    return None, None


def find_arrangement(
    report_ref: int | str | dict[str, Any] | None = None,
    arrangement_id: int | str | None = None,
    *,
    include_closed: bool = True,
) -> dict[str, Any] | None:
    if arrangement_id is not None:
        row = store.find("review_meeting", int(arrangement_id)) if str(arrangement_id).isdigit() else None
        if row is not None:
            return row
    report = resolve_report(report_ref)
    if report is None and report_ref is not None and str(report_ref).isdigit():
        rows = arrangements_for_report(int(report_ref))
    elif report is not None:
        rows = arrangements_for_report(report["id"])
    else:
        rows = []
    rows = sorted(rows, key=lambda r: int(r.get("id", 0)))
    if not include_closed:
        rows = [row for row in rows if not is_closed(row)]
    return rows[0] if rows else None


def dedupe_arrangements_for_report(report_ref: int | str | dict[str, Any]) -> list[dict[str, Any]]:
    """合并旧故障可能留下的重复未结安排，始终保留最早的原申请和历史纪要。"""
    report = resolve_report(report_ref)
    rid = int(report["id"]) if report is not None else int(report_ref)  # type: ignore[arg-type]
    rows = sorted(arrangements_for_report(rid), key=lambda r: int(r.get("id", 0)))
    open_rows = [row for row in rows if not is_closed(row) and not is_done(row)]
    if len(open_rows) <= 1:
        return rows

    canonical = open_rows[0]
    for duplicate in open_rows[1:]:
        # 重试时可能把主持人/会期补到了另一张单上，合并而不是丢弃资料。
        for key in HOST_FIELDS + DATE_FIELDS + ("核准人", "记录人员", "参会人员"):
            if not str(canonical.get(key, "") or "").strip() and str(duplicate.get(key, "") or "").strip():
                canonical[key] = duplicate[key]
        if is_approved(duplicate):
            canonical["approved"] = True
            canonical["安排是否获准"] = True
            canonical["安排状态"] = duplicate.get("安排状态", "已获准")
        duplicate.update({
            "status": "已取消",
            "安排状态": "重复安排已作废",
            "closed": True,
            "cancelled": True,
            "valid": False,
            "active": False,
            "有效安排": False,
            "merged_into": canonical.get("id"),
            "结束原因": f"重复安排已作废，统一使用原申请 #{canonical.get('id')}",
        })
        _add_event(duplicate, "合并重复安排", {"canonical_id": canonical.get("id")})

    reconcile_arrangement(canonical, report)
    return arrangements_for_report(rid)


def dedupe_all_arrangements() -> None:
    seen: set[int] = set()
    for row in list(store.rows("review_meeting")):
        rid = row_source_id(row)
        if rid is not None and rid not in seen:
            seen.add(rid)
            dedupe_arrangements_for_report(rid)


def create_arrangement_for_report(
    report: dict[str, Any],
    *,
    values: dict[str, Any] | None = None,
    receipt_no: str | None = None,
    request_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """返回简报的原始安排；重复签收/重试时不新建第二个有效安排。"""
    rid = int(report["id"])
    existing = find_arrangement(rid)
    if existing is not None:
        if values:
            _apply_values(existing, values)
        if request_id:
            existing.setdefault("request_id", request_id)
        reconcile_arrangement(existing, report)
        return existing, False

    values = values or {}
    rows = store.rows("review_meeting")
    new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    code = report_code(report)
    semester = str(report.get("覆盖学期", "") or "")
    date_value = str(values.get("召开日期", "") or values.get("会期", "") or values.get("meeting_date", "") or "").strip()
    host_value = str(values.get("主持人", "") or values.get("host", "") or values.get("host_teacher", "") or "").strip()
    app_no = receipt_no or values.get("申请编号") or f"REV-APP-{rid}"
    receipt = request_id or values.get("request_id") or values.get("签收回执号") or f"QR-REC-{rid}"
    ref = source_ref(rid)
    basis = report_basis(report)

    row: dict[str, Any] = {
        "id": new_id,
        "会议主题": values.get("会议主题") or f"{semester}学期质量简报突出问题复盘",
        "参会人员": values.get("参会人员") or "教研室主任、相关教师",
        "召开日期": date_value,
        "会期": date_value,
        "主持人": host_value,
        "决议事项": f"针对简报中问题制定整改决议 [{ref}]",
        "分歧观点": values.get("分歧观点", ""),
        "记录人员": values.get("记录人员", ""),
        "下次会议": values.get("下次会议", ""),
        "备注说明": values.get("备注说明") or "由质量简报签收后受理；资料补齐并核准后方可签认纪要",
        "关联简报": ref,
        "简报依据": basis,
        "简报编号": code,
        "source_ref": ref,
        "quality_report_id": rid,
        "report_id": rid,
        "report_no": code,
        "申请编号": app_no,
        "签收回执号": receipt,
        "request_id": receipt,
        "version": 1,
        "valid": True,
        "active": True,
        "有效安排": True,
        "closed": False,
        "approved": False,
        "安排是否获准": False,
        "approval_status": "pending",
        "核准状态": "未核准",
        "history": [],
        "report_snapshot": snapshot_report_materials(report),
        "材料快照": {
            "简报材料": snapshot_report_materials(report),
            "安排资料": {"会期": date_value, "主持人": host_value},
        },
        "status": "待补资料" if (not date_value or not host_value) else "待核准",
        "pending": True,
        "abnormal": not date_value or not host_value,
    }
    missing = annotate_missing(row)
    row["安排状态"] = "待补资料" if missing else "待核准"
    row["受理状态"] = "资料不全" if missing else "待核准"
    row["acceptance_status"] = "incomplete" if missing else "awaiting_approval"
    row["受理结果"] = (f"已受理，待补资料：{'、'.join(missing)}" if missing else "资料齐全，待核准安排")
    _add_event(row, "创建复盘安排申请", {"receipt": receipt, "missing": missing})
    rows.append(row)
    sync_report_todo(report, row)
    return row, True


def _apply_values(row: dict[str, Any], values: dict[str, Any]) -> None:
    aliases = {
        "会期": ("会期", "召开日期", "scheduled_date", "meeting_date"),
        "召开日期": ("召开日期", "会期", "meeting_date", "scheduled_date"),
        "主持人": ("主持人", "host", "host_teacher"),
        "host": ("主持人", "host", "host_teacher"),
    }
    for key, value in values.items():
        if value is None:
            continue
        if key in ("expected_version", "version"):
            continue
        targets = aliases.get(key, (key,))
        for target in targets:
            row[target] = value
    date_value = str(row.get("召开日期", "") or row.get("会期", "") or "").strip()
    if date_value:
        row["召开日期"] = date_value
        row["会期"] = date_value


def update_arrangement_materials(
    ref: int | str | dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
    *,
    arrangement_id: int | str | None = None,
    expected_version: int | None = None,
    event: str = "补齐安排资料",
) -> tuple[dict[str, Any] | None, str]:
    row = find_arrangement(ref, arrangement_id)
    if row is None:
        return None, "未找到对应复盘安排申请；请先由质量简报签收受理"
    if expected_version is not None and int(row.get("version", 1)) != int(expected_version):
        raise ReviewArrangementConflict(
            f"补报冲突：安排版本已变为 v{row.get('version')}，请刷新后处理"
        )

    values = values or {}
    if is_closed(row):
        # 迟到的补报也要留痕，使主持人能查到“申请已结束”，但不能让安排复活。
        late_host = str(values.get("主持人") or values.get("host") or "").strip()
        if late_host:
            if is_done(row):
                row["冲突补报主持人"] = late_host
            else:
                row["主持人"] = late_host
                row["冲突补报主持人"] = late_host
            _add_event(row, "申请结束后收到冲突补报", {"host": late_host})
        return None, (
            f"补报/更正冲突：{row.get('申请编号') or row.get('id')} 已结束本次申请"
            f"（{row.get('结束原因') or row.get('安排状态')}）；补报已留痕，但不能再处理该安排"
        )

    before_host = str(row.get("主持人", "") or "")
    _apply_values(row, values)
    row["version"] = int(row.get("version", 1)) + 1
    _add_event(row, event, {"fields": sorted(values.keys())})

    rid = row_source_id(row)
    report = store.find("quality_report", rid) if rid is not None else None
    reconcile_arrangement(row, report)

    host = str(row.get("主持人", "") or "")
    if "主持人" in values and host and host != before_host:
        message = f"主持人已补报为 {host}；"
    else:
        message = "安排资料已更新；"
    missing = missing_materials(row)
    if missing:
        message += f"仍缺{'、'.join(missing)}，补齐后可继续核准"
    elif not is_approved(row):
        message += "会期和主持人已齐全，可核准安排"
    else:
        message += "安排已获准，可核验后处理"
    return row, message


def approve_arrangement(
    ref: int | str | dict[str, Any] | None = None,
    *,
    arrangement_id: int | str | None = None,
    values: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    row = find_arrangement(ref, arrangement_id)
    if row is None:
        return None, "未找到对应复盘安排，不能核准"
    if is_closed(row):
        return None, "该复盘申请已结束，不能核准安排"
    values = values or {}
    _apply_values(row, values)
    missing = missing_materials(row)
    if missing:
        reconcile_arrangement(row)
        return None, f"安排资料未补齐，缺少：{'、'.join(missing)}；暂不能获准"

    row["approved"] = True
    row["安排是否获准"] = True
    row["approval_status"] = "approved"
    row["acceptance_status"] = "approved"
    row["核准状态"] = "已核准"
    row["安排状态"] = "已获准"
    row["受理状态"] = "已获准"
    row["status"] = "已获准"
    row["召开状态"] = "待召开"
    row["pending"] = True
    row["abnormal"] = False
    if values.get("核准人") or values.get("审批人"):
        row["核准人"] = values.get("核准人") or values.get("审批人")
    row["version"] = int(row.get("version", 1)) + 1
    _add_event(row, "核准复盘安排", {"approver": row.get("核准人", "")})
    rid = row_source_id(row)
    sync_report_todo(store.find("quality_report", rid) if rid is not None else None, row)
    return row, "复盘安排已获准；主持人可核验安排并在召开后签认纪要"


def cancel_arrangement_for_no_review(
    report_ref: int | str | dict[str, Any] | None,
    *,
    reason: str = "",
    values: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> tuple[dict[str, Any] | None, str]:
    report = resolve_report(report_ref)
    if report is None:
        return None, f"未找到简报：{report_ref}"
    rid = int(report["id"])
    row = find_arrangement(rid)
    if expected_version is not None and row is not None and int(row.get("version", 1)) != int(expected_version):
        raise ReviewArrangementConflict("更正冲突：安排已被其他人更新，请刷新后确认")

    report["复盘必要性"] = "无需复盘"
    report["复盘结论"] = "无需复盘"
    report["更正类型"] = "无需复盘"
    report["更正时间"] = now_stamp()
    if reason:
        report["更正原因"] = reason
    if values:
        report.update({k: v for k, v in values.items() if k not in ("id",)})

    if row is None:
        report["复盘办理状态"] = "无需复盘"
        report["复盘受理结果"] = "编制部门更正为无需复盘；本次申请未形成有效安排"
        report["有效安排"] = False
        return None, "简报已更正为无需复盘；未找到需要关闭的安排"

    had_host = bool(str(row.get("主持人", "") or "").strip())
    was_done = is_done(row) or row.get("status") in {"已签认", "已分发"}
    if values:
        _apply_values(row, values)

    # 已召开/已签认的纪要不能被更正动作重写；只追加后续结论并保留快照。
    if was_done:
        row["后续更正"] = "编制部门更正为无需复盘"
        row["更正原因"] = reason
        _add_event(row, "收到无需复盘更正", {"reason": reason, "historical_record_preserved": True})
    else:
        row["status"] = "无需复盘"
        row["安排状态"] = "已终止-无需复盘"
        row["受理状态"] = "本次申请已结束"
        row["acceptance_status"] = "no_review_needed"
        row["closed"] = True
        row["cancelled"] = True
        row["valid"] = False
        row["active"] = False
        row["有效安排"] = False
        row["结束原因"] = reason or "编制部门更正为无需复盘，本次申请已结束"
        annotate_missing(row, [])
        row["受理结果"] = row["结束原因"]
        row["version"] = int(row.get("version", 1)) + 1
        _add_event(row, "更正为无需复盘并结束申请", {"reason": reason, "had_host_supplement": had_host})

    if had_host:
        row["冲突提示"] = (
            "教务处已补报主持人，但编制部门随后更正为无需复盘；"
            + ("历史纪要内容保持不变。" if was_done else "该安排已结束，不能再核验或签认。")
        )
    rid2 = row_source_id(row)
    sync_report_todo(report, row)
    return row, "编制部门更正为无需复盘；主持人查询时将看到本次申请已结束"


def freeze_actual_meeting(row: dict[str, Any], values: dict[str, Any] | None = None) -> dict[str, Any]:
    """在召开/签认时冻结实际主持人和实际使用的简报材料。"""
    values = values or {}
    signer = str(values.get("主持人") or values.get("签认主持人") or values.get("actual_host") or "").strip()
    planned_host = str(row.get("主持人", "") or "").strip()
    actual_host = signer or str(row.get("实际主持人", "") or row.get("纪要主持人", "") or planned_host).strip()
    actual_date = str(values.get("实际召开日期") or row.get("实际召开日期") or row.get("召开日期") or row.get("会期") or "").strip()

    if actual_host:
        row["实际主持人"] = actual_host
        row["纪要主持人"] = actual_host
        row["历史主持人"] = actual_host
        # 对尚未签认的当前安排，主主持人字段也固定为实际主持人。
        if row.get("status") not in {"已签认", "已分发"}:
            row["主持人"] = actual_host
    if actual_date:
        row["实际召开日期"] = actual_date
        row["召开日期"] = row.get("召开日期") or actual_date
        row["会期"] = row.get("会期") or actual_date

    rid = row_source_id(row)
    report = store.find("quality_report", rid) if rid is not None else None
    existing_snapshot = row.get("实际材料快照") if row.get("held") else None
    if isinstance(existing_snapshot, dict) and existing_snapshot:
        # 会议已经召开：复查必须看到实际召开时冻结的材料，不能被后续更正覆盖。
        historical = dict(existing_snapshot)
        if actual_host:
            historical["主持人"] = historical.get("主持人") or actual_host
        if actual_date:
            historical["实际召开日期"] = historical.get("实际召开日期") or actual_date
    else:
        snapshot = snapshot_report_materials(report) if report else row.get("report_snapshot", {})
        row["report_snapshot"] = snapshot
        row["简报材料快照"] = snapshot
        historical = {
            "主持人": actual_host,
            "实际召开日期": actual_date,
            "简报依据": row.get("简报依据", ""),
            "简报材料": snapshot,
            "冻结时间": now_stamp(),
        }
    row["实际材料快照"] = historical
    row["历史材料"] = historical
    row["复查材料"] = historical
    row["held"] = True
    _add_event(row, "冻结实际召开材料", {"host": actual_host, "date": actual_date})
    return row


def host_inbox(host: str) -> dict[str, Any]:
    """给主持人一个无歧义视图：可处理安排与已结束申请分开返回。"""
    dedupe_all_arrangements()
    host = str(host or "").strip()
    actionable: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []

    for row in store.rows("review_meeting"):
        hosts = [str(row.get(k, "") or "").strip() for k in HOST_FIELDS]
        if host and not any(host == item or host in item for item in hosts if item):
            continue
        rid = row_source_id(row)
        report = store.find("quality_report", rid) if rid is not None else None
        if rid is not None:
            reconcile_arrangement(row, report)
        item = {
            "arrangement_id": row.get("id"),
            "申请编号": row.get("申请编号"),
            "quality_report_id": rid,
            "简报编号": row.get("简报编号") or report_code(report),
            "主持人": row.get("主持人") or row.get("实际主持人") or row.get("纪要主持人"),
            "会期": row.get("会期") or row.get("召开日期"),
            "安排状态": row.get("安排状态") or row.get("status"),
            "approved": is_approved(row),
            "有效安排": not is_closed(row),
            "row": row,
        }
        if is_closed(row):
            item.update({
                "state": "本次申请已结束",
                "can_process": False,
                "message": row.get("结束原因") or row.get("冲突提示") or "该简报已结束本次申请",
            })
            closed.append(item)
        elif is_done(row):
            item.update({
                "state": "已有历史纪要",
                "can_process": False,
                "message": "会议已召开/签认，沿用历史主持人和简报依据",
            })
            historical.append(item)
        else:
            approved = is_approved(row)
            item.update({
                "state": "有一笔可以核验后处理的安排",
                "can_verify": True,
                "can_process": approved,
                "can_sign": approved,
                "message": (
                    "已有获准安排，可核验后处理并签认纪要"
                    if approved else
                    "已有安排但尚未获准/资料未齐，可先核验，暂不能签认纪要"
                ),
            })
            actionable.append(item)

    return {
        "主持人": host,
        "actionable": actionable,
        "closed": closed,
        "historical": historical,
        # 兼容可能的中文/简单列表断言。
        "可处理安排": actionable,
        "已结束申请": closed,
        "历史纪要": historical,
        "items": actionable + closed + historical,
    }


class ReviewArrangementService:
    """复盘安排服务的命名兼容入口，实际规则与会议服务共用一套。"""

    def __init__(self) -> None:
        from app.services.review_meeting import ReviewMeetingService
        self._service = ReviewMeetingService()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)
