# -*- coding: utf-8 -*-
"""
采集「预设 + 功率档」的真实光谱值。
不新开厂商云 WS 连接（会踢掉 NAS 上的常驻桥），全部通过 MQTT debug 主题：
  订阅  netlea/tank_light/debug/raw      <- 桥把设备每一帧原样吐出来
  发布  netlea/tank_light/debug/send     -> 桥代发 cmd21(查状态) / cmd27(读主灯配置)
产出一行一条 JSONL，带毫秒时间戳，事后按时间切段取稳定值。
"""
import json, time, os, sys
import paho.mqtt.client as mqtt

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cap_gear_log.jsonl")
MQ = (os.environ.get("NETLEA_MQTT_HOST", "127.0.0.1"),
      int(os.environ.get("NETLEA_MQTT_PORT", "1883")),
      os.environ.get("NETLEA_MQTT_USER", ""),
      os.environ.get("NETLEA_MQTT_PASS", ""))
BASE = "netlea/tank_light"


def build(cmd, payload):
    t = [0x5A, 0, 1, cmd, 0, 0] + list(payload)
    t[1] = len(t) + 1
    t.append(sum(t) % 256)
    return "".join("%02X" % b for b in t)


f = open(OUT, "a", encoding="utf-8")


def on_message(cl, u, m):
    try:
        o = json.loads(m.payload.decode())
        b = [int(o["hex"][i:i + 2], 16) for i in range(0, len(o["hex"]), 2)]
    except Exception:
        return
    cmd = b[3]
    p = b[6:-1]

    def g(i):
        return p[i] if len(p) > i else None

    if cmd == 20:      # 状态：on0 mode1 ... R6 G7 W8 B9 F10
        row = {"ts": round(time.time(), 2), "t": time.strftime("%H:%M:%S"), "cmd": cmd,
               "on": g(0), "mode": g(1), "R": g(6), "G": g(7), "W": g(8), "B": g(9), "F": g(10),
               "modeId": None, "grade": None}
    elif cmd == 26:    # 配置：on2(0,1) off2(2,3) R4 G5 W6 B7 F8 modeId9 grade(10,11)
        row = {"ts": round(time.time(), 2), "t": time.strftime("%H:%M:%S"), "cmd": cmd,
               "on": g(4) is not None and 1 or None, "mode": None,
               "R": g(4), "G": g(5), "W": g(6), "B": g(7), "F": g(8),
               "modeId": g(9), "grade": (g(10) or 0) | ((g(11) or 0) << 8),
               "fade_on": g(0), "fade_off": g(2)}
    else:
        return
    f.write(json.dumps(row, ensure_ascii=False) + "\n")
    f.flush()
    if cmd in (20, 26):
        print(row["t"], "cmd%d" % cmd, "on=%s mode=%s R%s G%s W%s B%s F%s%s" % (
            row["on"], row["mode"], row["R"], row["G"], row["W"], row["B"], row["F"],
            ("  modeId=%s grade=%s" % (row["modeId"], row["grade"])) if cmd == 26 else ""), flush=True)


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "cap-%d" % time.time())
c.username_pw_set(MQ[2], MQ[3])
c.connect(MQ[0], MQ[1], 20)
c.on_message = on_message
c.subscribe(BASE + "/debug/raw")
c.loop_start()
time.sleep(1)

print("采集已启动，日志:", OUT, flush=True)
print("每 1.2s 查一次状态(cmd21)，每 6s 读一次配置(cmd27→cmd26 拿 modeId/grade)", flush=True)
last27 = 0
try:
    while True:
        c.publish(BASE + "/debug/send", build(21, []))
        if time.time() - last27 > 6:
            c.publish(BASE + "/debug/send", build(27, [0]))
            last27 = time.time()
        time.sleep(1.2)
except KeyboardInterrupt:
    pass
finally:
    c.loop_stop()
    f.close()
