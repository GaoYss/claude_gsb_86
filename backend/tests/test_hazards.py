"""隐患登记与整改跟踪接口测试。"""

import csv
import io
from datetime import date, timedelta

from tests.conftest import API


def _create_hazard(client, reservoir_id: int, **overrides) -> dict:
    payload = {
        "reservoir_id": reservoir_id,
        "title": "坝体局部裂缝",
        "category": "dam_body",
        "severity": "general",
        "source": "inspection",
        "discoverer": "张三",
        "description": "坝顶发现横向裂缝",
        "plan": "灌浆处理",
    }
    payload.update(overrides)
    response = client.post(f"{API}/hazards", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_hazard_writes_register_record(client, make_reservoir):
    reservoir = make_reservoir()
    hazard = _create_hazard(client, reservoir["id"])

    assert hazard["code"].startswith("YH" + date.today().strftime("%Y%m%d"))
    assert hazard["status"] == "registered"
    assert hazard["is_overdue"] is False
    assert hazard["closed_on"] is None
    assert [record["action"] for record in hazard["rectifications"]] == ["register"]
    assert hazard["reservoir"]["name"] == reservoir["name"]


def test_create_hazard_rejects_inspection_of_other_reservoir(client, make_reservoir):
    first = make_reservoir()
    second = make_reservoir()
    inspection = client.post(
        f"{API}/inspections", json={"reservoir_id": first["id"], "inspector": "张三"}
    ).json()

    response = client.post(
        f"{API}/hazards",
        json={
            "reservoir_id": second["id"],
            "inspection_id": inspection["id"],
            "title": "跨水库引用",
            "category": "dam_body",
        },
    )
    assert response.status_code == 422


def test_measure_record_moves_hazard_to_rectifying(client, make_reservoir):
    reservoir = make_reservoir()
    hazard = _create_hazard(client, reservoir["id"])

    response = client.post(
        f"{API}/hazards/{hazard['id']}/rectifications",
        json={"action": "measure", "content": "安排施工队进场灌浆", "operator": "李四"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "rectifying"

    records = body["rectifications"]
    assert records[-1]["status_from"] == "registered"
    assert records[-1]["status_to"] == "rectifying"


def test_progress_record_does_not_change_status(client, make_reservoir):
    reservoir = make_reservoir()
    hazard = _create_hazard(client, reservoir["id"])

    response = client.post(
        f"{API}/hazards/{hazard['id']}/rectifications",
        json={"action": "progress", "content": "已联系施工队"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "registered"


def test_full_rectification_flow(client, make_reservoir):
    reservoir = make_reservoir()
    hazard = _create_hazard(client, reservoir["id"])
    url = f"{API}/hazards/{hazard['id']}/transition"

    invalid = client.post(url, json={"target_status": "pending_acceptance"})
    assert invalid.status_code == 409
    assert "不允许" in invalid.json()["detail"]

    started = client.post(
        url, json={"target_status": "rectifying", "operator": "李四"}
    )
    assert started.status_code == 200
    assert started.json()["status"] == "rectifying"

    missing_content = client.post(url, json={"target_status": "pending_acceptance"})
    assert missing_content.status_code == 422
    assert "处理说明" in missing_content.json()["detail"]

    submitted = client.post(
        url,
        json={"target_status": "pending_acceptance", "content": "裂缝已灌浆处理", "operator": "李四"},
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "pending_acceptance"

    rejected = client.post(
        url,
        json={"target_status": "rectifying", "content": "现场复核仍有渗水，退回整改"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rectifying"

    client.post(url, json={"target_status": "pending_acceptance", "content": "已重新处理"})
    closed = client.post(url, json={"target_status": "closed", "operator": "验收组"})
    assert closed.status_code == 200
    body = closed.json()
    assert body["status"] == "closed"
    assert body["closed_on"] == date.today().isoformat()
    assert body["rectifications"][-1]["status_to"] == "closed"

    # 已销号为终态：既不能再流转，也不能追加记录
    assert client.post(url, json={"target_status": "rectifying"}).status_code == 409
    blocked = client.post(
        f"{API}/hazards/{hazard['id']}/rectifications",
        json={"action": "progress", "content": "补充记录"},
    )
    assert blocked.status_code == 409


def test_status_cannot_be_changed_through_update(client, make_reservoir):
    reservoir = make_reservoir()
    hazard = _create_hazard(client, reservoir["id"])

    response = client.put(
        f"{API}/hazards/{hazard['id']}", json={"status": "closed", "severity": "major"}
    )
    assert response.status_code == 422
    assert "transition" in response.json()["detail"]

    ok = client.put(f"{API}/hazards/{hazard['id']}", json={"severity": "major"})
    assert ok.status_code == 200
    assert ok.json()["severity"] == "major"


def test_overdue_flag_and_filter(client, make_reservoir):
    reservoir = make_reservoir()
    overdue = _create_hazard(
        client,
        reservoir["id"],
        title="逾期隐患",
        deadline=(date.today() - timedelta(days=2)).isoformat(),
    )
    future = _create_hazard(
        client,
        reservoir["id"],
        title="未到期隐患",
        deadline=(date.today() + timedelta(days=5)).isoformat(),
    )
    assert overdue["is_overdue"] is True
    assert future["is_overdue"] is False

    page = client.get(f"{API}/hazards", params={"overdue_only": True}).json()
    assert page["total"] == 1
    assert page["items"][0]["title"] == "逾期隐患"

    open_page = client.get(f"{API}/hazards", params={"open_only": True}).json()
    assert open_page["total"] == 2


def test_hazard_filters_and_delete(client, make_reservoir):
    reservoir = make_reservoir()
    hazard = _create_hazard(client, reservoir["id"], severity="major", category="spillway")

    by_severity = client.get(f"{API}/hazards", params={"severity": "major"}).json()
    assert by_severity["total"] == 1

    by_category = client.get(f"{API}/hazards", params={"category": "outlet"}).json()
    assert by_category["total"] == 0

    by_keyword = client.get(f"{API}/hazards", params={"keyword": "坝体局部"}).json()
    assert by_keyword["total"] == 1

    assert client.delete(f"{API}/hazards/{hazard['id']}").status_code == 200
    assert client.get(f"{API}/hazards/{hazard['id']}").status_code == 404


def test_missing_hazard_returns_404(client):
    assert client.get(f"{API}/hazards/123456").status_code == 404


def test_discovered_date_range_filter(client, make_reservoir):
    reservoir = make_reservoir()
    old = _create_hazard(
        client, reservoir["id"], title="老隐患", discovered_on="2026-03-01"
    )
    recent = _create_hazard(
        client, reservoir["id"], title="新隐患", discovered_on="2026-08-01"
    )

    page = client.get(
        f"{API}/hazards",
        params={"discovered_from": "2026-07-01", "discovered_to": "2026-12-31"},
    ).json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == recent["id"]

    inclusive = client.get(
        f"{API}/hazards", params={"discovered_from": "2026-03-01"}
    ).json()
    assert {item["id"] for item in inclusive["items"]} == {old["id"], recent["id"]}


def test_conflicting_filters_return_422_with_message(client, make_reservoir):
    reservoir = make_reservoir()
    _create_hazard(client, reservoir["id"])

    conflict_status_open = client.get(
        f"{API}/hazards", params={"status": "closed", "open_only": "true"}
    )
    assert conflict_status_open.status_code == 422
    assert "冲突" in conflict_status_open.json()["detail"]

    conflict_status_overdue = client.get(
        f"{API}/hazards", params={"status": "closed", "overdue_only": "true"}
    )
    assert conflict_status_overdue.status_code == 422
    assert "逾期" in conflict_status_overdue.json()["detail"]

    conflict_date_range = client.get(
        f"{API}/hazards",
        params={"discovered_from": "2026-09-01", "discovered_to": "2026-01-01"},
    )
    assert conflict_date_range.status_code == 422
    assert "开始日期晚于结束日期" in conflict_date_range.json()["detail"]

    # 导出接口同样拦截冲突，不会导出一张看似正常的空表
    export_conflict = client.get(
        f"{API}/hazards/export", params={"status": "closed", "open_only": "true"}
    )
    assert export_conflict.status_code == 422


def test_list_summary_matches_total(client, make_reservoir):
    """顶部汇总计数与列表条数同源。"""
    reservoir = make_reservoir()
    _create_hazard(
        client,
        reservoir["id"],
        title="逾期的一般隐患",
        severity="general",
        deadline=(date.today() - timedelta(days=3)).isoformat(),
    )
    _create_hazard(
        client,
        reservoir["id"],
        title="未到期重大隐患",
        severity="major",
        deadline=(date.today() + timedelta(days=3)).isoformat(),
    )
    _create_hazard(
        client,
        reservoir["id"],
        title="已销号隐患",
        deadline=(date.today() - timedelta(days=10)).isoformat(),
    )
    # 把第三条销号
    target = client.get(f"{API}/hazards", params={"keyword": "已销号隐患"}).json()["items"][0]
    client.post(
        f"{API}/hazards/{target['id']}/transition",
        json={"target_status": "rectifying"},
    )
    client.post(
        f"{API}/hazards/{target['id']}/transition",
        json={"target_status": "pending_acceptance", "content": "整改完成"},
    )
    closed = client.post(
        f"{API}/hazards/{target['id']}/transition", json={"target_status": "closed"}
    )
    assert closed.status_code == 200

    page = client.get(f"{API}/hazards", params={"reservoir_id": reservoir["id"]}).json()
    assert page["total"] == 3
    assert page["summary"] == {"total": 3, "open": 2, "overdue": 1}

    # 叠加等级条件后，条数与汇总仍一致
    major_page = client.get(
        f"{API}/hazards", params={"severity": "major"}
    ).json()
    assert major_page["total"] == major_page["summary"]["total"] == 1
    assert major_page["summary"]["open"] == 1
    assert major_page["summary"]["overdue"] == 0


def test_export_csv_uses_same_filters_as_list(client, make_reservoir):
    """导出内容与列表同条件同源。"""
    reservoir = make_reservoir()
    other = make_reservoir(name="另一座水库", code="SK-TEST-OTHER")
    mine = _create_hazard(client, reservoir["id"], title="本库隐患", severity="major")
    _create_hazard(client, other["id"], title="外库隐患", severity="major")

    params = {"reservoir_id": reservoir["id"], "severity": "major"}
    page = client.get(f"{API}/hazards", params=params).json()
    assert page["total"] == 1

    response = client.get(f"{API}/hazards/export", params=params)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]

    text = response.content.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    assert rows[0][0] == "隐患编号"
    assert len(rows) == 2  # 表头 + 1 条
    assert rows[1][0] == mine["code"]
    assert rows[1][1] == reservoir["name"]
    assert rows[1][4] == "重大隐患"

    # 不带条件时导出全部
    all_rows = list(
        csv.reader(io.StringIO(client.get(f"{API}/hazards/export").content.decode("utf-8-sig")))
    )
    assert len(all_rows) == 3


def test_combined_filters_stay_consistent_at_scale(client, make_reservoir):
    """隐患攒到较大规模后，组合筛选的列表条数、汇总、导出三者必须一致。"""
    reservoir = make_reservoir()
    other_reservoir = make_reservoir(name="干扰水库", code="SK-TEST-NOISE")

    severities = ["general", "serious", "major"]
    categories = ["dam_body", "spillway", "outlet", "seepage", "slope"]
    statuses = ["registered", "rectifying", "pending_acceptance", "closed"]
    created = []
    for i in range(240):
        severity = severities[i % 3]
        category = categories[i % 5]
        deadline = (
            (date.today() + timedelta(days=(i % 7) - 3)).isoformat()
            if i % 4
            else None
        )
        hazard = _create_hazard(
            client,
            reservoir["id"],
            title=f"规模隐患{i:03d}",
            severity=severity,
            category=category,
            deadline=deadline,
        )
        created.append(hazard)

    # 干扰数据：另一座水库的 60 条，任何条件组合都不应混入
    for i in range(60):
        _create_hazard(client, other_reservoir["id"], title=f"干扰隐患{i:03d}")

    # 把约一半隐患推进到各种状态，closed 约占 1/4
    for idx, hazard in enumerate(created):
        target_status = statuses[idx % 4]
        current = client.get(f"{API}/hazards/{hazard['id']}").json()["status"]
        order = ["registered", "rectifying", "pending_acceptance", "closed"]
        steps = order.index(target_status) - order.index(current)
        for step in range(steps):
            if step == 0:
                body = {"target_status": "rectifying"}
            elif step == 1:
                body = {"target_status": "pending_acceptance", "content": "提交验收"}
            else:
                body = {"target_status": "closed"}
            resp = client.post(f"{API}/hazards/{hazard['id']}/transition", json=body)
            assert resp.status_code == 200, resp.text

    # 多条件组合：水库 + 等级 + 部位 + 未销号 + 日期范围
    params = {
        "reservoir_id": reservoir["id"],
        "severity": "major",
        "category": "dam_body",
        "open_only": "true",
        "discovered_from": "2000-01-01",
    }
    page = client.get(f"{API}/hazards", params=params).json()

    # 与无分页全量拉取对账，避免只验证总数自洽
    all_matched = client.get(
        f"{API}/hazards",
        params={**params, "page_size": 100, "page": 1},
    ).json()
    assert all_matched["pages"] == 1
    expected_ids = {
        item["id"]
        for item in all_matched["items"]
        if item["severity"] == "major"
        and item["category"] == "dam_body"
        and item["status"] != "closed"
    }
    assert page["total"] == len(expected_ids)
    assert page["summary"]["total"] == len(expected_ids)
    assert page["summary"]["open"] == len(expected_ids)
    assert all(
        item["severity"] == "major"
        and item["category"] == "dam_body"
        and item["status"] != "closed"
        for item in page["items"]
    )

    export_text = client.get(f"{API}/hazards/export", params=params).content.decode(
        "utf-8-sig"
    )
    export_rows = list(csv.reader(io.StringIO(export_text)))
    assert len(export_rows) - 1 == page["total"] == page["summary"]["total"]
    exported_codes = {row[0] for row in export_rows[1:]}
    page_codes = {
        item["code"] for item in all_matched["items"] if item["id"] in expected_ids
    }
    assert exported_codes == page_codes

    # 只看逾期：列表、汇总、导出继续同源
    overdue_params = {"reservoir_id": reservoir["id"], "overdue_only": "true"}
    overdue_page = client.get(f"{API}/hazards", params=overdue_params).json()
    overdue_export = list(
        csv.reader(
            io.StringIO(
                client.get(f"{API}/hazards/export", params=overdue_params)
                .content.decode("utf-8-sig")
            )
        )
    )
    assert overdue_page["total"] == overdue_page["summary"]["total"]
    assert overdue_page["summary"]["overdue"] == overdue_page["total"]
    assert len(overdue_export) - 1 == overdue_page["total"]
    assert overdue_page["total"] > 0

