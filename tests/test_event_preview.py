"""事件上报预演与确认（POST /events/preview、POST /events/confirm）端到端测试：
需要可用的 PostgreSQL（在 Compose 环境内运行）。

运行方式：docker compose exec app pytest tests/test_event_preview.py
"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "manual_adjudications",
    "shift_results",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}
SHIFT = {  # 早班 08:00-16:00
    "device_id": "CNC-01",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T16:00:00+08:00",
}
SHIFT_B = {  # 晚班 16:00-24:00（端点相接，不重叠）
    "device_id": "CNC-01",
    "start": "2026-10-01T16:00:00+08:00",
    "end": "2026-10-02T00:00:00+08:00",
}


def ev(source, version, start, end, reason):
    return {
        "device_id": "CNC-01",
        "source_id": source,
        "version": version,
        "start": start,
        "end": end,
        "reason": reason,
    }


# 保养 10:00-11:00（MES-1 v1）；故障 10:30-12:00（PLC-9 v1）
E_MAINT_V1 = ev("MES-1", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "maintenance")
E_FAIL_V1 = ev("PLC-9", 1, "2026-10-01T10:30:00+08:00", "2026-10-01T12:00:00+08:00", "failure")


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _make_shift(client, shift=SHIFT) -> int:
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = client.post("/shifts", json=shift)
    assert r.status_code == 201
    return r.json()["id"]


def _settled_main_shift(client) -> int:
    """班次 + 保养/故障各一条 + 结算：当前 m60 f60 p0（同 README 主流程）。"""
    shift_id = _make_shift(client)
    assert client.post("/events", json=E_MAINT_V1).status_code == 201
    assert client.post("/events", json=E_FAIL_V1).status_code == 201
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.json()["result"]["totals"] == {"maintenance": 60, "failure": 60, "pending": 0}
    return shift_id


def _preview(client, payload):
    return client.post("/events/preview", json=payload)


def _confirm(client, token):
    return client.post("/events/confirm", json={"confirm_token": token})


def _revisions(client, shift_id):
    return client.get(f"/shifts/{shift_id}/revisions").json()


def _changes(client):
    return client.get("/changes", params={"cursor": 0, "limit": 500}).json()["changes"]


# ---------------------------------------------------------------- 基本流程


def test_preview_then_confirm_commits_like_single_ingest(client):
    """预演给出影响清单与确认凭据；确认后效果与单条上报一致（修订 + correction 变更）。"""
    shift_id = _settled_main_shift(client)

    # 预演：故障缩短为 10:30-11:30 → 现有 f60、拟变 f30、差额 -30
    r = _preview(client, ev("PLC-9", 2, "2026-10-01T10:30:00+08:00", "2026-10-01T11:30:00+08:00", "failure"))
    assert r.status_code == 200
    body = r.json()
    assert body["device_id"] == "CNC-01" and body["source_id"] == "PLC-9" and body["version"] == 2
    assert body["idempotent"] is False
    assert len(body["impacts"]) == 1
    imp = body["impacts"][0]
    assert imp["shift_id"] == shift_id
    assert imp["current"] == {"maintenance": 60, "failure": 60, "pending": 0}
    assert imp["proposed"] == {"maintenance": 60, "failure": 30, "pending": 0}
    assert imp["delta"] == {"maintenance": 0, "failure": -30, "pending": 0}
    token = body["confirm_token"]
    assert token

    # 预演只读：无新修订、无新变更、事件未落库
    assert [x["revision"] for x in _revisions(client, shift_id)] == [1]
    assert len(_changes(client)) == 1
    again = _preview(client, ev("PLC-9", 2, "2026-10-01T10:30:00+08:00", "2026-10-01T11:30:00+08:00", "failure"))
    assert again.status_code == 200 and again.json()["idempotent"] is False

    # 确认：原子提交，回执与单条上报一致
    r = _confirm(client, token)
    assert r.status_code == 201
    body = r.json()
    assert body["idempotent"] is False
    assert body["source_id"] == "PLC-9" and body["version"] == 2
    assert len(body["recomputed"]) == 1
    rec = body["recomputed"][0]
    assert rec["shift_id"] == shift_id and rec["revision"] == 2
    assert rec["totals"] == {"maintenance": 60, "failure": 30, "pending": 0}
    assert rec["delta"] == {"maintenance": 0, "failure": -30, "pending": 0}

    # 与单条上报相同的副作用：修订 2 + 一条 correction 变更
    assert [x["revision"] for x in _revisions(client, shift_id)] == [1, 2]
    changes = _changes(client)
    assert len(changes) == 2
    assert changes[-1]["trigger"] == "correction" and changes[-1]["revision"] == 2

    # 相同确认重试：幂等 200，不再产生任何数据
    r = _confirm(client, token)
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["recomputed"] == []
    assert [x["revision"] for x in _revisions(client, shift_id)] == [1, 2]
    assert len(_changes(client)) == 2


def test_preview_lists_unchanged_shifts_and_confirm_writes_no_revision(client):
    """结果不变的班次也列出（差额为 0）；确认时分钟数未变则不产生修订与变更。"""
    shift_id = _make_shift(client)
    assert client.post("/events", json=E_MAINT_V1).status_code == 201
    client.post(f"/shifts/{shift_id}/settle")

    # 候选 v2 与 v1 内容相同：班次仍列出，差额全 0
    r = _preview(client, dict(E_MAINT_V1, version=2))
    assert r.status_code == 200
    body = r.json()
    assert body["idempotent"] is False
    assert len(body["impacts"]) == 1
    imp = body["impacts"][0]
    assert imp["current"] == imp["proposed"] == {"maintenance": 60, "failure": 0, "pending": 0}
    assert imp["delta"] == {"maintenance": 0, "failure": 0, "pending": 0}

    r = _confirm(client, body["confirm_token"])
    assert r.status_code == 201
    assert r.json()["recomputed"] == []
    assert [x["revision"] for x in _revisions(client, shift_id)] == [1]
    assert len(_changes(client)) == 1


def test_preview_idempotent_for_existing_version(client):
    """同来源同版本同内容：预演幂等标示、无影响班次；确认同样幂等返回。"""
    shift_id = _settled_main_shift(client)

    r = _preview(client, E_MAINT_V1)
    assert r.status_code == 200
    body = r.json()
    assert body["idempotent"] is True
    assert body["impacts"] == []
    assert body["confirm_token"]

    r = _confirm(client, body["confirm_token"])
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["recomputed"] == []
    assert [x["revision"] for x in _revisions(client, shift_id)] == [1]


def test_preview_multi_shift_union_of_old_and_candidate(client):
    """相关班次 = 旧版 ∪ 候选版区间并集：跨班次两侧都列出、确认后两侧都重算。"""
    assert client.post("/devices", json=DEVICE).status_code == 201
    ids = []
    for s in (SHIFT, SHIFT_B):
        r = client.post("/shifts", json=s)
        assert r.status_code == 201
        ids.append(r.json()["id"])
    shift_a, shift_b = ids
    assert client.post(
        "/events",
        json=ev("PLC-9", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "failure"),
    ).status_code == 201
    client.post(f"/shifts/{shift_a}/settle")  # failure 60
    client.post(f"/shifts/{shift_b}/settle")  # 0

    # 候选 v2 改到 18:00-19:00（晚班）：旧版区间（早班）∪ 候选区间（晚班）
    r = _preview(client, ev("PLC-9", 2, "2026-10-01T18:00:00+08:00", "2026-10-01T19:00:00+08:00", "failure"))
    assert r.status_code == 200
    impacts = {x["shift_id"]: x for x in r.json()["impacts"]}
    assert set(impacts) == {shift_a, shift_b}
    assert impacts[shift_a]["current"]["failure"] == 60
    assert impacts[shift_a]["proposed"]["failure"] == 0
    assert impacts[shift_a]["delta"]["failure"] == -60
    assert impacts[shift_b]["current"]["failure"] == 0
    assert impacts[shift_b]["proposed"]["failure"] == 60
    assert impacts[shift_b]["delta"]["failure"] == 60

    r = _confirm(client, r.json()["confirm_token"])
    assert r.status_code == 201
    rec = {x["shift_id"]: x for x in r.json()["recomputed"]}
    assert set(rec) == {shift_a, shift_b}
    assert rec[shift_a]["totals"]["failure"] == 0
    assert rec[shift_b]["totals"]["failure"] == 60


def test_preview_respects_active_adjudications(client):
    """预演与确认均遵守生效中的现场裁定：裁定分钟不被事件改动。"""
    shift_id = _make_shift(client)
    assert client.post(
        "/events",
        json=ev("PLC-9", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "failure"),
    ).status_code == 201
    client.post(f"/shifts/{shift_id}/settle")  # failure 60
    r = client.post(
        f"/shifts/{shift_id}/adjudications",
        json={
            "ruling_no": "R-001",
            "start": "2026-10-01T10:00:00+08:00",
            "end": "2026-10-01T10:30:00+08:00",
            "reason": "maintenance",
            "note": "现场确认为计划保养",
        },
    )
    assert r.status_code == 201  # 修订 2：m30 f30

    # 预演 v2 故障延长到 12:00：裁定 10:00-10:30 仍归保养，其余按事件
    r = _preview(client, ev("PLC-9", 2, "2026-10-01T10:00:00+08:00", "2026-10-01T12:00:00+08:00", "failure"))
    assert r.status_code == 200
    imp = r.json()["impacts"][0]
    assert imp["current"] == {"maintenance": 30, "failure": 30, "pending": 0}
    assert imp["proposed"] == {"maintenance": 30, "failure": 90, "pending": 0}
    assert imp["delta"] == {"maintenance": 0, "failure": 60, "pending": 0}

    r = _confirm(client, r.json()["confirm_token"])
    assert r.status_code == 201
    rec = r.json()["recomputed"][0]
    assert rec["revision"] == 3
    assert rec["totals"] == {"maintenance": 30, "failure": 90, "pending": 0}


def test_preview_no_impact_when_no_settled_shift_overlaps(client):
    """候选区间不涉及任何已结算班次：影响清单为空，确认正常提交。"""
    shift_id = _settled_main_shift(client)
    r = _preview(client, ev("PLC-9", 2, "2026-11-01T10:00:00+08:00", "2026-11-01T11:00:00+08:00", "failure"))
    assert r.status_code == 200
    assert r.json()["impacts"] == []

    r = _confirm(client, r.json()["confirm_token"])
    assert r.status_code == 201
    assert r.json()["recomputed"] == []
    assert [x["revision"] for x in _revisions(client, shift_id)] == [1]


# ---------------------------------------------------------------- 校验与错误约定


def test_preview_version_conflict_and_gap(client):
    _make_shift(client)
    assert client.post("/events", json=E_MAINT_V1).status_code == 201

    r = _preview(client, dict(E_MAINT_V1, end="2026-10-01T11:30:00+08:00"))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_CONFLICT"

    r = _preview(client, dict(E_MAINT_V1, version=3))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_GAP"

    # 预演失败不留痕迹：v2 仍可正常上报
    assert client.post("/events", json=dict(E_MAINT_V1, version=2)).status_code == 201


def test_preview_validation_and_unknown_device(client):
    _make_shift(client)
    bad = [
        {**E_MAINT_V1, "start": "2026-10-01T10:00:00"},  # 缺时区
        {**E_MAINT_V1, "start": "2026-10-01T10:00:30+08:00"},  # 未对齐整分钟
        {**E_MAINT_V1, "reason": "unknown"},  # 原因非法
        {**E_MAINT_V1, "end": E_MAINT_V1["start"]},  # start >= end
        {**E_MAINT_V1, "version": 0},  # 版本非法
    ]
    for payload in bad:
        r = _preview(client, payload)
        assert r.status_code == 422, payload
        assert r.json()["detail"]["code"] == "VALIDATION_ERROR"

    r = _preview(client, {**E_MAINT_V1, "device_id": "NOPE"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"

    # 确认凭据为空 → 422
    r = client.post("/events/confirm", json={"confirm_token": ""})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_confirm_invalid_token(client):
    shift_id = _settled_main_shift(client)
    token = _preview(
        client, ev("PLC-9", 2, "2026-10-01T10:30:00+08:00", "2026-10-01T11:30:00+08:00", "failure")
    ).json()["confirm_token"]

    # 非凭据内容
    r = _confirm(client, "garbage")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CONFIRM_TOKEN"

    # 篡改报文段（破坏签名）
    payload_seg, mac_seg = token.split(".")
    tampered = payload_seg[:-1] + ("A" if payload_seg[-1] != "A" else "B") + "." + mac_seg
    r = _confirm(client, tampered)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CONFIRM_TOKEN"

    # 篡改签名段
    tampered = payload_seg + "." + mac_seg[:-1] + ("A" if mac_seg[-1] != "A" else "B")
    r = _confirm(client, tampered)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CONFIRM_TOKEN"

    # 与快照续页游标不通用：快照游标不能当确认凭据，确认凭据也不能当快照游标
    r = client.post("/shifts", json=SHIFT_B)
    other_shift = r.json()["id"]
    client.post(f"/shifts/{other_shift}/settle")
    snap = client.get(
        "/devices/CNC-01/shift-snapshots",
        params={
            "start": "2026-10-01T00:00:00+08:00",
            "end": "2026-10-04T00:00:00+08:00",
            "limit": 1,
        },
    ).json()
    assert snap["next_cursor"] is not None
    r = _confirm(client, snap["next_cursor"])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CONFIRM_TOKEN"
    r = client.get(
        "/devices/CNC-01/shift-snapshots",
        params={
            "start": "2026-10-01T00:00:00+08:00",
            "end": "2026-10-04T00:00:00+08:00",
            "cursor": token,
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CURSOR"

    # 以上均未写入：原凭据仍可正常确认
    r = _confirm(client, token)
    assert r.status_code == 201
    assert [x["revision"] for x in _revisions(client, shift_id)] == [1, 2]


# ---------------------------------------------------------------- 状态已变 → 冲突，须重新预演


def test_confirm_stale_after_other_source_event(client):
    """预演后其他来源上报影响同班次的事件：确认 409 PREVIEW_STALE，不落任何数据。"""
    shift_id = _make_shift(client)
    assert client.post("/events", json=E_MAINT_V1).status_code == 201
    client.post(f"/shifts/{shift_id}/settle")  # m60

    candidate = ev("PLC-9", 1, "2026-10-01T11:00:00+08:00", "2026-10-01T12:00:00+08:00", "failure")
    token = _preview(client, candidate).json()["confirm_token"]

    # 另一来源新事件改变该班次结果（产生 correction 修订）
    assert client.post(
        "/events",
        json=ev("MES-2", 1, "2026-10-01T14:00:00+08:00", "2026-10-01T15:00:00+08:00", "failure"),
    ).status_code == 201

    r = _confirm(client, token)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PREVIEW_STALE"

    # 确认未写入：候选事件不存在（可重新作为 v1 上报），无额外修订/变更
    r = client.post("/events", json=candidate)
    assert r.status_code == 201 and r.json()["idempotent"] is False
    assert [x["revision"] for x in _revisions(client, shift_id)] == [1, 2, 3]


def test_confirm_stale_after_adjudication_then_repreview(client):
    """预演后班次新增裁定：确认 409；重新预演反映最新状态后可正常确认。"""
    shift_id = _make_shift(client)
    assert client.post(
        "/events",
        json=ev("PLC-9", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "failure"),
    ).status_code == 201
    client.post(f"/shifts/{shift_id}/settle")  # f60

    candidate = ev("PLC-9", 2, "2026-10-01T10:00:00+08:00", "2026-10-01T12:00:00+08:00", "failure")
    token = _preview(client, candidate).json()["confirm_token"]

    # 裁定 10:00-10:30 为保养（改变班次结果）
    r = client.post(
        f"/shifts/{shift_id}/adjudications",
        json={
            "ruling_no": "R-001",
            "start": "2026-10-01T10:00:00+08:00",
            "end": "2026-10-01T10:30:00+08:00",
            "reason": "maintenance",
        },
    )
    assert r.status_code == 201

    r = _confirm(client, token)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PREVIEW_STALE"

    # 重新预演：现有分钟数反映裁定后的状态
    r = _preview(client, candidate)
    assert r.status_code == 200
    imp = r.json()["impacts"][0]
    assert imp["current"] == {"maintenance": 30, "failure": 30, "pending": 0}
    assert imp["proposed"] == {"maintenance": 30, "failure": 90, "pending": 0}

    r = _confirm(client, r.json()["confirm_token"])
    assert r.status_code == 201
    rec = r.json()["recomputed"][0]
    assert rec["totals"] == {"maintenance": 30, "failure": 90, "pending": 0}


def test_confirm_stale_after_new_shift_settled(client):
    """预演后相关区间内出现新结算班次：确认 409；重新预演后确认覆盖两侧班次。"""
    shift_a = _make_shift(client)
    client.post(f"/shifts/{shift_a}/settle")  # 无事件，全 0

    candidate = ev("NEW-SRC", 1, "2026-10-01T15:00:00+08:00", "2026-10-01T17:00:00+08:00", "failure")
    r = _preview(client, candidate)
    assert [x["shift_id"] for x in r.json()["impacts"]] == [shift_a]
    token = r.json()["confirm_token"]

    # 新班次 16:00-24:00 创建并结算（与候选区间相交）
    r = client.post("/shifts", json=SHIFT_B)
    shift_b = r.json()["id"]
    client.post(f"/shifts/{shift_b}/settle")

    r = _confirm(client, token)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PREVIEW_STALE"

    r = _preview(client, candidate)
    impacts = {x["shift_id"]: x for x in r.json()["impacts"]}
    assert set(impacts) == {shift_a, shift_b}
    assert impacts[shift_a]["proposed"]["failure"] == 60  # 15:00-16:00
    assert impacts[shift_b]["proposed"]["failure"] == 60  # 16:00-17:00

    r = _confirm(client, r.json()["confirm_token"])
    assert r.status_code == 201
    rec = {x["shift_id"]: x for x in r.json()["recomputed"]}
    assert set(rec) == {shift_a, shift_b}
    assert rec[shift_a]["totals"]["failure"] == 60
    assert rec[shift_b]["totals"]["failure"] == 60


def test_confirm_after_same_source_advanced(client):
    """预演后来源版本链被单条上报推进：同内容 → 幂等 200；异内容 → 409 VERSION_CONFLICT。"""
    shift_id = _settled_main_shift(client)

    # 同内容：确认幂等返回，不产生新数据
    v2 = ev("MES-1", 2, "2026-10-01T10:00:00+08:00", "2026-10-01T11:30:00+08:00", "maintenance")
    token = _preview(client, v2).json()["confirm_token"]
    assert client.post("/events", json=v2).status_code == 201
    r = _confirm(client, token)
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["recomputed"] == []

    # 异内容：确认按既有约定 409 VERSION_CONFLICT
    v3 = ev("MES-1", 3, "2026-10-01T10:00:00+08:00", "2026-10-01T12:00:00+08:00", "maintenance")
    token = _preview(client, v3).json()["confirm_token"]
    other_v3 = ev("MES-1", 3, "2026-10-01T09:30:00+08:00", "2026-10-01T12:00:00+08:00", "maintenance")
    assert client.post("/events", json=other_v3).status_code == 201
    r = _confirm(client, token)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_CONFLICT"

    # 版本链未被确认操作破坏：v4 可继续；修订流完整（结算 + 三次更正）
    assert client.post("/events", json=dict(v3, version=4)).status_code == 201
    assert [x["revision"] for x in _revisions(client, shift_id)] == [1, 2, 3, 4]


# ---------------------------------------------------------------- 并发一致性


def test_concurrent_confirms_single_winner(client):
    """并发提交同一确认凭据：行锁串行化，恰好一个创建、其余幂等，版本链连续。"""
    shift_id = _settled_main_shift(client)
    candidate = ev("PLC-9", 2, "2026-10-01T10:30:00+08:00", "2026-10-01T11:30:00+08:00", "failure")
    token = _preview(client, candidate).json()["confirm_token"]

    barrier = threading.Barrier(4)
    results: list = []
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait(timeout=10)
            r = _confirm(client, token)
            assert r.status_code in (200, 201), r.text
            results.append(r)
        except Exception as exc:  # noqa: BLE001 - 汇集到主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sum(1 for r in results if r.status_code == 201) == 1  # 恰好一个实际创建
    assert sum(1 for r in results if r.status_code == 200) == 3  # 其余幂等

    # 只产生一次修订与一条变更；版本链连续：v3 可继续
    assert [x["revision"] for x in _revisions(client, shift_id)] == [1, 2]
    assert len(_changes(client)) == 2
    r = client.post("/events", json=dict(candidate, version=3))
    assert r.status_code == 201
