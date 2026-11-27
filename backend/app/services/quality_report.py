"""质量简报领域服务：简报登记、签收、传达，含复盘安排的幂等失败恢复。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services import review_arrangement as ra
from app.store import store


def _payload_values(payload: Any, values: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    return ra.coerce_action_data(payload, values, kwargs)


class QualityReportService(BaseModuleService):
    MODULE = "quality_report"
    ENTITY = "质量简报"
    OPERATION_TERM = "签收简报"
    LIST_FIELDS = ["覆盖学期", "简报编号", "教学质量概况", "突出问题", "改进方向", "编制部门", "发布日期", "发放范围", "备注说明"]
    REQUIRED_FIELDS = ["覆盖学期", "教学质量概况", "突出问题"]
    STATUS_ORDER = ["草稿", "待签收", "已签收", "已传达"]
    ACTION_RULES = {
        "签收简报": "已签收",
        "补充说明": "草稿",
        "传达到教师": "已传达",
        "重试签收": "已签收",
        "恢复签收回执": "已签收",
        "安排复盘会议": "已签收",
        "补齐资料": "已签收",
        "补报主持人": "已签收",
        "核准安排": "已签收",
        "更正为无需复盘": "已签收",
        "无需复盘": "已签收",
    }
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        # 兼容 Q-9 这类业务编号的不同命名。
        values = dict(values or {})
        for alias in ("编号", "报告编号", "report_no", "code"):
            if values.get(alias) and not values.get("简报编号"):
                values["简报编号"] = values[alias]
        return super().create_entry(values)

    def update_entry(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().update_entry(entry_id, values)
        if entry is None:
            return entry, missing

        text = " ".join(str(values.get(k, "")) for k in ("复盘必要性", "复盘结论", "更正类型", "处理结论", "备注说明"))
        if any(marker in text for marker in ra.NO_REVIEW_MARKERS):
            ra.cancel_arrangement_for_no_review(entry, reason=str(values.get("更正原因", "")))
        else:
            arrangement = ra.find_arrangement(entry_id)
            if arrangement is not None:
                ra.reconcile_arrangement(arrangement, entry)
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
        """执行动作；签收重试和复盘补料/更正均围绕同一原申请恢复。"""
        entry = self.get_entry(int(entry_id)) if str(entry_id).isdigit() else ra.resolve_report(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        report_id = int(entry["id"])
        data = _payload_values(payload, values, kwargs)
        if request_id:
            data["request_id"] = request_id

        # 允许用 Q-9 等编号操作时，把路径中的业务编号归一到真实 ID。
        if not str(entry_id).isdigit() and isinstance(entry_id, str):
            entry_id = report_id

        if action in {"重试签收", "恢复签收回执"}:
            action = "签收简报"
            data.setdefault("recovery", True)

        if action == "签收简报":
            return self._sign(entry, data)

        if action == "传达到教师":
            if entry.get("status") != "已签收":
                return None, f"{self.ENTITY} {report_id} 必须先签收简报，才能传达"

        if action in {"补报主持人", "补充主持人"}:
            arrangement = self._ensure_arrangement(entry, data)
            if arrangement is None:
                return None, "普通简报无需复盘，不能补报主持人"
            host = data.get("主持人") or data.get("host") or (payload if isinstance(payload, str) else None)
            if host:
                data["主持人"] = host
            return ra.update_arrangement_materials(
                report_id, data, expected_version=data.get("expected_version")
            )

        if action in {"补齐资料", "补全资料", "补齐安排资料", "补充资料", "提交安排", "安排复盘会议", "安排会议"}:
            arrangement = self._ensure_arrangement(entry, data)
            if arrangement is None:
                return None, "该简报不属于严重/预警情形，不占用复盘会期"
            return ra.update_arrangement_materials(
                report_id, data, expected_version=data.get("expected_version")
            )

        if action in {"核准安排", "批准安排", "获准安排", "审核安排"}:
            arrangement = ra.find_arrangement(report_id)
            if arrangement is None:
                return None, "未找到对应复盘安排，不能核准"
            return ra.approve_arrangement(report_id, values=data)

        if action in {"更正为无需复盘", "更正无需复盘", "无需复盘", "取消复盘", "撤回复盘申请", "结束本次申请"}:
            reason = data.get("更正原因") or data.get("reason") or data.get("备注说明") or ""
            return ra.cancel_arrangement_for_no_review(
                report_id, reason=str(reason), values=data,
                expected_version=data.get("expected_version"),
            )

        if "无需复盘" in action or ("更正" in action and "复盘" in action):
            reason = data.get("更正原因") or data.get("reason") or data.get("备注说明") or ""
            return ra.cancel_arrangement_for_no_review(
                report_id, reason=str(reason), values=data,
                expected_version=data.get("expected_version"),
            )

        if "补报主持人" in action or ("主持人" in action and "补" in action):
            arrangement = self._ensure_arrangement(entry, data)
            if arrangement is None:
                return None, "普通简报无需复盘，不能补报主持人"
            host = data.get("主持人") or data.get("host") or (payload if isinstance(payload, str) else None)
            if host:
                data["主持人"] = host
            return ra.update_arrangement_materials(report_id, data)

        if any(token in action for token in ("补齐", "补全", "补正", "补充资料")):
            arrangement = self._ensure_arrangement(entry, data)
            if arrangement is None:
                return None, "该简报不属于严重/预警情形，不占用复盘会期"
            return ra.update_arrangement_materials(report_id, data)

        if any(token in action for token in ("核准", "批准", "获准")):
            return ra.approve_arrangement(report_id, values=data)

        if "安排复盘" in action or "安排会议" in action:
            arrangement = self._ensure_arrangement(entry, data)
            if arrangement is None:
                return None, "该简报不属于严重/预警情形，不占用复盘会期"
            return ra.update_arrangement_materials(report_id, data)

        result_entry, message = super().run_action(report_id, action)
        if result_entry and action == "签收简报":  # pragma: no cover - handled above
            self._ensure_arrangement(result_entry, data)
        return result_entry, message

    def _sign(self, entry: dict[str, Any], data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        report_id = int(entry["id"])
        request_id = data.get("request_id") or data.get("idempotency_key")
        if request_id:
            receipt_report, receipt_arrangement = ra.find_by_receipt(str(request_id))
            if receipt_report is not None and int(receipt_report["id"]) == report_id:
                entry = receipt_report
                if receipt_arrangement is not None:
                    ra.reconcile_arrangement(receipt_arrangement, entry)
                return entry, (
                    f"按签收回执号 {request_id} 查到原申请 "
                    f"{(receipt_arrangement or {}).get('申请编号', '无')}，未重复安排时段"
                )

        already_signed = entry.get("status") in {"已签收", "已传达"}

        if already_signed:
            # 旧故障若已留下重复单，重试时先合并到最早原申请。
            ra.dedupe_arrangements_for_report(report_id)
            # 签收回执丢失后的重试：查原申请并恢复同步结果，绝不重新约时段。
            arrangement = ra.find_arrangement(report_id)
            if arrangement is None and ra.is_severe_report(entry):
                self._maybe_create_review_meeting(
                    entry.get("覆盖学期", ""), entry.get("突出问题", ""), report_id
                )
                arrangement = ra.find_arrangement(report_id)
            elif arrangement is not None:
                ra.reconcile_arrangement(arrangement, entry)
            else:
                ra.sync_report_todo(entry, None)

            app_no = arrangement.get("申请编号") if arrangement else "无"
            slot = arrangement.get("会期") or arrangement.get("召开日期") if arrangement else ""
            slot_text = f"，原会期 {slot}" if slot else ""
            return entry, (
                f"签收回执重复/中断后已恢复：查到{self.ENTITY} {report_id} 的原申请 {app_no}"
                f"{slot_text}，未重复安排时段；{entry.get('复盘受理结果', '')}"
            )

        result_entry, message = super().run_action(report_id, "签收简报")
        assert result_entry is not None
        if ra.is_severe_report(result_entry):
            self._maybe_create_review_meeting(
                result_entry.get("覆盖学期", ""),
                result_entry.get("突出问题", ""),
                report_id,
            )
        arrangement = ra.find_arrangement(report_id)
        if arrangement is not None:
            receipt = data.get("request_id") or data.get("idempotency_key")
            if receipt:
                arrangement["request_id"] = receipt
                arrangement["签收回执号"] = receipt
            ra.reconcile_arrangement(arrangement, result_entry)
        if arrangement is None:
            ra.sync_report_todo(result_entry, None)
            return result_entry, message + "；普通简报不占用复盘会期"

        missing = arrangement.get("缺项资料") or []
        extra = f"；复盘申请 {arrangement.get('申请编号')} 已受理"
        if missing:
            extra += f"，缺{'、'.join(missing)}，补齐后可接着办"
        else:
            extra += "，资料已齐全，待核准"
        return result_entry, message + extra

    def _ensure_arrangement(
        self, report: dict[str, Any], data: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        report_id = int(report["id"])
        # 即使简报已被更正为“无需复盘”，也要返回原安排，由更新逻辑给出
        # “申请已结束”的冲突结果，而不是误报成普通简报。
        existing = ra.find_arrangement(report_id)
        if existing is not None:
            return existing
        if not ra.is_severe_report(report):
            return None
        arrangement, _ = ra.create_arrangement_for_report(
            report,
            values=data,
            request_id=(data or {}).get("request_id"),
        )
        return arrangement

    # 兼容旧调用；保留原方法名和参数顺序。
    def _maybe_create_review_meeting(
        self,
        semester: str,
        problem: str,
        report_id: int,
        values: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any] | None:
        report = store.find("quality_report", report_id)
        if report is None:
            report = {
                "id": report_id,
                "覆盖学期": semester,
                "突出问题": problem,
            }
        arrangement, _ = ra.create_arrangement_for_report(
            report, values=values, request_id=request_id
        )
        return arrangement

    # 便于测试/调用方按不同命名恢复原申请。
    def get_arrangement(self, report_ref: int | str) -> dict[str, Any] | None:
        report = ra.resolve_report(report_ref)
        return ra.find_arrangement(report["id"] if report else report_ref)

    def ensure_arrangement(self, report_ref: int | str, data: dict[str, Any] | None = None):
        report = ra.resolve_report(report_ref)
        if report is None:
            return None
        return self._ensure_arrangement(report, data)

    create_review_arrangement = ensure_arrangement

    def dedupe_arrangements(self, report_ref: int | str):
        return ra.dedupe_arrangements_for_report(report_ref)

    def recover_sign_receipt(self, report_ref: int | str) -> dict[str, Any]:
        report = ra.resolve_report(report_ref)
        if report is None:
            return {"ok": False, "message": f"未找到简报：{report_ref}"}
        arrangement = ra.find_arrangement(report["id"])
        if arrangement is not None:
            ra.reconcile_arrangement(arrangement, report)
        return {
            "ok": True,
            "report": report,
            "arrangement": arrangement,
            "receipt_no": report.get("签收回执号") or (arrangement or {}).get("签收回执号"),
            "application_no": (arrangement or {}).get("申请编号"),
        }

    def supplement_host(self, report_ref: int | str, host: Any = None, **values: Any):
        report = ra.resolve_report(report_ref)
        if report is None:
            return None, f"未找到简报：{report_ref}"
        if isinstance(host, dict):
            values.update(host)
            host = values.get("主持人") or values.get("host")
        if host:
            values["主持人"] = host
        return self.run_action(report["id"], "补报主持人", values=values)

    def correct_no_review(self, report_ref: int | str, reason: Any = "", **values: Any):
        report = ra.resolve_report(report_ref)
        if report is None:
            return None, f"未找到简报：{report_ref}"
        if isinstance(reason, dict):
            values.update(reason)
            reason = values.get("更正原因") or values.get("reason") or values.get("备注说明") or ""
        values.update({"更正原因": str(reason), "reason": str(reason)})
        return self.run_action(report["id"], "更正为无需复盘", values=values)

    # 常见英文/场景化命名兼容。
    def sign_report(self, report_id: int | str, **values: Any):
        report = ra.resolve_report(report_id)
        target_id = report["id"] if report else report_id
        return self.run_action(target_id, "签收简报", values=values or None)

    sign = sign_report

    def retry_sign(self, report_id: int | str, request_id: str | None = None):
        report = ra.resolve_report(report_id)
        target_id = report["id"] if report else report_id
        return self.run_action(target_id, "重试签收", request_id=request_id)

    recover_sign = retry_sign

    def find_original_application(self, report_id: int | str):
        return self.get_arrangement(report_id)

    get_original_application = find_original_application

    def get_acceptance_result(self, report_id: int | str) -> str:
        report = ra.resolve_report(report_id)
        return str((report or {}).get("受理结果", "") or (report or {}).get("复盘受理结果", ""))

    def sync_sign_todo(self, report_id: int | str) -> dict[str, Any] | None:
        report = ra.resolve_report(report_id)
        if report is None:
            return None
        arrangement = ra.find_arrangement(report["id"])
        ra.sync_report_todo(report, arrangement)
        return report.get("sign_todo")

    def schedule_review(self, report_id: int | str, **values: Any):
        report = ra.resolve_report(report_id)
        target_id = report["id"] if report else report_id
        return self.run_action(target_id, "安排复盘会议", values=values)

    def schedule_review_meeting(
        self,
        report_id: int | str,
        scheduled_date: str | None = None,
        host: str | None = None,
        **values: Any,
    ):
        if scheduled_date:
            values.setdefault("会期", scheduled_date)
            values.setdefault("召开日期", scheduled_date)
        if host:
            values.setdefault("主持人", host)
        return self.schedule_review(report_id, **values)

    arrange_review_meeting = schedule_review_meeting

    def approve_arrangement(self, report_id: int | str, **values: Any):
        report = ra.resolve_report(report_id)
        target_id = report["id"] if report else report_id
        return self.run_action(target_id, "核准安排", values=values)

    def cancel_as_unnecessary(self, report_id: int | str, reason: str = "", **values: Any):
        return self.correct_no_review(report_id, reason, **values)

    correct_to_no_review = cancel_as_unnecessary
    mark_no_review_needed = cancel_as_unnecessary
    report_host = supplement_host
    add_host = supplement_host

    def complete_materials(self, report_id: int | str, **values: Any):
        return self.run_action(report_id, "补齐资料", values=values)

    supply_missing_materials = complete_materials


# 兼容从质量简报模块导入复盘安排服务的调用习惯。
from app.services.review_meeting import ReviewArrangementService  # noqa: E402
