"""联测资料整批替换与海图零点换算的端到端测试。"""

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_tables():
    conn = db.get_conn()
    with db.lock():
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM calibration_points")
        conn.execute("DELETE FROM survey_connections")
        conn.execute("DELETE FROM survey_staff")
        conn.execute("DELETE FROM survey_meta")
    yield


# ---------------------------------------------------------------- 工具

def post_obs(items):
    r = client.post("/observations", json={"observations": items})
    assert r.status_code == 200, r.text
    return r.json()


def post_cal(items):
    r = client.post("/calibrations", json={"calibrations": items})
    assert r.status_code == 200, r.text
    return r.json()


def put_survey(payload, expect=200):
    r = client.put("/survey", json=payload)
    assert r.status_code == expect, r.text
    return r.json()


def chart_tides(start=0, end=10**13):
    r = client.get(
        "/chart-tide-levels", params={"start_ms": start, "end_ms": end}
    )
    assert r.status_code == 200, r.text
    return r.json()


def by_id(body):
    return {item["obs_id"]: item for item in body["items"]}


def survey(chart="chart0", staffs=None, connections=None):
    return {
        "chart_datum": chart,
        "staffs": staffs or [],
        "connections": connections or [],
    }


def conn(cid, frm, to, delta):
    return {"connection_id": cid, "from_datum": frm, "to_datum": to, "delta_mm": delta}


@pytest.fixture
def three_obs():
    """A 段 1000ms 三点，B 段一个点，均可校时。"""
    post_cal([
        {"boot_segment": "A", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "A", "counter": 100, "utc_ms": 1100},
        {"boot_segment": "B", "counter": 0, "utc_ms": 2000},
    ])
    post_obs([
        {"obs_id": "a1", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
        {"obs_id": "a2", "boot_segment": "A", "counter": 50, "tide_level": 2.0},
        {"obs_id": "a3", "boot_segment": "A", "counter": 100, "tide_level": 3.0},
        {"obs_id": "b1", "boot_segment": "B", "counter": 0, "tide_level": 4.0},
    ])


# ---------------------------------------------------------------- 资料替换

def test_survey_put_and_get_roundtrip():
    payload = survey(
        chart="chart0",
        staffs=[{"boot_segment": "A", "staff": "s1"}],
        connections=[conn("c1", "s1", "chart0", 123)],
    )
    body = put_survey(payload)
    assert body["chart_datum"] == "chart0"
    assert body["staffs"] == [{"boot_segment": "A", "staff": "s1"}]
    assert body["connections"] == [
        {"connection_id": "c1", "from_datum": "s1", "to_datum": "chart0", "delta_mm": 123}
    ]
    assert body["staff_count"] == 1 and body["connection_count"] == 1

    got = client.get("/survey").json()
    assert got == body


def test_survey_replace_discards_old_material():
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "old"}],
        connections=[conn("old-1", "old", "chart0", 500)],
    ))
    put_survey(survey(
        chart="new-chart",
        staffs=[{"boot_segment": "B", "staff": "new"}],
        connections=[conn("new-1", "new", "new-chart", -20)],
    ))
    got = client.get("/survey").json()
    assert got["chart_datum"] == "new-chart"
    assert [s["boot_segment"] for s in got["staffs"]] == ["B"]
    assert [c["connection_id"] for c in got["connections"]] == ["new-1"]


@pytest.mark.parametrize("connections", [
    [conn("dup", "a", "b", 1), conn("dup", "b", "c", 2)],
    [conn(f"c{i}", "a", "b", i) for i in range(17)],
])
def test_survey_rejects_duplicate_ids_and_more_than_16(connections):
    put_survey(survey(connections=[conn("keep", "a", "b", 7)]))
    put_survey(survey(connections=connections), expect=422)
    got = client.get("/survey").json()
    # 校验失败不改动旧资料
    assert [c["connection_id"] for c in got["connections"]] == ["keep"]


def test_survey_accepts_exactly_16_connections():
    cs = [conn(f"c{i}", "a", f"d{i}", i) for i in range(16)]
    body = put_survey(survey(connections=cs))
    assert body["connection_count"] == 16


def test_survey_accepts_compact_location_and_record_fields():
    # 兼容常见写法：datum / staff 映射 / from+to+delta
    r = client.put("/survey", json={
        "datum": "chart0",
        "staff": {"A": "s1"},
        "surveys": [{"id": "c1", "from": "s1", "to": "chart0", "delta": -30}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chart_datum"] == "chart0"
    assert body["staffs"] == [{"boot_segment": "A", "staff": "s1"}]
    assert body["connections"] == [{
        "connection_id": "c1", "from_datum": "s1",
        "to_datum": "chart0", "delta_mm": -30,
    }]


def test_survey_rejects_duplicate_segment_staff():
    r = client.put("/survey", json=survey(staffs=[
        {"boot_segment": "A", "staff": "s1"},
        {"boot_segment": "A", "staff": "s2"},
    ]))
    assert r.status_code == 422


def test_post_survey_is_alias_of_put():
    payload = survey(
        staffs=[{"boot_segment": "A", "staff": "s"}],
        connections=[conn("c", "s", "chart0", 25)],
    )
    r = client.post("/survey", json=payload)
    assert r.status_code == 200
    assert client.get("/survey").json() == r.json()


def test_chart_tide_alias_route_matches_primary(three_obs):
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "s"}],
        connections=[conn("c", "s", "chart0", 10)],
    ))
    a = client.get("/chart-tide-levels", params={"start_ms": 0, "end_ms": 10**13})
    b = client.get(
        "/survey/chart-tide-levels", params={"start_ms": 0, "end_ms": 10**13}
    )
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()


# ---------------------------------------------------------------- 换算

def test_conversion_adds_route_delta_and_lists_connection_ids(three_obs):
    put_survey(survey(
        chart="chart0",
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[
            conn("c1", "sA", "mid", 100),
            conn("c2", "mid", "chart0", 240),
        ],
    ))
    body = chart_tides()
    items = by_id(body)
    assert body["chart_datum"] == "chart0"
    assert body["count"] == 4  # B 段未登记标尺，仍返回但待联测
    a1 = items["a1"]
    assert a1["status"] == "ok"
    assert a1["tide_level"] == pytest.approx(1.34)
    assert a1["chart_tide_level"] == a1["tide_level"]
    assert a1["connection_ids"] == ["c1", "c2"]
    assert items["a2"]["tide_level"] == pytest.approx(2.34)
    assert items["a3"]["tide_level"] == pytest.approx(3.34)


def test_reverse_connection_negates_delta(three_obs):
    # 资料记录的是 chart0 -> sA 加 250mm；标尺 sA 换算到海图零点应减 250mm
    put_survey(survey(
        chart="chart0",
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[conn("c1", "chart0", "sA", 250)],
    ))
    items = by_id(chart_tides())
    assert items["a1"]["tide_level"] == pytest.approx(0.75)
    assert items["a1"]["connection_ids"] == ["c1"]


def test_pending_survey_when_no_staff_or_no_route(three_obs):
    put_survey(survey(
        chart="chart0",
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[conn("c1", "sA", "other", 100)],  # 到不了 chart0
    ))
    items = by_id(chart_tides())
    assert items["a1"]["status"] == "pending_survey"
    assert items["a1"]["tide_level"] is None
    assert "无联测路线" in items["a1"]["reason"]
    assert "未登记标尺" in items["b1"]["reason"]
    assert items["b1"]["status"] == "pending_survey"


def test_pending_survey_when_no_survey_at_all(three_obs):
    body = chart_tides()
    assert body["chart_datum"] is None
    assert all(i["status"] == "pending_survey" for i in body["items"])


def test_staff_equal_to_chart_datum_converts_without_records(three_obs):
    put_survey(survey(
        chart="chart0",
        staffs=[{"boot_segment": "A", "staff": "chart0"}],
    ))
    items = by_id(chart_tides())
    assert items["a1"]["status"] == "ok"
    assert items["a1"]["tide_level"] == pytest.approx(1.0)
    assert items["a1"]["connection_ids"] == []


def test_negative_delta_and_float_precision():
    post_cal([{"boot_segment": "A", "counter": 0, "utc_ms": 1000}])
    post_obs([{"obs_id": "x", "boot_segment": "A", "counter": 0, "tide_level": 2.31}])
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "s"}],
        connections=[conn("c", "s", "chart0", -110)],
    ))
    item = by_id(chart_tides())["x"]
    assert item["tide_level"] == pytest.approx(2.2)
    assert item["tide_level"] == 2.2  # 十进制运算，无 2.199999... 尾差


# ---------------------------------------------------------------- 复测

def test_two_simple_routes_with_different_delta_is_pending_resurvey(three_obs):
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[
            conn("r1", "sA", "chart0", 100),
            conn("r2a", "sA", "mid", 40),
            conn("r2b", "mid", "chart0", 90),  # r1=100, r2a+r2b=130
        ],
    ))
    item = by_id(chart_tides())["a1"]
    assert item["status"] == "pending_resurvey"
    assert item["tide_level"] is None
    assert len(item["routes"]) == 2
    routes = {tuple(r["connection_ids"]): r["delta_mm"] for r in item["routes"]}
    assert routes == {("r1",): 100, ("r2a", "r2b"): 130}
    assert "待复测" in item["reason"]


def test_two_simple_routes_with_same_delta_converts(three_obs):
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[
            conn("r1", "sA", "chart0", 100),
            conn("r2a", "sA", "mid", 40),
            conn("r2b", "mid", "chart0", 60),  # 两条路线都是 100
        ],
    ))
    item = by_id(chart_tides())["a1"]
    assert item["status"] == "ok"
    assert item["tide_level"] == pytest.approx(1.1)
    assert item["connection_ids"] in (["r1"], ["r2a", "r2b"])


def test_reverse_duplicate_consistently_signed_is_not_conflict(three_obs):
    # 同一条边反向重复登记、数值互为相反数：路线高差一致，可换算
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[
            conn("c1", "sA", "chart0", 80),
            conn("c2", "chart0", "sA", -80),
        ],
    ))
    item = by_id(chart_tides())["a1"]
    assert item["status"] == "ok"
    assert item["tide_level"] == pytest.approx(1.08)


def test_two_routes_through_both_endpoints_conflict_even_with_one_sided_loop(three_obs):
    # chart0 上挂一个旁支闭环 sA-x-chart0：从标尺出发有两条简单路线
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[
            conn("direct", "sA", "chart0", 50),
            conn("h1", "sA", "x", 10),
            conn("h2", "x", "chart0", 90),
        ],
    ))
    item = by_id(chart_tides())["a1"]
    assert item["status"] == "pending_resurvey"
    assert {r["delta_mm"] for r in item["routes"]} == {50, 100}


# ---------------------------------------------------------------- 旁支闭环

def test_dangling_loop_on_single_datum_does_not_affect_conversion(three_obs):
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[
            conn("main", "sA", "chart0", 300),
            # chart0 上的旁支闭环：只接在海图零点这一个基准点上
            conn("loop1", "chart0", "spur", 20),
            conn("loop2", "spur", "chart0", -20),
        ],
    ))
    item = by_id(chart_tides())["a1"]
    assert item["status"] == "ok"
    assert item["tide_level"] == pytest.approx(1.3)
    assert item["connection_ids"] == ["main"]


def test_dangling_loop_at_staff_end_does_not_affect_conversion(three_obs):
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[
            conn("main", "sA", "chart0", 300),
            conn("loop1", "sA", "spur", 20),
            conn("loop2", "spur", "sA", -20),
        ],
    ))
    item = by_id(chart_tides())["a1"]
    assert item["status"] == "ok"
    assert item["tide_level"] == pytest.approx(1.3)


def test_self_loop_connection_is_ignored(three_obs):
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[
            conn("main", "sA", "chart0", 300),
            conn("self", "chart0", "chart0", 999),
        ],
    ))
    item = by_id(chart_tides())["a1"]
    assert item["status"] == "ok"
    assert item["tide_level"] == pytest.approx(1.3)


# ---------------------------------------------------------------- 历史观测与原子性

def test_replacing_survey_reconverts_historical_observations(three_obs):
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[conn("c", "sA", "chart0", 100)],
    ))
    assert by_id(chart_tides())["a1"]["tide_level"] == pytest.approx(1.1)

    # 替换资料后，同一条历史观测按新资料换算
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[conn("c", "sA", "chart0", 500)],
    ))
    assert by_id(chart_tides())["a1"]["tide_level"] == pytest.approx(1.5)

    # 再替换为另一套标尺基准名，历史观测随之待联测
    put_survey(survey(staffs=[{"boot_segment": "A", "staff": "other"}]))
    assert by_id(chart_tides())["a1"]["status"] == "pending_survey"


def test_original_tide_levels_endpoint_is_unchanged(three_obs):
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "sA"}],
        connections=[conn("c", "sA", "chart0", 800)],
    ))
    r = client.get("/tide-levels", params={"start_ms": 0, "end_ms": 10**13})
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["obs_id"] for i in items] == ["a1", "a2", "a3", "b1"]
    assert [i["tide_level"] for i in items] == [1.0, 2.0, 3.0, 4.0]


def test_chart_tides_only_include_calibrated_and_conflict_free():
    post_cal([
        {"boot_segment": "A", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "B", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "C", "counter": 0, "utc_ms": 5000},
    ])
    post_obs([
        {"obs_id": "a", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
        {"obs_id": "b", "boot_segment": "B", "counter": 0, "tide_level": 2.0},
        {"obs_id": "c", "boot_segment": "C", "counter": 0, "tide_level": 3.0},
        {"obs_id": "p", "boot_segment": "C", "counter": 9, "tide_level": 4.0},
    ])
    put_survey(survey(staffs=[
        {"boot_segment": "A", "staff": "s"},
        {"boot_segment": "B", "staff": "s"},
        {"boot_segment": "C", "staff": "s"},
    ], connections=[conn("c", "s", "chart0", 10)]))
    ids = [i["obs_id"] for i in chart_tides()["items"]]
    assert ids == ["c"]  # a/b 冲突，p 待校时


def test_chart_tide_range_and_inverted_range():
    post_cal([{"boot_segment": "A", "counter": 0, "utc_ms": 1000}])
    post_obs([{"obs_id": "x", "boot_segment": "A", "counter": 0, "tide_level": 1.0}])
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "s"}],
        connections=[conn("c", "s", "chart0", 100)],
    ))
    assert chart_tides(1000, 1000)["count"] == 1
    assert chart_tides(1001, 2000)["count"] == 0
    r = client.get("/chart-tide-levels", params={"start_ms": 2, "end_ms": 1})
    assert r.status_code == 422


def test_survey_persists_across_reconnect(tmp_path, monkeypatch):
    path = str(tmp_path / "persist.db")
    monkeypatch.setenv("BUOY_DB", path)
    db.reset_for_tests()

    post_cal([{"boot_segment": "A", "counter": 0, "utc_ms": 1000}])
    post_obs([{"obs_id": "x", "boot_segment": "A", "counter": 0, "tide_level": 2.0}])
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "s"}],
        connections=[conn("c", "s", "chart0", 150)],
    ))

    # 模拟进程重启：丢弃连接后重新打开同一数据库文件
    db.reset_for_tests()
    item = by_id(chart_tides())["x"]
    assert item["status"] == "ok"
    assert item["tide_level"] == pytest.approx(2.15)
    assert item["connection_ids"] == ["c"]
    assert client.get("/survey").json()["connection_count"] == 1


def test_openapi_documents_new_endpoints():
    paths = client.get("/openapi.json").json()["paths"]
    assert "/survey" in paths and "/chart-tide-levels" in paths
    assert "put" in paths["/survey"] and "get" in paths["/survey"]


def test_failed_write_rolls_back_and_keeps_old_survey():
    put_survey(survey(
        staffs=[{"boot_segment": "A", "staff": "S"}],
        connections=[conn("c1", "S", "chart0", 10)],
    ))
    conn_ = db.get_conn()
    with db.lock():
        conn_.execute(
            "CREATE TRIGGER boom BEFORE INSERT ON survey_staff "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
    try:
        r = client.put("/survey", json=survey(
            chart="OTHER",
            staffs=[{"boot_segment": "B", "staff": "T"}],
            connections=[conn("c2", "T", "OTHER", 20)],
        ))
        assert r.status_code == 500
        assert "旧资料照常供数" in r.json()["detail"]["message"]
    finally:
        with db.lock():
            conn_.execute("DROP TRIGGER boom")
    got = client.get("/survey").json()
    assert got["chart_datum"] == "chart0"
    assert [c["connection_id"] for c in got["connections"]] == ["c1"]
