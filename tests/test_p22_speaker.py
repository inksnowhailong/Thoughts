"""P2.2 B1：声纹库 v2、旧库兼容、top-k 打分、备份回滚、注册会话。
全程用临时目录里的假库，不碰线上 data\\speakers.npz。
运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p22_speaker.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import guanjia.speaker as spk_mod  # noqa: E402
from guanjia.enroll_session import MIN_SELF_SIM, MIN_TOTAL, EnrollSession, quality  # noqa: E402
from guanjia.settings import Settings  # noqa: E402
from guanjia.speaker import STORE, SpeakerGate, backup_path, load_store, save_store, score_against  # noqa: E402

failed = 0


def check(cond: bool, name: str) -> None:
    global failed
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failed += 1


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


rng = np.random.default_rng(0)
tmp = Path(tempfile.mkdtemp(prefix="gj-spk-"))
try:
    # 1. top-k 平均
    refs = np.stack([unit([1, 0, 0]), unit([0.9, 0.1, 0]), unit([0, 1, 0]), unit([0, 0, 1])])
    v = unit([1, 0, 0])
    sims = sorted((refs @ v).tolist())[-3:]
    check(abs(score_against(v, refs) - sum(sims) / 3) < 1e-6, "score = mean of top-3 cosines")
    check(abs(score_against(v, refs[:1]) - 1.0) < 1e-6, "single ref = plain cosine (legacy unchanged)")

    # 2. v2 存取
    store = tmp / "speakers.npz"
    db = {"我": np.stack([unit(rng.standard_normal(192)) for _ in range(5)]), "妈": unit(rng.standard_normal(192))[None]}
    save_store(store, db)
    got, legacy = load_store(store, 192)
    check(not legacy and set(got) == {"我", "妈"} and got["我"].shape == (5, 192), "v2 round trip keeps names and all samples")
    check(not list(tmp.glob("*.tmp")), "no temp file left after save")

    # 3. 旧库：每人一个 (192,) 向量
    old = tmp / "old.npz"
    np.savez(old, **{"我": unit(rng.standard_normal(192))})
    got, legacy = load_store(old, 192)
    check(legacy and got["我"].shape == (1, 192), "legacy store read as [1,192]")
    try:
        load_store(old, 512)
        check(False, "dim mismatch raises")
    except ValueError:
        check(True, "dim mismatch raises")

    # 4. 备份文件名
    check(backup_path(Path("data/speakers.npz")).name == "speakers.campp.pre-reenroll.npz", "campplus backup name")
    check(backup_path(Path("data/speakers.eres2netv2.npz")).name == "speakers.eres2netv2.pre-reenroll.npz", "other model backup name")

    # 5. 质量检查
    sr = 16000
    voice = (0.3 * np.sin(np.arange(sr * 3) / 7)).astype(np.float32)
    check(quality(voice)[0], "3s voiced segment passes")
    short = voice[: sr]
    check(not quality(short)[0], "1s segment rejected (effective < 1.5s)")
    check(quality(voice[: int(sr * 1.2)], min_effective=1.0)[0], "far group: 1.2s effective passes with 1.0s floor")
    check(not quality(voice[: int(sr * 0.8)], min_effective=1.0)[0], "far group: 0.8s effective still rejected")
    gated = voice.copy()
    gated[: int(len(gated) * 0.6)] = 0
    check(not quality(gated)[0], "60% digital zeros rejected")
    check(not quality(voice * 0.01)[0], "too quiet rejected")
    check(not quality(np.tile(voice, 4))[0], "12s segment rejected")

    # 6. 注册会话（假向量函数）
    calls = []

    base = rng.standard_normal(192)

    def fake_embed(x):
        calls.append(len(x))
        return unit(base + 0.1 * rng.standard_normal(192))  # 同一个人的各句：互相很像

    s = EnrollSession("我", fake_embed, old_refs=None)
    check(s.snapshot()["group"] == "near", "starts with near group")
    r = s.feed(short)
    check(not r["ok"] and len(calls) == 0, "bad segment not embedded")
    r = s.feed(voice, playing=True)
    check(not r["ok"] and len(calls) == 0, "segment during playback ignored")
    for _ in range(MIN_TOTAL - 1):
        s.feed(voice)
    check(not s.to_testing(), "cannot test before MIN_TOTAL")
    s.feed(voice)
    s.discard_last()
    check(s.snapshot()["total"] == MIN_TOTAL - 1, "discard_last removes one")
    s.feed(voice)
    check(s.to_testing() and s.state == "testing", "enough samples -> testing")
    s.feed(voice)
    snap = s.snapshot()
    check(s.state == "ready" and snap["total"] == MIN_TOTAL and snap["test_score"] is not None, "test segment scored, not added")
    check(snap["test_old_score"] is None, "no old refs -> no old score")

    # 6b. 同一人一致性：已收 3 段后，正交向量被拒，total 不变（review 3）
    seq = [unit([1, 0.05, 0]), unit([1, 0, 0.05]), unit([1, 0.05, 0.05]), unit([0, 1, 0]), unit([1, 0.02, 0.02])]
    s2 = EnrollSession("我", lambda x: seq.pop(0), old_refs=None)
    for _ in range(3):
        s2.feed(voice)
    r = s2.feed(voice)
    check(not r["ok"] and s2.snapshot()["total"] == 3 and "不像同一个人" in r["reason"] and r["self_score"] < MIN_SELF_SIM,
          "outlier after 3 segments rejected with score, total unchanged")
    check(f"{r['self_score']:.2f}" in r["reason"], "reject reason shows the score")
    check(s2.feed(voice)["ok"] and s2.snapshot()["total"] == 4, "a similar segment is still accepted afterwards")
    seq2 = [unit([1, 0, 0]), unit([0, 1, 0]), unit([0, 0, 1])]
    s3 = EnrollSession("我", lambda x: seq2.pop(0), old_refs=None)
    check(all(s3.feed(voice)["ok"] for _ in range(3)), "first 3 segments are never similarity-checked")

    # 7. 真模型：旧库读取 → save_person 写 v2 → 备份 → 回滚（临时目录里的假库）
    live = tmp / "speakers.npz"
    live.unlink()
    check(live != STORE, "test never points at the live store")
    g0 = SpeakerGate(store=live)
    dim = g0.dim
    check(g0.names() == [] and not g0.legacy, "missing store -> empty gate")
    np.savez(live, **{"我": unit(rng.standard_normal(dim))})
    g = SpeakerGate(store=live)
    check(g.legacy and g.counts() == {"我": 1}, "gate loads legacy store")
    check(g.threshold == Settings().spk_threshold, "default threshold comes from Settings")
    name, score = g.identify(voice)
    check(isinstance(score, float), "identify works on legacy store")
    b = g.save_person("我", np.stack([g.embed(voice), g.embed(voice[::-1].copy())]))
    check(b is not None and b.exists() and load_store(b, dim)[1], "save_person backed up the legacy store first")
    check(not g.legacy and g.counts() == {"我": 2}, "save_person replaces samples and writes v2")
    g.save_person("我", np.stack([g.embed(voice)] * 3))
    check(load_store(b, dim)[1], "pre-reenroll backup is never overwritten (still legacy)")
    v2_bytes = live.read_bytes()
    check(g.rollback() and g.legacy and g.counts() == {"我": 1}, "rollback restores pre-reenroll store")
    prev = live.with_name("speakers.prev.npz")
    check(prev.read_bytes() == v2_bytes, "rollback first refreshes .prev with the store it replaces")

    # 8. 两线程同时保存（两把门 / 同一把门）：pre-reenroll 仍是旧格式库，库是完整 v2，没有临时文件残留（review 1）
    for label, pair in (("two gates", lambda: (SpeakerGate(store=live), SpeakerGate(store=live))),
                        ("one gate", lambda: (lambda x: (x, x))(SpeakerGate(store=live)))):
        for f in tmp.glob("speakers*"):
            f.unlink()
        np.savez(live, **{"我": unit(rng.standard_normal(dim))})
        ga, gb = pair()
        gate_barrier = threading.Barrier(2)
        errors = []

        def commit(gx, embs):
            try:
                gate_barrier.wait()
                gx.save_person("我", embs)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        embs3 = np.stack([unit(rng.standard_normal(dim)) for _ in range(3)])
        embs5 = np.stack([unit(rng.standard_normal(dim)) for _ in range(5)])
        ts = [threading.Thread(target=commit, args=(ga, embs3)), threading.Thread(target=commit, args=(gb, embs5))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        pre = spk_mod.backup_path(live)
        check(not errors, f"{label}: concurrent save_person raised nothing {errors[:1]}")
        check(pre.exists() and load_store(pre, dim)[1] and load_store(pre, dim)[0]["我"].shape == (1, dim),
              f"{label}: pre-reenroll is still the legacy store after concurrent commits")
        got, lg = load_store(live, dim)
        check(not lg and got["我"].shape[0] in (3, 5), f"{label}: live store is a complete v2 store")
        check(not list(tmp.glob("*.tmp")), f"{label}: no temp files left")

    # 9. 备份中途失败：pre-reenroll 不存在、库不变、没有临时文件；之后再保存能成功（review 1）
    for f in tmp.glob("speakers*"):
        f.unlink()
    np.savez(live, **{"我": unit(rng.standard_normal(dim))})
    gm = SpeakerGate(store=live)
    orig_copy2 = shutil.copy2

    def broken_copy2(src, dst, *a, **k):
        Path(dst).write_bytes(b"partial")  # 写了半截再炸
        raise OSError("disk full")

    spk_mod.shutil.copy2 = broken_copy2
    try:
        gm.save_person("我", np.stack([unit(rng.standard_normal(dim))] * 2))
        check(False, "copy2 failure propagates")
    except OSError:
        check(True, "copy2 failure propagates")
    finally:
        spk_mod.shutil.copy2 = orig_copy2
    check(not spk_mod.backup_path(live).exists(), "failed backup leaves no pre-reenroll file")
    check(not list(tmp.glob("*.tmp")) and gm.legacy and gm.counts() == {"我": 1}, "failed backup: no temp file, store and memory unchanged")
    gm.save_person("我", np.stack([unit(rng.standard_normal(dim))] * 2))
    check(spk_mod.backup_path(live).exists() and load_store(spk_mod.backup_path(live), dim)[1], "backup succeeds on retry and is the legacy store")

    # 10. 写库失败（权限等）：内存里的库不切换（review 9）
    before_db = gm.counts()
    orig_save = spk_mod.save_store

    def deny(path, db):
        raise PermissionError("denied")

    spk_mod.save_store = deny
    try:
        gm.save_person("我", np.stack([unit(rng.standard_normal(dim))] * 4))
        check(False, "save_store failure propagates")
    except PermissionError:
        check(gm.counts() == before_db and not gm.legacy, "save_store failure leaves the in-memory store unchanged")
    finally:
        spk_mod.save_store = orig_save

    # 11. 库为空/读不出：identify 分数是 None（review 6）；读不出时记原因并拒绝保存（review 7）
    check(g0.identify(voice) == (None, None), "empty store: identify score is None, not 0.0")
    for f in tmp.glob("speakers*"):
        f.unlink()
    live.write_bytes(b"this is not a zip file")
    gbad = SpeakerGate(store=live)
    check(gbad.load_error and gbad.names() == [] and gbad.identify(voice) == (None, None), "corrupt store: load_error set, treated as empty")
    try:
        gbad.save_person("我", np.stack([unit(rng.standard_normal(dim))]))
        check(False, "save refused on unreadable store")
    except ValueError:
        check(live.read_bytes() == b"this is not a zip file" and not spk_mod.backup_path(live).exists(),
              "save refused on unreadable store: file untouched, no bogus backup")
    save_store(live, {"我": unit(rng.standard_normal(64))[None]})  # 维度不符（模型是 dim，这里 64）
    check(SpeakerGate(store=live).load_error is not None, "dimension mismatch also sets load_error")
    save_store(live, {"我": unit(rng.standard_normal(dim))[None]})
    gok = SpeakerGate(store=live)
    check(gok.load_error is None and gok.counts() == {"我": 1}, "healthy store: no load_error")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL PASS test_p22_speaker" if failed == 0 else f"{failed} FAILED test_p22_speaker")
sys.exit(1 if failed else 0)
