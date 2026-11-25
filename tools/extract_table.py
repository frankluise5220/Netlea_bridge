# -*- coding: utf-8 -*-
"""抽取 SG-500u-YDw11 主灯全表 -> model_spectrums.json"""
import json, os

P = r"C:\Users\jsbyf\AppData\Local\Doubao\User Data\Default\.doubao\agent_mode\workspace\.sessions\38455597165272834\agents\m_0cweA9NpS4N\lamp.txt"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_spectrums.json")
text = open(P, encoding="utf-8", errors="replace").read()
a = text.find('name:"SG-500u-YDw11"')
c = text.find("config:'", a)
start = c + len("config:'")
depth = in_str = 0
esc = False
in_str = False
end = None
for i in range(start, len(text)):
    ch = text[i]
    if in_str:
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_str = False
        continue
    if ch == '"':
        in_str = True
    elif ch == "{":
        depth += 1
    elif ch == "}":
        depth -= 1
        if depth == 0:
            end = i + 1
            break
cfg = json.loads(text[start:end])

out = {"groups": []}
for g in cfg["zhudengConfig"]:
    gd = {"name": g["name"], "fansMin": g.get("fansMin"), "fansMax": g.get("fansMax"), "tabs": []}
    for tab in g["tabList"]:
        gd["tabs"].append({"name": tab["tabName"],
                           "levels": [{"n": s["number"], "R": s.get("R", 0), "G": s.get("G", 0),
                                       "W": s.get("W", 0), "B": s.get("B", 0), "F": s.get("F", 0),
                                       "P": s.get("P", 0)} for s in tab["spectrums"]]})
    out["groups"].append(gd)

json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("写入", OUT)
for g in out["groups"]:
    print(g["name"], "->", [(t["name"], len(t["levels"])) for t in g["tabs"]])
