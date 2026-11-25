# -*- coding: utf-8 -*-
"""生成 bridge.py 内嵌的 LEVELS 表代码块"""
import json

S = json.load(open(r"C:\Users\jsbyf\DoubaoWork\chats\2026-11-25\new-chat-1\model_spectrums.json", encoding="utf-8"))
GNAMES = {"原生": "原生", "自然": "自然", "增艳": "增艳"}

lines = []
idx = 0
for g in S["groups"]:
    for tab in g["tabs"]:
        name = "%s·%s" % (g["name"], tab["name"])
        rows = [(l["R"], l["G"], l["W"], l["B"], l["F"], l["P"]) for l in tab["levels"]]
        lines.append('    %d: ("%s", [%s]),' % (
            idx, name, "".join("(%d,%d,%d,%d,%d,%d), " % r for r in rows)))
        idx += 1

print("\n".join(lines))
