# -*- coding: utf-8 -*-
p = r"C:\Users\jsbyf\AppData\Local\Doubao\User Data\Default\.doubao\agent_mode\workspace\.sessions\38455597165272834\agents\m_0cweA9NpS4N\lamp.txt"
ln = open(p, encoding="utf-8", errors="replace").readlines()[80407]
i = ln.find("config:'")
print(repr(ln[i:i + 300]))
