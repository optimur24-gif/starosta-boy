from datetime import date

import httpx
import pytest

from bgmu_bot.bgmu import BGMUSource, readable_details
from bgmu_bot.source import SourceError
from bgmu_bot.website import WebsiteClient


def event(subject="Биология", *, day="2026-10-21", subgroup="А", teacher="Преподаватель", room="Кабинет 1"):
    return {
        "date": day, "start": "09:00", "end": "10:35", "kind": "Практическое занятие", "subject": subject,
        "details": [{"group": "Л-101", "groupId": 1, "subgroup": subgroup, "teacher": teacher, "teacherId": 10,
                     "room": room, "roomId": 20 if room == "Кабинет 1" else 21, "building": "Корпус 1"}],
        "groups": [{"id": 1, "name": "Л-101"}],
    }


def api_response(request, *, events=None):
    query = dict(request.url.params)
    if query["action"] == "period":
        return {"success": True, "years": {"26": "2026/2027"}, "period": {"year": 26, "semester": 1}}
    if query["action"] == "catalog":
        if query["type"] == "group":
            items = [
                {"id": 1, "name": "Л-101", "faculty": "Лечебный факультет", "course": 1},
                {"id": 2, "name": "Л-201", "faculty": "Лечебный факультет", "course": 2},
                {"id": 3, "name": "П-101", "faculty": "Педиатрический факультет", "course": 1},
            ]
        else:
            items = [{"id": 10, "name": "Преподаватель" if query["type"] == "teacher" else "Кабинет", "subtitle": "Корпус 1"}]
        return {"success": True, "items": items}
    return {"success": True, "entity": {"name": "Л-101"}, "events": events if events is not None else [event()],
            "meta": {"stale": False, "updatedAt": "2026-10-21T08:00:00+00:00"}, "undatedRecords": 0}


@pytest.mark.asyncio
async def test_real_api_contract_navigation_and_no_schedule_cache():
    requested = []

    def respond(request):
        assert request.url.path == "/api.php"
        requested.append(dict(request.url.params))
        return httpx.Response(200, json=api_response(request))

    async with WebsiteClient(transport=httpx.MockTransport(respond)) as website:
        source = BGMUSource(website)
        assert {choice.key for choice in (await source.menu(())).choices} == {"group"}
        assert (await source.menu(("group",))).choices[0].label == "2026/2027"
        assert (await source.menu(("group", "26"))).choices[0].key == "1"
        path = ("group", "26", "1")
        assert len((await source.menu(path)).choices) == 2
        path += ("Лечебный факультет",)
        assert [choice.key for choice in (await source.menu(path)).choices] == ["1", "2"]
        path += ("1",)
        assert [choice.label for choice in (await source.menu(path)).choices] == ["Л-101"]
        path += ("1",)
        assert [choice.key for choice in (await source.menu(path)).choices] == ["*", "А"]
        assert await source.menu(path + ("А",)) is None
        before = sum(item["action"] == "schedule" for item in requested)
        first = await source.schedule(path + ("А",), date(2026, 10, 21))
        second = await source.schedule(path + ("А",), date(2026, 10, 21))
        assert first == second
        assert len(first.sections) == 1
        assert "Практическое занятие" in first.sections[0].title
        assert sum(item["action"] == "schedule" for item in requested) == before + 2
        assert all(item.get("year") == "26" and item.get("semester") == "1" for item in requested if item["action"] != "period")
        assert "subgroup=%D0%90" in first.source_url
        assert any("13:00 (Уфа)" in notice for notice in first.notices)


@pytest.mark.asyncio
async def test_subgroup_keeps_common_lessons_and_filters_other_subgroups():
    events = [event("Общая лекция", subgroup=""), event("Для А", subgroup="А"), event("Для Б", subgroup="Б"), event("Другой день", day="2026-10-22")]
    async with WebsiteClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=api_response(request, events=events)))) as website:
        source = BGMUSource(website)
        result = await source.schedule(("group", "26", "1", "Лечебный факультет", "1", "1", "А"), date(2026, 10, 21))
    assert [section.entries[0] for section in result.sections] == ["Общая лекция", "Для А"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"success": False, "message": "upstream failed"},
    {"success": True, "entity": {"name": "Л-101"}},
    {"success": True, "entity": {}, "events": [{"subject": "wrong schema"}]},
])
async def test_upstream_errors_and_changed_schema_are_not_empty_schedules(payload):
    async with WebsiteClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as website:
        with pytest.raises(SourceError):
            await BGMUSource(website).schedule(("group", "26", "1", "Лечебный факультет", "1", "1"), date(2026, 10, 21))


@pytest.mark.asyncio
async def test_empty_day_and_upstream_staleness_remain_distinct():
    def respond(request):
        data = api_response(request, events=[])
        data["meta"]["stale"] = True
        data["meta"]["warning"] = "Источник временно недоступен"
        data["undatedRecords"] = 2
        return httpx.Response(200, json=data)

    async with WebsiteClient(transport=httpx.MockTransport(respond)) as website:
        result = await BGMUSource(website).schedule(("group", "26", "1", "Лечебный факультет", "1", "1"), date(2026, 10, 21))
    assert not result.sections
    assert any("сохранённую копию" in value for value in result.notices)
    assert any("без точной даты" in value for value in result.notices)


def test_detail_merging_preserves_teacher_and_location_differences():
    rows = [event(subgroup="Б")["details"][0], event(subgroup="А")["details"][0],
            event(subgroup="А", room="Кабинет 2")["details"][0]]
    result = readable_details(rows)
    assert len(result) == 2
    assert "Подгруппа А, Б" in result[0]
    assert "Кабинет 2" in result[1]


@pytest.mark.asyncio
async def test_live_check_exercises_all_sections_and_reports_counts_without_lessons():
    import json
    from bgmu_bot.check_source import verify_live

    async with WebsiteClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=api_response(request)))) as website:
        result = await verify_live(website)
    assert {scenario["mode"] for scenario in result["scenarios"]} == {"group"}
    assert all(scenario["day_lessons"] == 1 for scenario in result["scenarios"])
    assert "Биология" not in json.dumps(result, ensure_ascii=False)
    assert result["scenarios"][0]["subgroups_checked"] == ["А"]
