"""单浮标潮位校时 API。

浮标重启会使毫秒计数归零，本服务把「开机段 + 段内计数」换算成可信的
UTC 毫秒时间线供潮位分析：

  POST /observations   分批提交观测（ID、开机段、计数、潮位）
  POST /calibrations   分批提交校时点（开机段、计数、UTC 毫秒）
  PUT  /survey         整批替换联测资料（海图零点、各段标尺、联测记录）
  GET  /survey         查看当前联测资料
  GET  /tide-levels    按 UTC 毫秒范围查可用原潮位
  GET  /chart-tide-levels  按 UTC 毫秒范围查换算到海图零点的潮位
  GET  /observations/{obs_id}  按观测 ID 查状态与原因
  GET  /docs           Swagger UI
"""

from __future__ import annotations

import sqlite3

from fastapi import FastAPI, HTTPException, Query
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from . import core, db, survey

app = FastAPI(
    title="单浮标潮位校时服务",
    description=__doc__,
    version="1.0.0",
)


# ---------------------------------------------------------------- 模型

class ObservationIn(BaseModel):
    obs_id: str = Field(min_length=1, description="观测 ID")
    boot_segment: str = Field(min_length=1, description="开机段")
    counter: int = Field(ge=0, description="段内毫秒计数")
    tide_level: float = Field(description="潮位")


class ObservationBatch(BaseModel):
    observations: list[ObservationIn] = Field(min_length=1)


class CalibrationIn(BaseModel):
    boot_segment: str = Field(min_length=1, description="开机段")
    counter: int = Field(ge=0, description="段内毫秒计数")
    utc_ms: int = Field(ge=0, description="对应的 UTC 毫秒")


class CalibrationBatch(BaseModel):
    calibrations: list[CalibrationIn] = Field(min_length=1)


class ChangedState(BaseModel):
    obs_id: str
    utc_ms: int | None
    status: str


class BatchResult(BaseModel):
    changed_count: int = Field(description="本次提交中时刻或状态变化的观测数")
    changed: list[ChangedState] = Field(description="时刻或状态变化的观测")


class TidePoint(BaseModel):
    obs_id: str
    utc_ms: int
    tide_level: float


class TideQueryResult(BaseModel):
    count: int
    items: list[TidePoint]


class CalibrationEndpoint(BaseModel):
    counter: int
    utc_ms: int


class CalibrationBasis(BaseModel):
    lo: CalibrationEndpoint = Field(description="下界校时点")
    hi: CalibrationEndpoint = Field(description="上界校时点")


class ObservationDetail(BaseModel):
    obs_id: str
    boot_segment: str
    counter: int
    tide_level: float
    utc_ms: int | None
    status: str = Field(description="ok / pending_calibration / conflict")
    reason: str | None
    calibration: CalibrationBasis | None = Field(description="校时依据；待校时为 null")


# ---------------------------------------------------------------- 联测资料模型

class StaffIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    boot_segment: str = Field(
        min_length=1,
        validation_alias=AliasChoices("boot_segment", "segment"),
        description="开机段",
    )
    staff: str = Field(
        min_length=1,
        validation_alias=AliasChoices(
            "staff", "staff_name", "datum", "benchmark", "staff_datum"
        ),
        description="该段所用标尺（基准名）",
    )


class ConnectionIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    connection_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices(
            "connection_id", "id", "survey_id", "record_id"
        ),
        serialization_alias="connection_id",
        description="联测记录唯一编号",
    )
    from_datum: str = Field(
        min_length=1,
        validation_alias=AliasChoices(
            "from_datum", "start_datum", "from", "from_benchmark", "start"
        ),
        serialization_alias="from_datum",
        description="起点基准",
    )
    to_datum: str = Field(
        min_length=1,
        validation_alias=AliasChoices(
            "to_datum", "end_datum", "to", "to_benchmark", "end", "target"
        ),
        serialization_alias="to_datum",
        description="终点基准",
    )
    delta_mm: int = Field(
        validation_alias=AliasChoices(
            "delta_mm", "offset_mm", "add_mm", "delta", "correction_mm", "mm"
        ),
        serialization_alias="delta_mm",
        description="从起点换到终点要加的毫米数（整数，反向使用时取反）",
    )


class SurveyIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    chart_datum: str = Field(
        min_length=1,
        validation_alias=AliasChoices(
            "chart_datum", "chart_datum_name", "datum", "chart_zero", "zero_datum"
        ),
        description="海图零点基准名",
    )
    staffs: list[StaffIn] = Field(
        default_factory=list,
        validation_alias=AliasChoices("staffs", "staff", "segments"),
        description="各开机段所用标尺",
    )
    connections: list[ConnectionIn] = Field(
        default_factory=list,
        validation_alias=AliasChoices("connections", "surveys", "records"),
        description="联测记录，最多 16 条",
    )

    @model_validator(mode="before")
    @classmethod
    def _staffs_from_mapping(cls, data):
        """兼容 {"A": "s1"} 形式的标尺登记。"""
        if isinstance(data, dict):
            staffs = data.get("staffs", data.get("staff"))
            if isinstance(staffs, dict):
                data = dict(data)
                data["staffs"] = [
                    {"boot_segment": seg, "staff": staff}
                    for seg, staff in staffs.items()
                ]
        return data

    @model_validator(mode="after")
    def _check_unique(self):
        if len(self.connections) > 16:
            raise ValueError("联测记录最多 16 条")
        ids = [c.connection_id for c in self.connections]
        if len(set(ids)) != len(ids):
            raise ValueError("联测记录编号必须唯一")
        segs = [s.boot_segment for s in self.staffs]
        if len(set(segs)) != len(segs):
            raise ValueError("同一开机段只能登记一个标尺")
        return self


class StaffOut(BaseModel):
    boot_segment: str
    staff: str


class ConnectionOut(BaseModel):
    connection_id: str
    from_datum: str
    to_datum: str
    delta_mm: int


class SurveyOut(BaseModel):
    chart_datum: str | None
    staffs: list[StaffOut]
    connections: list[ConnectionOut]
    staff_count: int
    connection_count: int


class RouteOut(BaseModel):
    connection_ids: list[str] = Field(description="沿途联测编号")
    delta_mm: int = Field(description="该路线累计高差（毫米）")


class ChartTidePoint(BaseModel):
    obs_id: str
    utc_ms: int
    status: str = Field(description="ok / pending_survey / pending_resurvey")
    status_text: str = Field(description="可换算 / 待联测 / 待复测")
    reason: str | None = Field(description="待联测/待复测时的原因")
    tide_level: float | None = Field(description="换算到海图零点的潮位（米）")
    chart_tide_level: float | None = Field(description="同 tide_level")
    original_tide_level: float = Field(description="未换算的原潮位（米）")
    delta_mm: int | None = Field(description="换算所用路线累计高差（毫米）")
    connection_ids: list[str] = Field(description="换算所用沿途联测编号")
    routes: list[RouteOut] = Field(description="待复测时给出的两条不同路线")


class ChartTideQueryResult(BaseModel):
    count: int
    chart_datum: str | None
    items: list[ChartTidePoint]


# ---------------------------------------------------------------- 提交

@app.post("/observations", response_model=BatchResult, summary="分批提交观测")
def submit_observations(batch: ObservationBatch) -> BatchResult:
    """写入观测原文并按当前校时点换算；同一 obs_id 重复提交会覆盖原文并重算。

    响应列出本次提交中时刻或状态发生变化的观测 ID（含新观测，
    以及因新观测落入同一 UTC 毫秒而被卷入/解除冲突的观测）。
    """
    conn = db.get_conn()
    with db.lock():
        with db.transaction(conn):
            ids = core.upsert_observations(
                conn, [o.model_dump() for o in batch.observations]
            )
            changed = core.fetch_states(conn, ids)
    return BatchResult(changed_count=len(changed), changed=changed)


@app.post("/calibrations", response_model=BatchResult, summary="分批提交校时点")
def submit_calibrations(batch: CalibrationBatch) -> BatchResult:
    """写入校时点；同段同计数的新校时点替换旧值，并重算受影响观测。

    若合并后任一开机段内 UTC 随计数倒退，则整批拒绝（409），旧数据照常供数。
    响应列出本次提交中时刻或状态发生变化的观测 ID。
    """
    conn = db.get_conn()
    with db.lock():
        with db.transaction(conn):
            try:
                ids = core.upsert_calibrations(
                    conn, [c.model_dump() for c in batch.calibrations]
                )
            except core.MonotonicViolation as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "校时点使段内 UTC 倒退，整批拒绝，旧数据照常供数",
                        "violations": exc.violations,
                    },
                )
            changed = core.fetch_states(conn, ids)
    return BatchResult(changed_count=len(changed), changed=changed)


# ---------------------------------------------------------------- 联测资料

@app.put("/survey", response_model=SurveyOut, summary="整批替换联测资料")
def replace_survey(payload: SurveyIn) -> SurveyOut:
    """一次提交海图零点、各开机段标尺与联测记录，整体替换旧资料。

    校验失败（如重复编号、超过 16 条）返回 422，写入失败整批回滚，
    旧资料继续供数；资料与观测分属不同提交状态，任一次查询只读取
    同一已提交版本的观测与联测资料。替换后历史观测也按新资料换算。
    """
    conn = db.get_conn()
    with db.lock():
        try:
            with db.transaction(conn):
                snapshot = survey.replace_survey(conn, payload.model_dump())
        except sqlite3.Error as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "联测资料替换失败，整批回滚，旧资料照常供数",
                    "error": str(exc),
                },
            )
    body = survey.describe(snapshot)
    return SurveyOut(
        **body,
        staff_count=len(body["staffs"]),
        connection_count=len(body["connections"]),
    )


@app.post(
    "/survey",
    response_model=SurveyOut,
    summary="整批替换联测资料（与 PUT /survey 等价）",
    include_in_schema=False,
)
def replace_survey_post(payload: SurveyIn) -> SurveyOut:
    return replace_survey(payload)


@app.get("/survey", response_model=SurveyOut, summary="查看当前联测资料")
def get_survey() -> SurveyOut:
    conn = db.get_conn()
    with db.lock():
        with db.read_transaction(conn):
            snapshot = survey.load_survey(conn)
    body = survey.describe(snapshot)
    return SurveyOut(
        **body,
        staff_count=len(body["staffs"]),
        connection_count=len(body["connections"]),
    )


@app.get(
    "/chart-tide-levels",
    response_model=ChartTideQueryResult,
    summary="按 UTC 范围查换算到海图零点的潮位",
)
def chart_tide_levels(
    start_ms: int = Query(ge=0, description="起始 UTC 毫秒（含）"),
    end_ms: int = Query(ge=0, description="结束 UTC 毫秒（含）"),
) -> ChartTideQueryResult:
    """列出已校时且无冲突的观测，逐条换算到海图零点。

    可换算时返回换算潮位（原潮位加上路线累计高差）与沿途联测编号；
    未登记标尺或无路线时标为 pending_survey，两条简单路线高差不同时
    标为 pending_resurvey 并给出两条路线的联测编号和高差。
    """
    if end_ms < start_ms:
        raise HTTPException(status_code=422, detail="end_ms 不得小于 start_ms")
    conn = db.get_conn()
    with db.lock():
        with db.read_transaction(conn):
            body = survey.chart_tide_items(conn, start_ms, end_ms)
    return ChartTideQueryResult(**body)


@app.get(
    "/survey/chart-tide-levels",
    response_model=ChartTideQueryResult,
    summary="按 UTC 范围查换算到海图零点的潮位（同 /chart-tide-levels）",
    include_in_schema=False,
)
def chart_tide_levels_alias(
    start_ms: int = Query(ge=0, description="起始 UTC 毫秒（含）"),
    end_ms: int = Query(ge=0, description="结束 UTC 毫秒（含）"),
) -> ChartTideQueryResult:
    return chart_tide_levels(start_ms, end_ms)


# ---------------------------------------------------------------- 查询

@app.get("/tide-levels", response_model=TideQueryResult, summary="按 UTC 范围查可用潮位")
def tide_levels(
    start_ms: int = Query(ge=0, description="起始 UTC 毫秒（含）"),
    end_ms: int = Query(ge=0, description="结束 UTC 毫秒（含）"),
) -> TideQueryResult:
    """只返回已校时且无冲突（status=ok）的观测，按 UTC 毫秒升序。"""
    if end_ms < start_ms:
        raise HTTPException(status_code=422, detail="end_ms 不得小于 start_ms")
    conn = db.get_conn()
    with db.lock():
        rows = conn.execute(
            "SELECT obs_id, utc_ms, tide_level FROM observations "
            "WHERE status = ? AND utc_ms BETWEEN ? AND ? "
            "ORDER BY utc_ms, obs_id",
            (core.OK, start_ms, end_ms),
        ).fetchall()
    items = [
        TidePoint(obs_id=r["obs_id"], utc_ms=r["utc_ms"], tide_level=r["tide_level"])
        for r in rows
    ]
    return TideQueryResult(count=len(items), items=items)


@app.get(
    "/observations/{obs_id}",
    response_model=ObservationDetail,
    summary="按观测 ID 查状态和原因",
)
def get_observation(obs_id: str) -> ObservationDetail:
    conn = db.get_conn()
    with db.lock():
        row = conn.execute(
            "SELECT * FROM observations WHERE obs_id = ?", (obs_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"观测 '{obs_id}' 不存在")
    calibration = None
    if row["cal_lo_counter"] is not None:
        calibration = CalibrationBasis(
            lo=CalibrationEndpoint(
                counter=row["cal_lo_counter"], utc_ms=row["cal_lo_utc_ms"]
            ),
            hi=CalibrationEndpoint(
                counter=row["cal_hi_counter"], utc_ms=row["cal_hi_utc_ms"]
            ),
        )
    return ObservationDetail(
        obs_id=row["obs_id"],
        boot_segment=row["boot_segment"],
        counter=row["counter"],
        tide_level=row["tide_level"],
        utc_ms=row["utc_ms"],
        status=row["status"],
        reason=row["reason"],
        calibration=calibration,
    )


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "up"}
