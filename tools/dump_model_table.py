# -*- coding: utf-8 -*-
"""从 APK 明文 JS 里抽出本机型 SG-500u-YDw11 的完整光谱表（跨行配平花括号）"""
import json

P = r"C:\Users\jsbyf\AppData\Local\Doubao\User Data\Default\.doubao\agent_mode\workspace\.sessions\38455597165272834\agents\m_0cweA9NpS4N\lamp.txt"
text = open(P, encoding="utf-8", errors="replace").read()

anchor = 'name:"SG-500u-YDw11"'
a = text.find(anchor)
assert a > 0, "没找到机型"
c = text.find("config:'", a)
start = c + len("config:'")

depth = 0
in_str = False
esc = False
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

raw = text[start:end]
cfg = json.loads(raw)
print("model = SG-500u-YDw11   config keys:", list(cfg.keys()))

zhu = cfg.get("zhudengConfig", [])
print("主灯光谱分组数:", len(zhu))
for g in zhu:
    print("\n=== 分组「%s」  fans %s-%s ===" % (g.get("name"), g.get("fansMin"), g.get("fansMax")))
    for tab in g.get("tabList", []):
        sp = tab["spectrums"]
        print("  【%s】%d 档，功率范围 %sW-%sW" % (tab["tabName"], len(sp),
                                              sp[0].get("P"), sp[-1].get("P")))
        for s in sp:
            print("     %2d档  R%-3s G%-3s W%-3s B%-3s  风扇%-2s  %sW" % (
                s["number"], s.get("R"), s.get("G"), s.get("W"), s.get("B"),
                s.get("F"), s.get("P")))
