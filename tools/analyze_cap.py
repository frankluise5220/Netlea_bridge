# -*- coding: utf-8 -*-
"""把采集日志按"值变化"切段，输出每次稳态区间和持续时间。"""
import json, sys, collections

P = r"C:\Users\jsbyf\DoubaoWork\chats\2026-11-25\new-chat-1\cap_gear_log.jsonl"
rows = []
for line in open(P, encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        rows.append(json.loads(line))
    except Exception:
        pass

# 只留 cmd20(状态) 做主时间线，cmd26 单独穿插标注
segs = []
cur = None
for r in rows:
    if r["cmd"] != 20:
        continue
    key = (r["on"], r["R"], r["G"], r["W"], r["B"], r["F"])
    if cur is None or cur["key"] != key:
        if cur:
            segs.append(cur)
        cur = {"key": key, "start": r["ts"], "end": r["ts"], "t0": r["t"], "t1": r["t"]}
    else:
        cur["end"] = r["ts"]
        cur["t1"] = r["t"]
if cur:
    segs.append(cur)

print("共 %d 段稳定区间（>=1.5s 的才列出）" % len(segs))
for s in segs:
    dur = s["end"] - s["start"]
    if dur < 1.5:
        continue
    print("%s-%s (%4.1fs)  on=%s  R%-3s G%-3s W%-3s B%-3s F%-3s"
          % (s["t0"], s["t1"], dur, s["key"][0], s["key"][1], s["key"][2],
             s["key"][3], s["key"][4], s["key"][5]))

print("\n--- cmd26 (设备存的 modeId / grade) 时间线 ---")
seen = set()
for r in rows:
    if r["cmd"] != 26:
        continue
    k = (r["modeId"], r["grade"], r["R"], r["G"], r["W"], r["B"], r["F"])
    if k in seen:
        continue
    seen.add(k)
    print("%s  modeId=%-3s grade=%-3s  R%-3s G%-3s W%-3s B%-3s F%-3s"
          % (r["t"], r["modeId"], r["grade"], r["R"], r["G"], r["W"], r["B"], r["F"]))
