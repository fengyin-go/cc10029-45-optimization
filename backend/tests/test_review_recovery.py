"""质量简报签收后复盘安排的失败恢复测试。"""
import pytest

from app.services.quality_report import QualityReportService
from app.services.review_meeting import ReviewMeetingService
from app.services.review_arrangement import ReviewArrangementConflict
from app.seed import SEED_ROWS
from app.store import store


def clear_all():
    for name in store.module_names():
        store.rows(name).clear()


def restore_seed():
    clear_all()
    for name, rows in SEED_ROWS.items():
        store.rows(name).extend(dict(row) for row in rows)


@pytest.fixture(autouse=True)
def clean_store():
    clear_all()
    yield
    # 恢复全局内存仓库的示例数据，避免本文件影响排序在后面的测试模块。
    restore_seed()


def make_report(**overrides):
    values = {
        "简报编号": "Q-9",
        "覆盖学期": "2025-秋季",
        "教学质量概况": "整体偏弱",
        "突出问题": "多门课程出现严重问题并触发教学预警",
        "编制部门": "质量编制科",
    }
    values.update(overrides)
    report, missing = QualityReportService().create_entry(values)
    assert report is not None, missing
    return report


def test_sign_creates_one_incomplete_application_and_syncs_missing_materials():
    quality = QualityReportService()
    report = make_report()

    signed, message = quality.run_action(report["id"], "签收简报")
    assert signed is not None
    assert "缺会期、主持人" in message

    arrangements = [
        row for row in store.rows("review_meeting")
        if f"quality_report:{report['id']}" in str(row.get("决议事项", ""))
    ]
    assert len(arrangements) == 1
    arrangement = arrangements[0]
    assert arrangement["缺项资料"] == ["会期", "主持人"]
    assert arrangement["有效安排"] is True
    assert arrangement["approved"] is False
    assert "待补资料" in arrangement["安排状态"]
    assert "会期" in report["复盘受理结果"]
    assert "主持人" in report["复盘受理结果"]


def test_retry_receipt_reuses_original_application_and_slot():
    quality = QualityReportService()
    report = make_report()
    quality.run_action(report["id"], "签收简报")
    first = quality.get_arrangement(report["id"])
    ReviewMeetingService().update_entry(first["id"], {"会期": "2026-01-12", "主持人": "周主持"})

    retried, message = quality.run_action(report["id"], "签收简报")
    second = quality.get_arrangement(report["id"])

    assert retried is not None
    assert second["id"] == first["id"]
    assert second["会期"] == "2026-01-12"
    assert "未重复安排时段" in message
    assert len([
        row for row in store.rows("review_meeting")
        if row.get("quality_report_id") == report["id"]
    ]) == 1


def test_sign_minutes_requires_approved_arrangement_then_freezes_basis():
    quality = QualityReportService()
    meetings = ReviewMeetingService()
    report = make_report()
    quality.run_action(report["id"], "签收简报")
    arrangement = quality.get_arrangement(report["id"])

    blocked, block_message = meetings.run_action(arrangement["id"], "签认纪要")
    assert blocked is None
    assert "缺少会期、主持人" in block_message

    meetings.update_entry(arrangement["id"], {"会期": "2026-01-12", "主持人": "吴主持"})
    blocked, block_message = meetings.run_action(arrangement["id"], "签认纪要")
    assert blocked is None
    assert "尚未获准" in block_message

    approved, approve_message = meetings.run_action(arrangement["id"], "核准安排")
    assert approved is not None
    assert approved["安排是否获准"] is True
    assert "已获准" in approve_message

    signed, message = meetings.run_action(arrangement["id"], "签认纪要", {"主持人": "吴主持"})
    assert signed is not None
    assert signed["status"] == "已签认"
    assert signed["纪要主持人"] == "吴主持"
    assert signed["实际材料快照"]["简报材料"]["简报编号"] == "Q-9"
    assert f"quality_report:{report['id']}" in signed["简报依据"]
    assert "已签认" in message


def test_q9_host_supplement_conflicting_correction_is_unambiguous():
    quality = QualityReportService()
    meetings = ReviewMeetingService()
    report = make_report()
    quality.run_action(report["id"], "签收简报")

    supplemented, supplement_message = quality.supplement_host("Q-9", "郑主持")
    assert supplemented is not None
    assert "郑主持" in supplement_message

    closed, correction_message = quality.correct_no_review("Q-9", "编制部门复核为无需复盘")
    assert closed is not None
    assert closed["有效安排"] is False
    assert "已结束" in correction_message

    view = meetings.host_workbench("郑主持")
    assert len(view["actionable"]) == 0
    assert len(view["closed"]) == 1
    assert "已结束" in view["closed"][0]["state"]

    rows, total = meetings.list_for_host("郑主持")
    assert total == 1
    assert rows[0]["有效安排"] is False


def test_correction_first_makes_late_supplement_a_conflict_and_host_sees_closed():
    quality = QualityReportService()
    meetings = ReviewMeetingService()
    report = make_report()
    quality.run_action(report["id"], "签收简报")
    quality.correct_no_review(report["id"], "无需复盘")

    result, message = quality.supplement_host(report["id"], " late主持人".strip())
    assert result is None
    assert "已结束" in message

    view = meetings.host_workbench("late主持人")
    assert len(view["actionable"]) == 0
    assert len(view["closed"]) == 1


def test_same_report_cannot_have_two_active_arrangements():
    quality = QualityReportService()
    meetings = ReviewMeetingService()
    report = make_report()
    quality.run_action(report["id"], "签收简报")

    with pytest.raises(ReviewArrangementConflict, match="只能有一个有效安排"):
        meetings.create_entry({
            "会议主题": "重复复盘",
            "参会人员": "教研室主任",
            "主持人": "王主持",
            "召开日期": "2026-01-13",
            "关联简报": f"quality_report:{report['id']}",
        })


def test_ordinary_report_does_not_occupy_review_slot():
    quality = QualityReportService()
    report = make_report(突出问题="常规作业格式问题", 问题等级="普通")

    result, message = quality.run_action(report["id"], "签收简报")
    assert result is not None
    assert "不占用复盘会期" in message
    assert store.rows("review_meeting") == []
    assert "不安排复盘会议" in report["复盘受理结果"]


def test_held_meeting_keeps_historical_host_and_materials_after_later_correction():
    quality = QualityReportService()
    meetings = ReviewMeetingService()
    report = make_report()
    quality.run_action(report["id"], "签收简报")
    arrangement = quality.get_arrangement(report["id"])
    meetings.update_entry(arrangement["id"], {"会期": "2026-01-12", "主持人": "历史主持人"})
    meetings.run_action(arrangement["id"], "核准安排")
    meetings.run_action(arrangement["id"], "召开会议")
    meetings.run_action(arrangement["id"], "签认纪要")

    before = store.find("review_meeting", arrangement["id"])
    snapshot = before["实际材料快照"]
    quality.correct_no_review(report["id"], "事后更正无需复盘")
    quality.update_entry(report["id"], {"突出问题": "事后改写后的问题"})

    after = store.find("review_meeting", arrangement["id"])
    assert after["主持人"] == "历史主持人"
    assert after["纪要主持人"] == "历史主持人"
    assert after["实际材料快照"] == snapshot
    assert after["实际材料快照"]["简报材料"]["突出问题"] != "事后改写后的问题"
