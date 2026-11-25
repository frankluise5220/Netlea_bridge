# -*- coding: utf-8 -*-
"""用 model_spectrums.json 覆盖 bridge.py 里的 LEVELS 表（避免手抄出错）"""
import json, re

BR = r"C:\Users\jsbyf\AppData\Local\Doubao\User Data\Default\.doubao\agent_mode\workspace\.sessions\38455597165272834\agents\m_0cweA9NpS4N\bridge.py"
S = json.load(open(r"C:\Users\jsbyf\DoubaoWork\chats\2026-11-25\new-chat-1\model_spectrums.json", encoding="utf-8"))

rows = []
idx = 0
for g in S["groups"]:
    for tab in g["tabs"]:
        name = "%s·%s" % (g["name"], tab["name"])
        vals = "".join("(%d,%d,%d,%d,%d,%d)," % (l["R"], l["G"], l["W"], l["B"], l["F"], l["P"])
                       for l in tab["levels"])
        rows.append('    %d: ("%s", [%s]),' % (idx, name, vals))
        idx += 1
block = "LEVELS = {\n" + "\n".join(rows) + "\n}\n"

src = open(BR, encoding="utf-8").read()
start = src.index("LEVELS = {")
end = src.index("DEFAULT_LEVEL =", start)
new = src[:start] + block + "\n" + src[end:]
open(BR, "w", encoding="utf-8").write(new)

import ast
ast.parse(new)
print("LEVELS 已重建：%d 个预设" % idx)
for i, g in enumerate(S["groups"]):
    print(" ", g["name"], [(t["name"], len(t["levels"])) for t in g["tabs"]])
