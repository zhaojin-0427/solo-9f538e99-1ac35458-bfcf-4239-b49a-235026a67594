"""联测资料（标尺基准网）与海图零点换算。

联测资料由三部分组成，作为**一个整体**整批替换：

  * 海图零点基准名（换算目标）
  * 各开机段所用标尺（基准名）
  * 联测记录：唯一编号、起点基准、终点基准、
    从起点换到终点要加的毫米数（反向使用时取反）

换算时在「基准名」构成的无向多重图上寻找标尺到海图零点的路线，
把沿途累计高差（毫米）加到以米计的原潮位上。

路线必须**不重复经过基准点**。因此只挂在一个基准点上的旁支闭环
不会产生第二条简单路线，也就不影响换算；而连接在两个基准点之间的
闭环会给出两条不同的简单路线，若两者高差不同，则该段标尺无法确定
唯一换算值，观测标为待复测。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

OK = "ok"
PENDING_SURVEY = "pending_survey"
PENDING_RESURVEY = "pending_resurvey"

_STATUS_TEXT = {
    OK: "可换算",
    PENDING_SURVEY: "待联测",
    PENDING_RESURVEY: "待复测",
}

# 简单路线枚举上限。联测记录最多 16 条，正常规模远低于此值；
# 上限只用于防御异常输入导致的指数级枚举。
MAX_ROUTES = 20000


# ---------------------------------------------------------------- 资料读取

@dataclass(frozen=True)
class Connection:
    seq: int
    connection_id: str
    from_datum: str
    to_datum: str
    delta_mm: int


@dataclass(frozen=True)
class SurveySnapshot:
    chart_datum: str | None
    staffs: dict[str, str]
    connections: tuple[Connection, ...]


def load_survey(conn: sqlite3.Connection) -> SurveySnapshot:
    """读取当前已提交的联测资料。调用方应处于同一事务/快照中。"""
    meta = conn.execute(
        "SELECT chart_datum FROM survey_meta WHERE id = 1"
    ).fetchone()
    chart_datum = meta["chart_datum"] if meta is not None else None

    staffs = {
        r["boot_segment"]: r["staff"]
        for r in conn.execute(
            "SELECT boot_segment, staff FROM survey_staff ORDER BY boot_segment"
        )
    }
    connections = tuple(
        Connection(
            seq=r["seq"],
            connection_id=r["connection_id"],
            from_datum=r["from_datum"],
            to_datum=r["to_datum"],
            delta_mm=r["delta_mm"],
        )
        for r in conn.execute(
            "SELECT seq, connection_id, from_datum, to_datum, delta_mm "
            "FROM survey_connections ORDER BY seq"
        )
    )
    return SurveySnapshot(
        chart_datum=chart_datum,
        staffs=staffs,
        connections=connections,
    )


def replace_survey(conn: sqlite3.Connection, payload: dict) -> SurveySnapshot:
    """整批替换联测资料（调用方负责事务；异常即回滚、旧资料保留）。"""
    conn.execute("DELETE FROM survey_connections")
    conn.execute("DELETE FROM survey_staff")
    conn.execute("DELETE FROM survey_meta")

    conn.execute(
        "INSERT INTO survey_meta (id, chart_datum) VALUES (1, ?)",
        (payload["chart_datum"],),
    )
    conn.executemany(
        "INSERT INTO survey_staff (boot_segment, staff) VALUES (?, ?)",
        [(s["boot_segment"], s["staff"]) for s in payload["staffs"]],
    )
    conn.executemany(
        "INSERT INTO survey_connections "
        "(seq, connection_id, from_datum, to_datum, delta_mm) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (i, c["connection_id"], c["from_datum"], c["to_datum"], c["delta_mm"])
            for i, c in enumerate(payload["connections"])
        ],
    )
    return load_survey(conn)


# ---------------------------------------------------------------- 基准网

def _adjacency(connections: Iterable[Connection]) -> dict[str, list[tuple[str, str, int]]]:
    """无向多重图：(相邻基准, 联测编号, 沿该方向的高差毫米)。

    反向使用联测记录时高差取反。
    """
    adj: dict[str, list[tuple[str, str, int]]] = {}
    for c in connections:
        adj.setdefault(c.from_datum, []).append(
            (c.to_datum, c.connection_id, c.delta_mm)
        )
        adj.setdefault(c.to_datum, []).append(
            (c.from_datum, c.connection_id, -c.delta_mm)
        )
    for edges in adj.values():
        # 输出顺序只影响「多条同高差路线时选哪条」的确定性
        edges.sort(key=lambda e: (e[1], e[0], e[2]))
    return adj


@dataclass(frozen=True)
class Route:
    connection_ids: tuple[str, ...]
    delta_mm: int


def _simple_routes(
    adj: dict[str, list[tuple[str, str, int]]], start: str, target: str
) -> list[Route]:
    """枚举 start -> target 的所有不重复经过基准点的路线。

    start == target 时唯一合法路线是空路线（高差 0）；自环或旁支闭环
    因会重复基准点而被排除。
    """
    if start == target:
        return [Route((), 0)]

    routes: list[Route] = []
    visited = {start}
    path: list[tuple[str, str, int]] = []

    def dfs(node: str, total: int) -> None:
        if len(routes) >= MAX_ROUTES:
            return
        for nxt, cid, delta in adj.get(node, ()):
            if nxt in visited:
                continue
            path.append((nxt, cid, delta))
            if nxt == target:
                routes.append(
                    Route(tuple(p[1] for p in path), total + delta)
                )
            else:
                visited.add(nxt)
                dfs(nxt, total + delta)
                visited.remove(nxt)
            path.pop()

    dfs(start, 0)
    return routes


# ---------------------------------------------------------------- 换算判断

@dataclass(frozen=True)
class Conversion:
    status: str
    reason: str | None = None
    route: Route | None = None
    # 待复测时给出两条高差不同的路线
    routes: tuple[Route, ...] = ()


def _route_sort_key(route: Route) -> tuple:
    return (len(route.connection_ids), route.connection_ids, route.delta_mm)


def evaluate_segment(
    segment: str, snapshot: SurveySnapshot, adj: dict[str, list[tuple[str, str, int]]]
) -> Conversion:
    """判断某开机段的标尺能否唯一换算到海图零点。"""
    staff = snapshot.staffs.get(segment)
    chart = snapshot.chart_datum

    if not chart:
        return Conversion(PENDING_SURVEY, "尚未提交联测资料，待联测")
    if not staff:
        return Conversion(
            PENDING_SURVEY, f"开机段 '{segment}' 未登记标尺，待联测"
        )

    routes = _simple_routes(adj, staff, chart)
    if not routes:
        return Conversion(
            PENDING_SURVEY,
            f"标尺 '{staff}' 到海图零点 '{chart}' 无联测路线，待联测",
        )

    if len({r.delta_mm for r in routes}) > 1:
        ordered = sorted(routes, key=_route_sort_key)
        first = ordered[0]
        second = next(r for r in ordered if r.delta_mm != first.delta_mm)
        return Conversion(
            PENDING_RESURVEY,
            (
                f"标尺 '{staff}' 到海图零点 '{chart}' 有两条高差不同的路线"
                f"（{first.delta_mm} mm 与 {second.delta_mm} mm），待复测"
            ),
            None,
            (first, second),
        )

    return Conversion(OK, None, min(routes, key=_route_sort_key))


def chart_tide_level(raw_tide_level: float, delta_mm: int) -> float:
    """原潮位（米）加路线累计高差（毫米）。用十进制运算避免浮点尾差。"""
    return float(
        Decimal(str(raw_tide_level)) + Decimal(delta_mm) / Decimal(1000)
    )


def route_payload(route: Route) -> dict:
    return {
        "connection_ids": list(route.connection_ids),
        "delta_mm": route.delta_mm,
    }


# ---------------------------------------------------------------- 查询

def chart_tide_items(
    conn: sqlite3.Connection, start_ms: int, end_ms: int
) -> dict:
    """按 UTC 范围列出可用观测并逐条做海图零点换算。

    调用方需持有 db.lock() 并在同一只读事务（同一已提交快照）中调用，
    使观测与联测资料来自同一版本。历史观测同样按当前资料换算。
    """
    snapshot = load_survey(conn)
    adj = _adjacency(snapshot.connections)
    conversions: dict[str, Conversion] = {}

    rows = conn.execute(
        "SELECT obs_id, boot_segment, utc_ms, tide_level FROM observations "
        "WHERE status = ? AND utc_ms BETWEEN ? AND ? "
        "ORDER BY utc_ms, obs_id",
        ("ok", start_ms, end_ms),
    ).fetchall()

    items = []
    for row in rows:
        segment = row["boot_segment"]
        if segment not in conversions:
            conversions[segment] = evaluate_segment(segment, snapshot, adj)
        conv = conversions[segment]

        item = {
            "obs_id": row["obs_id"],
            "utc_ms": row["utc_ms"],
            "status": conv.status,
            "status_text": _STATUS_TEXT[conv.status],
            "reason": conv.reason,
            "tide_level": None,
            "chart_tide_level": None,
            "original_tide_level": row["tide_level"],
            "delta_mm": None,
            "connection_ids": [],
            "routes": [],
        }
        if conv.status == OK and conv.route is not None:
            converted = chart_tide_level(row["tide_level"], conv.route.delta_mm)
            item["tide_level"] = converted
            item["chart_tide_level"] = converted
            item["original_tide_level"] = row["tide_level"]
            item["delta_mm"] = conv.route.delta_mm
            item["connection_ids"] = list(conv.route.connection_ids)
        elif conv.routes:
            item["routes"] = [route_payload(r) for r in conv.routes]
        items.append(item)

    return {
        "count": len(items),
        "chart_datum": snapshot.chart_datum,
        "items": items,
    }


def describe(snapshot: SurveySnapshot) -> dict:
    return {
        "chart_datum": snapshot.chart_datum,
        "staffs": [
            {"boot_segment": seg, "staff": staff}
            for seg, staff in sorted(snapshot.staffs.items())
        ],
        "connections": [
            {
                "connection_id": c.connection_id,
                "from_datum": c.from_datum,
                "to_datum": c.to_datum,
                "delta_mm": c.delta_mm,
            }
            for c in snapshot.connections
        ],
    }

