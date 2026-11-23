"""换档更正自测：重映射、整次退回、换档/补交交错、并发一致性、/check 无 500。"""
from __future__ import annotations

import os
import tempfile
import threading

_tmp = tempfile.mkdtemp(prefix="shift-test-remap-")
os.environ["APP_DB_PATH"] = os.path.join(_tmp, "test-remap.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)
client.__enter__()

# B0=[10,20] B1=[21,29] B2=[30,40] B3=[41,50]，N=14
REPORT = {
    "buckets": [
        {"lo": 10, "hi": 20}, {"lo": 21, "hi": 29},
        {"lo": 30, "hi": 40}, {"lo": 41, "hi": 50},
    ],
    "terminals": [
        {"terminal_id": "T1", "bucket_idx": 0, "count": 7},
        {"terminal_id": "T2", "bucket_idx": 1, "count": 2},
        {"terminal_id": "T3", "bucket_idx": 2, "count": 2},
        {"terminal_id": "T3", "bucket_idx": 3, "count": 1},
        {"terminal_id": "T4", "bucket_idx": 3, "count": 2},
    ],
    "p50_ms": 20, "p95_ms": 41, "total_sum_ms": 370,
}

# 合法换档：B0/B1 区间对调，同时把终端声明的档号一起对调。
# T1 改报新 B1（区间 [10,20]），T2 改报新 B0（区间 [21,29]）。
REPORT_SWAPPED = {
    **REPORT,
    "buckets": [
        {"lo": 21, "hi": 29}, {"lo": 10, "hi": 20},
        {"lo": 30, "hi": 40}, {"lo": 41, "hi": 50},
    ],
    "terminals": [
        {"terminal_id": "T1", "bucket_idx": 1, "count": 7},
        {"terminal_id": "T2", "bucket_idx": 0, "count": 2},
        {"terminal_id": "T3", "bucket_idx": 2, "count": 2},
        {"terminal_id": "T3", "bucket_idx": 3, "count": 1},
        {"terminal_id": "T4", "bucket_idx": 3, "count": 2},
    ],
}


def _items(shift):
    return client.get(f"/shifts/{shift}/details").json()["items"]


def _detail_map(shift):
    return {(d["terminal_id"], d["seq"]): (d["bucket_idx"], d["value_ms"])
            for d in _items(shift)}


def _check(shift):
    r = client.post(f"/shifts/{shift}/check")
    assert r.status_code == 200, r.text
    return r.json()


def test_swap_remaps_existing_details_by_value():
    shift = "S1"
    assert client.put(f"/shifts/{shift}/report", json=REPORT).status_code == 200
    items = [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 15},
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 20},
        {"terminal_id": "T2", "seq": 1, "bucket_idx": 1, "value_ms": 25},
        {"terminal_id": "T3", "seq": 1, "bucket_idx": 2, "value_ms": 30},
        {"terminal_id": "T4", "seq": 1, "bucket_idx": 3, "value_ms": 41},
    ]
    r = client.post(f"/shifts/{shift}/details", json={"items": items})
    assert r.status_code == 200 and r.json()["stored"] == 5

    r = client.put(f"/shifts/{shift}/report", json=REPORT_SWAPPED)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replaced"] is True
    assert body["details_remapped"] == 5
    assert body["bucket_idx_changed"] == 3      # T1 两笔 + T2 一笔改档

    stored = _detail_map(shift)
    # 流水号与耗时原样保留，仅档号按实际耗时归入新档
    assert stored[("T1", 1)] == (1, 15)
    assert stored[("T1", 2)] == (1, 20)
    assert stored[("T2", 1)] == (0, 25)
    assert stored[("T3", 1)] == (2, 30)         # 未对调的档保持原号
    assert stored[("T4", 1)] == (3, 41)

    # 换档后 /check 正常（不再 500），且固定明细仍被纳入求解
    chk = _check(shift)
    assert chk["pinned_count"] == 5
    for conv in chk["conventions"]:
        assert "witness" in conv


def test_remap_rollback_when_detail_finds_no_bucket():
    shift = "S2"
    client.put(f"/shifts/{shift}/report", json=REPORT)
    client.post(f"/shifts/{shift}/details", json={"items": [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 15},
        {"terminal_id": "T2", "seq": 1, "bucket_idx": 1, "value_ms": 25},
    ]})
    before_report = client.get(f"/shifts/{shift}/details").json()

    # 新班报把 B1 区间收窄到 [22,29]：25 仍可落档，但 B0 改为 [12,20]，
    # 15 仍在；改为让 15 无处可去：B0=[16,20] 且其余区间不含 15
    bad = {
        **REPORT_SWAPPED,
        "buckets": [
            {"lo": 21, "hi": 29}, {"lo": 16, "hi": 20},
            {"lo": 30, "hi": 40}, {"lo": 41, "hi": 50},
        ],
    }
    r = client.put(f"/shifts/{shift}/report", json=bad)
    assert r.status_code == 422
    assert r.json()["detail"]["rejected"] is True
    errs = r.json()["detail"]["errors"]
    assert any(e.get("terminal_id") == "T1" and e.get("seq") == 1
               for e in errs)

    # 整次更正退回：班报与明细都还是旧状态
    assert client.get(f"/shifts/{shift}/details").json() == before_report
    assert _detail_map(shift)[("T1", 1)] == (0, 15)
    assert _check(shift)["total_count"] == 14


def test_remap_rollback_when_over_reported_count():
    shift = "S3"
    client.put(f"/shifts/{shift}/report", json=REPORT)
    # T2 在 B1 报 2 笔、交 2 笔
    client.post(f"/shifts/{shift}/details", json={"items": [
        {"terminal_id": "T2", "seq": 1, "bucket_idx": 1, "value_ms": 22},
        {"terminal_id": "T2", "seq": 2, "bucket_idx": 1, "value_ms": 25},
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 15},
    ]})
    before = _detail_map(shift)

    # 新班报：T2 仍报 B0（区间 [21,29]）但笔数只报 1 -> 2 笔明细归入即超报
    bad = {
        **REPORT_SWAPPED,
        "terminals": [
            {"terminal_id": "T1", "bucket_idx": 1, "count": 7},
            {"terminal_id": "T2", "bucket_idx": 0, "count": 1},
            {"terminal_id": "T3", "bucket_idx": 2, "count": 2},
            {"terminal_id": "T3", "bucket_idx": 3, "count": 1},
            {"terminal_id": "T4", "bucket_idx": 3, "count": 2},
        ],
    }
    r = client.put(f"/shifts/{shift}/report", json=bad)
    assert r.status_code == 422
    errs = r.json()["detail"]["errors"]
    assert any(e.get("terminal_id") == "T2" and e.get("bucket_idx") == 0
               and e.get("remapped") == 2 and e.get("capacity") == 1
               for e in errs)

    # 原班报与明细继续可用，核对不报错
    assert _detail_map(shift) == before
    assert _check(shift)["total_count"] == 14


def test_interleaved_swap_then_submit_uses_effective_report():
    shift = "S4"
    client.put(f"/shifts/{shift}/report", json=REPORT)
    client.post(f"/shifts/{shift}/details", json={"items": [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 15},
        {"terminal_id": "T2", "seq": 1, "bucket_idx": 1, "value_ms": 25},
    ]})
    # 换档生效
    r = client.put(f"/shifts/{shift}/report", json=REPORT_SWAPPED)
    assert r.status_code == 200

    # 换档纳入了此前明细：T1#1 已在新 B1
    assert _detail_map(shift)[("T1", 1)] == (1, 15)

    # 换档后的补交按生效班报整批校验：用旧档号 B0 交 T1 明细必被退回
    r = client.post(f"/shifts/{shift}/details", json={"items": [
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 18},
    ]})
    assert r.status_code == 422
    assert r.json()["detail"]["rejected"] is True
    assert ("T1", 2) not in _detail_map(shift)

    # 用新档号 B1（[10,20]）补交成功；整批中一条非法则整批不落库
    r = client.post(f"/shifts/{shift}/details", json={"items": [
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 1, "value_ms": 18},
        {"terminal_id": "T1", "seq": 3, "bucket_idx": 0, "value_ms": 99},
    ]})
    assert r.status_code == 422
    assert ("T1", 2) not in _detail_map(shift)

    r = client.post(f"/shifts/{shift}/details", json={"items": [
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 1, "value_ms": 18},
    ]})
    assert r.status_code == 200 and r.json()["inserted"] == 1
    assert _detail_map(shift)[("T1", 2)] == (1, 18)


def test_swap_rollback_then_old_report_still_checks_and_accepts_details():
    shift = "S5"
    client.put(f"/shifts/{shift}/report", json=REPORT)
    client.post(f"/shifts/{shift}/details", json={"items": [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 15},
    ]})
    # 让 15 找不到新档的换档 -> 退回
    bad = {
        **REPORT_SWAPPED,
        "buckets": [
            {"lo": 21, "hi": 29}, {"lo": 16, "hi": 20},
            {"lo": 30, "hi": 40}, {"lo": 41, "hi": 50},
        ],
    }
    assert client.put(f"/shifts/{shift}/report", json=bad).status_code == 422

    # 继续用原班报核对、补交均正常
    assert _check(shift)["total_count"] == 14
    r = client.post(f"/shifts/{shift}/details", json={"items": [
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 19},
    ]})
    assert r.status_code == 200
    assert _detail_map(shift)[("T1", 2)] == (0, 19)


def test_concurrent_swap_and_submit_never_corrupts():
    shift = "S6"
    client.put(f"/shifts/{shift}/report", json=REPORT)
    client.post(f"/shifts/{shift}/details", json={"items": [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 15},
        {"terminal_id": "T2", "seq": 1, "bucket_idx": 1, "value_ms": 25},
    ]})

    errors: list[Exception] = []

    def swap():
        try:
            for _ in range(30):
                client.put(f"/shifts/{shift}/report", json=REPORT_SWAPPED)
                client.put(f"/shifts/{shift}/report", json=REPORT)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def submit_old_idx():
        # 旧班报下合法的提交（B0=[10,20]）；换档期间可能 422，也可能 200
        try:
            for i in range(30):
                client.post(f"/shifts/{shift}/details", json={"items": [
                    {"terminal_id": "T1", "seq": 100 + i,
                     "bucket_idx": 0, "value_ms": 15},
                ]})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def checks():
        try:
            for _ in range(60):
                r = client.post(f"/shifts/{shift}/check")
                # 任何已提交状态下 /check 都只能是 200 或 422，绝不 500
                assert r.status_code in (200, 422), r.text
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=swap),
               threading.Thread(target=submit_old_idx),
               threading.Thread(target=checks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    # 最终状态自洽：每笔明细的档号与其耗时、当班班报完全一致
    r = client.get(f"/shifts/{shift}/details")
    assert r.status_code == 200
    # 重新登记 REPORT 后收尾，确认无重复键/孤档
    client.put(f"/shifts/{shift}/report", json=REPORT)
    final = _detail_map(shift)
    for (term, seq), (bidx, val) in final.items():
        b = REPORT["buckets"][bidx]
        assert b["lo"] <= val <= b["hi"], (term, seq, bidx, val)
    # 并发结束后核对仍可用
    assert _check(shift)["total_count"] == 14


def test_check_reads_one_committed_snapshot_while_submits_continue():
    shift = "S7"
    client.put(f"/shifts/{shift}/report", json=REPORT)
    initial = [
        {"terminal_id": "T1", "seq": i + 1, "bucket_idx": 0,
         "value_ms": 20} for i in range(3)]
    client.post(f"/shifts/{shift}/details", json={"items": initial})

    stop = threading.Event()
    errors: list[Exception] = []

    def keep_submitting():
        i = 0
        try:
            while not stop.is_set():
                client.post(f"/shifts/{shift}/details", json={"items": [
                    {"terminal_id": "T1", "seq": 10 + i,
                     "bucket_idx": 0, "value_ms": 20},
                ]})
                i += 1
                if i >= 4:          # T1@B0 容量 7，满后预期 422
                    i = 0
                    # 用更正把名额释放出来，保持持续写入
                    client.post(f"/shifts/{shift}/details", json={"items": [
                        {"terminal_id": "T1", "seq": 10,
                         "bucket_idx": 0, "value_ms": 10},
                    ]})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    writer = threading.Thread(target=keep_submitting)
    writer.start()
    try:
        for _ in range(20):
            r = client.post(f"/shifts/{shift}/check")
            assert r.status_code == 200, r.text
            body = r.json()
            # 单次响应内部自洽：pinned_count 对应某一已提交快照
            assert body["pinned_count"] <= body["total_count"] == 14
    finally:
        stop.set()
        writer.join()
    assert not errors, errors
