"""隐患组合筛选、冲突提示、导出同源与规模稳定性测试。"""

import csv
import io
from datetime import date, timedelta

from app.core.config import settings
from tests.conftest import API

CATEGORIES = ["dam_body", "spillway", "outlet", "seepage", "slope", "facility", "other"]
SEVERITIES = ["general", "serious", "major"]


def _create_hazard(client, reservoir_id, **overrides) -> dict:
    payload = {
        "reservoir_id": reservoir_id,
        "title": "测试隐患",
        "category": "dam_body",
        "severity": "general",
        "source": "inspection",
    }
    payload.update(overrides)
    response = client.post(f"{API}/hazards", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _close_hazard(client, hazard_id: int) -> None:
    """走状态机把隐患销号。"""
    client.post(
        f"{API}/hazards/{hazard_id}/transition",
        json={"target_status": "rectifying"},
    )
    client.post(
        f"{API}/hazards/{hazard_id}/transition",
        json={"target_status": "pending_acceptance", "content": "整改完成"},
    )
    closed = client.post(
        f"{API}/hazards/{hazard_id}/transition",
        json={"target_status": "closed", "content": "验收通过"},
    )
    assert closed.status_code == 200, closed.text


def _csv_rows(response_text: str) -> list[list[str]]:
    # 去掉 UTF-8 BOM 后再解析
    return list(csv.reader(io.StringIO(response_text.lstrip("\ufeff"))))


def test_combined_filters_narrow_step_by_step(client, make_reservoir):
    reservoir = make_reservoir()
    other = make_reservoir()
    today = date.today()

    target = _create_hazard(
        client,
        reservoir["id"],
        title="重大溢洪道隐患",
        category="spillway",
        severity="major",
        discovered_on=today.isoformat(),
    )
    _create_hazard(
        client,
        reservoir["id"],
        title="一般坝体隐患",
        category="dam_body",
        severity="general",
        discovered_on=(today - timedelta(days=40)).isoformat(),
    )
    _create_hazard(
        client,
        other["id"],
        title="其它水库重大隐患",
        category="spillway",
        severity="major",
    )

    params = {
        "reservoir_id": reservoir["id"],
        "category": "spillway",
        "severity": "major",
        "status": "registered",
        "discovered_from": (today - timedelta(days=7)).isoformat(),
        "discovered_to": today.isoformat(),
    }
    page = client.get(f"{API}/hazards", params=params).json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == target["id"]

    # 时间范围整体落在未来：条件本身合法，只是正常无结果（不应报冲突）
    future_page = client.get(
        f"{API}/hazards",
        params={
            **params,
            "discovered_from": (today + timedelta(days=1)).isoformat(),
            "discovered_to": (today + timedelta(days=7)).isoformat(),
        },
    ).json()
    assert future_page["total"] == 0
    assert future_page["items"] == []


def test_date_range_is_inclusive_on_both_ends(client, make_reservoir):
    reservoir = make_reservoir()
    day = date.today() - timedelta(days=10)
    hazard = _create_hazard(client, reservoir["id"], discovered_on=day.isoformat())

    page = client.get(
        f"{API}/hazards",
        params={
            "discovered_from": day.isoformat(),
            "discovered_to": day.isoformat(),
        },
    ).json()
    ids = [item["id"] for item in page["items"]]
    assert hazard["id"] in ids


def test_conflicting_closed_status_and_open_only_rejected(client, make_reservoir):
    make_reservoir()
    response = client.get(
        f"{API}/hazards", params={"status": "closed", "open_only": "true"}
    )
    assert response.status_code == 422
    assert "冲突" in response.json()["detail"]


def test_conflicting_closed_status_and_overdue_only_rejected(client, make_reservoir):
    make_reservoir()
    response = client.get(
        f"{API}/hazards", params={"status": "closed", "overdue_only": "true"}
    )
    assert response.status_code == 422
    assert "冲突" in response.json()["detail"]


def test_reversed_date_range_rejected(client, make_reservoir):
    make_reservoir()
    response = client.get(
        f"{API}/hazards",
        params={"discovered_from": "2026-09-10", "discovered_to": "2026-09-01"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "冲突" in detail and "发现日期" in detail


def test_conflicts_are_rejected_on_export_as_well(client, make_reservoir):
    make_reservoir()
    response = client.get(
        f"{API}/hazards/export",
        params={"status": "closed", "open_only": "true"},
    )
    assert response.status_code == 422
    assert "冲突" in response.json()["detail"]


def test_export_matches_list_total_and_uses_chinese_labels(client, make_reservoir):
    reservoir = make_reservoir()
    _create_hazard(
        client,
        reservoir["id"],
        title="溢洪道老化",
        category="spillway",
        severity="serious",
        assignee="王五",
    )
    _create_hazard(client, reservoir["id"], title="坝顶裂缝", category="dam_body")

    params = {"reservoir_id": reservoir["id"], "category": "spillway"}
    page = client.get(f"{API}/hazards", params=params).json()
    assert page["total"] == 1

    response = client.get(f"{API}/hazards/export", params=params)
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]

    rows = _csv_rows(response.text)
    # 表头 + 数据行，数据行数必须与列表 total 同源一致
    assert len(rows) == page["total"] + 1
    assert rows[0][0] == "隐患编号"
    data_row = rows[1]
    assert data_row[1] == "溢洪道老化"
    assert data_row[2] == reservoir["name"]
    assert data_row[3] == "溢洪道"
    assert data_row[4] == "较大隐患"
    assert data_row[5] == "待整改"


def test_export_empty_result_is_actionable_error(client, make_reservoir):
    reservoir = make_reservoir()
    response = client.get(
        f"{API}/hazards/export",
        params={"reservoir_id": reservoir["id"], "category": "outlet"},
    )
    assert response.status_code == 422
    assert "没有可导出" in response.json()["detail"]


def test_export_over_limit_asks_to_narrow_filters(client, make_reservoir, monkeypatch):
    monkeypatch.setattr(settings, "export_max_rows", 2)
    reservoir = make_reservoir()
    _create_hazard(client, reservoir["id"], title="隐患一")
    _create_hazard(client, reservoir["id"], title="隐患二")
    _create_hazard(client, reservoir["id"], title="隐患三")

    response = client.get(f"{API}/hazards/export", params={"reservoir_id": reservoir["id"]})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "3 条" in detail and "上限 2 条" in detail


def test_pagination_and_export_stay_consistent_at_scale(client, make_reservoir):
    """较大规模 + 组合筛选：各页合计、total、导出行数三者一致，无重复无遗漏。"""
    reservoirs = [make_reservoir(), make_reservoir(), make_reservoir()]
    today = date.today()

    created: list[int] = []
    for index in range(240):
        reservoir = reservoirs[index % 3]
        hazard = _create_hazard(
            client,
            reservoir["id"],
            title=f"规模隐患{index:03d}",
            category=CATEGORIES[index % len(CATEGORIES)],
            severity=SEVERITIES[index % len(SEVERITIES)],
            discovered_on=(today - timedelta(days=index % 60)).isoformat(),
        )
        created.append(hazard["id"])

    # 每 7 条销号 1 条，制造多种状态
    for hazard_id in created[::7]:
        _close_hazard(client, hazard_id)

    target_reservoir = reservoirs[1]
    params = {
        "reservoir_id": target_reservoir["id"],
        "severity": "serious",
        "discovered_from": (today - timedelta(days=60)).isoformat(),
        "discovered_to": today.isoformat(),
    }

    first_page = client.get(
        f"{API}/hazards", params={**params, "page": 1, "page_size": 30}
    ).json()
    total = first_page["total"]
    assert total == 80  # 240 条中属于 2 号水库且等级为 serious 的共 80 条
    assert first_page["pages"] == 3  # 确实跨页

    seen: list[int] = []
    page_no = 1
    while True:
        page = client.get(
            f"{API}/hazards",
            params={**params, "page": page_no, "page_size": 30},
        ).json()
        seen.extend(item["id"] for item in page["items"])
        if page_no >= page["pages"]:
            break
        page_no += 1

    assert len(seen) == total
    assert len(set(seen)) == total  # 无重复，结合总数即无遗漏

    # 导出与筛选同源：行数 == total
    export = client.get(f"{API}/hazards/export", params=params)
    assert export.status_code == 200
    rows = _csv_rows(export.text)
    assert len(rows) == total + 1

    # 同一口径下，已销号与未销号两部分加总应等于总数，状态拆分不重不漏
    closed_page = client.get(f"{API}/hazards", params={**params, "status": "closed"}).json()
    open_page = client.get(f"{API}/hazards", params={**params, "open_only": "true"}).json()
    assert closed_page["total"] + open_page["total"] == total
