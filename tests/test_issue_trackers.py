"""Ticket lists from an issue tracker's JSON API (Redmine's issues.json shape)."""

from __future__ import annotations

import pytest

from watchpoint.config import Target
from watchpoint.rules import rule_alerts
from watchpoint.textify import json_records, json_to_text


def _issue(id_: int, status: str, cf: list[dict]) -> dict:
    return {"id": id_, "status": {"id": 1, "name": status}, "subject": f"件名{id_}", "custom_fields": cf}


PAGE = {
    "issues": [
        _issue(1, "対応中", [
            {"id": 12, "name": "辛さ", "value": "中辛"},
            {"id": 13, "name": "味見", "value": None},
            {"id": 14, "name": "産地", "multiple": True, "value": ["北海道", "長野"]},
            {"id": 15, "name": "subject", "value": "CF の件名"},
        ]),
        _issue(2, "完了", [{"id": 12, "name": "辛さ", "value": "甘口"}, {"id": 13, "name": "味見", "value": "済"}]),
    ],
    "total_count": 2, "offset": 0, "limit": 100,
}  # fmt: skip


def _t(**kw) -> Target:
    return Target(name="t", source="https://redmine.example.com/issues.json", fetch="json", key="id", **kw)


def test_custom_field_values_are_fields_of_their_own() -> None:
    first, second = json_records(PAGE, _t())
    assert first["辛さ"] == "中辛" and first["産地"] == "北海道, 長野" and first["味見"] == ""
    assert first["subject"] == "件名1" and first["custom_fields.subject"] == "CF の件名"  # no clash
    assert second["味見"] == "済"


def test_a_custom_field_change_shows_as_that_field() -> None:
    before = json_to_text(PAGE, _t())
    PAGE2 = {**PAGE, "issues": [PAGE["issues"][0], _issue(2, "完了", [{"id": 12, "name": "辛さ", "value": "辛口"}])]}
    after = json_to_text(PAGE2, _t())
    assert "辛さ=甘口" in before and "辛さ=辛口" in after


def test_rules_can_name_a_custom_field() -> None:
    t = _t(rules=[{"field": "味見", "required": True, "when": {"status.name": "完了"}}])
    assert rule_alerts(t, json_to_text(PAGE, t)) == []
    t = _t(rules=[{"field": "味見", "required": True}])
    assert rule_alerts(t, json_to_text(PAGE, t)) == ["id 1: 味見 が空です"]


def test_the_list_is_found_without_writing_records() -> None:
    assert [r["subject"] for r in json_records(PAGE, _t())] == ["件名1", "件名2"]
    with pytest.raises(ValueError, match="一覧が複数あります.*issues・users"):
        json_records({"issues": [], "users": []}, _t())
