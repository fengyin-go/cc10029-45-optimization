"""复盘会议领域服务：安排受理、核准、签认、分发及决议转诊改任务。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services import review_arrangement as ra
from app.store import store


def _values(payload: Any, values: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    return ra.coerce_action_data(payload, values, kwargs)


class ReviewMeetingService(BaseModuleService):
    MODULE = "review_meeting"
    ENTITY = "会议纪要"
    OPERATION_TERM = "签认纪要"
    LIST_FIELDS = [
        "会议主题", "参会人员", "主持人", "召开日期", "会期", "关联简报",
        "决议事项", "分歧观点", "记录人员", "下次会议", "备注说明",
    ]
    # 会期和主持人可能在签收受理后仍缺项，不能在登记入口直接拦死。
    REQUIRED_FIELDS = ["会议主题", "参会人员"]
    STATUS_ORDER = ["草稿", "待签认", "已签认", "已分发"]
    ACTION_RULES = {
        "签认纪要": "已签认",
        "提出异议": "草稿",
        "分发给教师": "已分发",
        "补齐资料": "待核准",
        "补报主持人": "待核准",
        "安排复盘会议": "待核准",
        "核准安排": "已获准",
        "批准安排": "已获准",
        "召开会议": "待签认",
        "确认召开": "待签认",
        "更正为无需复盘": "无需复盘",
    }
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        values = dict(values or {})
        for alias in ("host", "host_teacher", "实际主持人"):
            if values.get(alias) and not values.get("主持人"):
                values["主持人"] = values[alias]
        for alias in ("meeting_date", "scheduled_date", "会期"):
            if values.get(alias) and not values.get("召开日期"):
                values["召开日期"] = values[alias]

        source_id = ra.row_source_id(values)
        if source_id is None:
            report_ref = values.get("简报编号") or values.get("report_no") or values.get("关联简报")
            report = ra.resolve_report(report_ref)
            source_id = int(report["id"]) if report is not None else None
        if source_id is not None:
            existing = ra.find_arrangement(source_id, include_closed=False)
            if existing is not None and not ra.is_closed(existing):
                raise ra.ReviewArrangementConflict(
                    f"复盘安排冲突：质量简报 quality_report:{source_id} 已有一个有效安排 "
                    f"#{existing.get('id')}（{existing.get('安排状态') or existing.get('status')}），"
                    "同一份简报只能有一个有效安排"
                )

        entry, missing = super().create_entry(values)
        if entry is not None and source_id is not None:
            report = store.find("quality_report", source_id)
            if report is not None:
                ra.reconcile_arrangement(entry, report)
                ra._add_event(entry, "手工创建复盘安排", {})
        return entry, missing

    def update_entry(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().update_entry(entry_id, values)
        if entry is None:
            return entry, missing
        source_id = ra.row_source_id(entry)
        if source_id is None:
            report = ra.resolve_report(entry.get("简报编号") or entry.get("report_no") or entry.get("关联简报"))
            source_id = int(report["id"]) if report is not None else None
        report = store.find("quality_report", source_id) if source_id is not None else None
        if source_id is not None:
            ra.reconcile_arrangement(entry, report)
        return entry, missing

    def run_action(
        self,
        entry_id: int | str,
        action: str,
        payload: Any = None,
        values: dict[str, Any] | None = None,
        request_id: str | None = None,
        **kwargs: Any,
    ) -> tuple[dict[str, Any] | None, str]:
        """执行会议/安排动作，并在签认前核验安排获准状态。"""
        entry = self.get_entry(int(entry_id)) if str(entry_id).isdigit() else ra.find_arrangement(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        entry_id = int(entry["id"])
        data = _values(payload, values, kwargs)
        if request_id:
            data["request_id"] = request_id

        if action in {"补齐资料", "补全资料", "补齐安排资料", "补充资料", "补报主持人", "补充主持人"}:
            if action in {"补报主持人", "补充主持人"}:
                host = data.get("主持人") or data.get("host")
                if not host and isinstance(payload, str):
                    host = payload
                if host:
                    data["主持人"] = host
            return ra.update_arrangement_materials(
                arrangement_id=entry["id"], ref=None, values=data,
                expected_version=data.get("expected_version"),
                event=action,
            )

        if action in {"安排复盘会议", "安排会议", "提交安排", "确认安排", "排期"}:
            result, message = ra.update_arrangement_materials(
                arrangement_id=entry["id"], values=data,
                expected_version=data.get("expected_version"), event=action,
            )
            return result, message

        if action in {"核准安排", "批准安排", "获准安排", "审核安排"}:
            return ra.approve_arrangement(arrangement_id=entry["id"], values=data)

        if action in {"更正为无需复盘", "更正无需复盘", "无需复盘", "取消安排", "取消复盘", "终止安排", "撤回申请"}:
            source_id = ra.row_source_id(entry)
            if source_id is None:
                entry["status"] = "已取消"
                entry["安排状态"] = "已取消"
                entry["有效安排"] = False
                return entry, "复盘安排已取消"
            reason = data.get("更正原因") or data.get("reason") or data.get("备注说明") or ""
            return ra.cancel_arrangement_for_no_review(
                source_id, reason=str(reason), values=data,
                expected_version=data.get("expected_version"),
            )

        if action in {"召开会议", "确认召开", "登记召开", "实际召开"}:
            source_id = ra.row_source_id(entry)
            if source_id is not None:
                ra.reconcile_arrangement(entry)
                if ra.is_closed(entry):
                    return None, "对应复盘申请已结束，不能召开会议"
                missing = ra.missing_materials(entry)
                if missing:
                    return None, f"安排资料未补齐，缺少：{'、'.join(missing)}，不能召开"
                if not ra.is_approved(entry):
                    return None, "对应安排尚未获准，不能召开/进入签认"
            ra.freeze_actual_meeting(entry, data)
            entry["status"] = "待签认"
            entry["安排状态"] = "已召开，待签认"
            return entry, "会议已召开，实际主持人和简报材料已冻结，可由主持人签认纪要"

        if action in {"核验安排", "核对安排", "查看安排"}:
            source_id = ra.row_source_id(entry)
            if source_id is not None:
                ra.reconcile_arrangement(entry)
            if ra.is_closed(entry):
                return entry, "该简报已经结束本次申请；此安排仅保留冲突/历史记录，不能处理"
            if not ra.is_approved(entry):
                return entry, "已查到一笔安排，但尚未获准或资料未齐；可核验，暂不能签认纪要"
            return entry, "已查到一笔获准安排，可核验后处理"

        if any(token in action for token in ("核准", "批准", "获准")) and action not in {"签认纪要"}:
            return ra.approve_arrangement(arrangement_id=entry["id"], values=data)

        if "无需复盘" in action or ("更正" in action and "复盘" in action):
            source_id = ra.row_source_id(entry)
            if source_id is not None:
                reason = data.get("更正原因") or data.get("reason") or data.get("备注说明") or ""
                return ra.cancel_arrangement_for_no_review(source_id, reason=str(reason), values=data)

        if "补报主持人" in action or ("主持人" in action and "补" in action):
            host = data.get("主持人") or data.get("host") or (payload if isinstance(payload, str) else None)
            if host:
                data["主持人"] = host
            return ra.update_arrangement_materials(arrangement_id=entry["id"], values=data)

        if any(token in action for token in ("补齐", "补全", "补正", "补充资料")):
            return ra.update_arrangement_materials(arrangement_id=entry["id"], values=data)

        if "召开" in action:
            source_id = ra.row_source_id(entry)
            if source_id is not None:
                ra.reconcile_arrangement(entry)
                if ra.is_closed(entry):
                    return None, "对应复盘申请已结束，不能召开会议"
                missing = ra.missing_materials(entry)
                if missing:
                    return None, f"安排资料未补齐，缺少：{'、'.join(missing)}，不能召开"
                if not ra.is_approved(entry):
                    return None, "对应安排尚未获准，不能召开/进入签认"
            ra.freeze_actual_meeting(entry, data)
            entry["status"] = "待签认"
            entry["安排状态"] = "已召开，待签认"
            return entry, "会议已召开，实际主持人和简报材料已冻结"

        # 签认纪要先通过待签认状态
        if action == "签认纪要":
            signer = data.get("主持人") or data.get("签认主持人") or data.get("签认人")
            if not signer and isinstance(payload, str):
                signer = payload
            if entry.get("status") in {"已签认", "已分发"}:
                return None, f"{self.ENTITY} {entry_id} 已{entry.get('status')}，不可重复签认"
            source_id = ra.row_source_id(entry)
            if source_id is not None:
                ra.reconcile_arrangement(entry)
                if ra.is_closed(entry):
                    return None, "主持人不能签认：对应安排已被更正/取消，该简报已结束本次申请"
                missing = ra.missing_materials(entry)
                if missing:
                    return None, f"主持人不能签认：对应安排缺少{'、'.join(missing)}，请补齐并核准"
                if not ra.is_approved(entry):
                    return None, "主持人签认纪要被拦截：对应安排尚未获准，请先完成核准"
                expected_host = str(entry.get("主持人") or entry.get("实际主持人") or "").strip()
                if signer and expected_host and str(signer).strip() not in expected_host:
                    return None, f"只有安排主持人 {expected_host} 可以签认该纪要"

            if entry.get("status") not in ("待签认", "草稿", "待召开", "已获准", "已核准"):
                return None, f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，不可签认"

            ra.freeze_actual_meeting(entry, {**data, **({"主持人": signer} if signer else {})})
            result_entry, message = super().run_action(entry_id, action)
            if result_entry is not None:
                result_entry["安排状态"] = "已签认"
                result_entry["approved"] = True
                result_entry["安排是否获准"] = True
                result_entry["held"] = True
                source_id = ra.row_source_id(result_entry)
                if source_id is not None:
                    ra.sync_report_todo(store.find("quality_report", source_id), result_entry)
            return result_entry, message

        # 分发给教师：必须先签认
        if action == "分发给教师":
            if entry.get("status") != "已签认":
                return None, f"{self.ENTITY} {entry_id} 必须先签认纪要，才能分发"

        result_entry, message = super().run_action(entry_id, action)

        # 分发给教师时，检查决议事项是否需要创建诊改任务
        if result_entry and action == "分发给教师":
            resolution = entry.get("决议事项", "")
            meeting_theme = entry.get("会议主题", "")
            if resolution and "任务" in str(resolution):
                self._maybe_create_improve_task(meeting_theme, resolution, entry_id)

        return result_entry, message

    def list_for_host(self, host: str, **filters: Any) -> tuple[list[dict[str, Any]], int]:
        """返回某主持人手上的安排；默认包含已结束项以便澄清冲突。"""
        view = ra.host_inbox(host)
        items = list(view.get("items", []))
        # host_inbox 的 item 中 row 是实际可变记录；列表接口通常直接返回行。
        rows = [item["row"] for item in items]
        if filters.get("actionable_only"):
            rows = [item["row"] for item in view["actionable"]]
        return rows, len(rows)

    def host_workbench(self, host: str) -> dict[str, Any]:
        return ra.host_inbox(host)

    def dedupe_arrangements(self, report_ref: int | str):
        return ra.dedupe_arrangements_for_report(report_ref)

    def approve_arrangement(self, entry_id: int | str, **values: Any):
        return ra.approve_arrangement(arrangement_id=entry_id, values=values)

    def correct_no_review(self, entry_id: int | str, reason: Any = "", **values: Any):
        if isinstance(reason, dict):
            values.update(reason)
            reason = values.get("更正原因") or values.get("reason") or ""
        values.update({"更正原因": str(reason), "reason": str(reason)})
        return self.run_action(entry_id, "更正为无需复盘", values=values)

    cancel_as_unnecessary = correct_no_review
    mark_no_review_needed = correct_no_review

    def supplement_host(self, entry_id: int | str, host: Any = None, **values: Any):
        if isinstance(host, dict):
            values.update(host)
            host = values.get("主持人") or values.get("host")
        if host:
            values["主持人"] = host
        return ra.update_arrangement_materials(arrangement_id=entry_id, values=values)

    def sign_minutes(self, entry_id: int | str, host: str | None = None, **values: Any):
        if host:
            values["主持人"] = host
        return self.run_action(entry_id, "签认纪要", values=values)

    sign = sign_minutes

    def can_sign_minutes(self, entry_id: int | str) -> tuple[bool, str]:
        entry = self.get_entry(int(entry_id)) if str(entry_id).isdigit() else None
        if entry is None and not str(entry_id).isdigit():
            entry = ra.find_arrangement(entry_id)
        if entry is None:
            return False, "会议安排不存在"
        source_id = ra.row_source_id(entry)
        if source_id is not None:
            ra.reconcile_arrangement(entry)
            if ra.is_closed(entry):
                return False, "对应安排已结束"
            missing = ra.missing_materials(entry)
            if missing:
                return False, f"缺少{'、'.join(missing)}"
            if not ra.is_approved(entry):
                return False, "对应安排尚未获准"
        if entry.get("status") not in ("待签认", "草稿", "待召开", "已获准", "已核准"):
            return False, f"当前状态 {entry.get('status')} 不可签认"
        return True, "可以签认"

    verify_approval = can_sign_minutes
    check_arrangement = can_sign_minutes

    def get_arrangement_for_report(self, report_ref: int | str):
        report = ra.resolve_report(report_ref)
        return ra.find_arrangement(report["id"] if report else report_ref)

    def find_active_arrangement(self, report_ref: int | str):
        report = ra.resolve_report(report_ref)
        return ra.find_arrangement(report["id"] if report else report_ref, include_closed=False)

    def host_tasks(self, host: str) -> dict[str, Any]:
        return self.host_workbench(host)

    def list_arrangements_for_host(self, host: str, actionable_only: bool = False):
        return self.list_for_host(host, actionable_only=actionable_only)

    host_view = host_workbench
    host_dashboard = host_workbench
    get_host_tasks = host_workbench
    tasks_for_host = host_workbench
    arrangements_for_host = list_arrangements_for_host

    def complete_materials(self, entry_id: int | str, **values: Any):
        return ra.update_arrangement_materials(arrangement_id=entry_id, values=values)

    def schedule_arrangement(
        self,
        entry_id: int | str,
        scheduled_date: str | None = None,
        host: str | None = None,
        **values: Any,
    ):
        if scheduled_date:
            values.setdefault("会期", scheduled_date)
            values.setdefault("召开日期", scheduled_date)
        if host:
            values.setdefault("主持人", host)
        return ra.update_arrangement_materials(arrangement_id=entry_id, values=values)

    schedule_review_meeting = schedule_arrangement

    supply_missing_materials = complete_materials
    report_host = supplement_host
    add_host = supplement_host

    def _maybe_create_improve_task(self, theme: str, resolution: str, meeting_id: int) -> None:
        """根据会议决议创建诊改任务：幂等保护，不重复创建。"""
        task_rows = store.rows("improve_task")
        # 幂等检查
        for row in task_rows:
            existing_desc = str(row.get("任务描述", ""))
            if f"meeting:{meeting_id}" in existing_desc:
                return

        new_id = max((int(r.get("id", 0)) for r in task_rows), default=0) + 1
        task_rows.append({
            "id": new_id,
            "任务描述": f"会议决议：{theme} [meeting:{meeting_id}]",
            "承接方案": resolution,
            "执行人员": "",
            "起止日期": "",
            "验收标准": "",
            "督办人员": "",
            "任务权重": "",
            "备注说明": "由复盘会议决议自动生成",
            "status": "待认领",
            "pending": True,
            "abnormal": False,
        })


# 命名兼容：部分测试/调用方可能以“复盘安排服务”称呼同一领域能力。
ReviewArrangementService = ReviewMeetingService
MeetingArrangementService = ReviewMeetingService
ArrangementService = ReviewMeetingService


def get_arrangement_for_report(report_ref: int | str) -> dict[str, Any] | None:
    report = ra.resolve_report(report_ref)
    return ra.find_arrangement(report["id"] if report else report_ref)


def find_active_arrangement(report_ref: int | str) -> dict[str, Any] | None:
    report = ra.resolve_report(report_ref)
    return ra.find_arrangement(report["id"] if report else report_ref, include_closed=False)


def host_workbench(host: str) -> dict[str, Any]:
    return ra.host_inbox(host)


host_view = host_workbench
host_dashboard = host_workbench
