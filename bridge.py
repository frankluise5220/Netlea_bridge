"""尼特利「水族时光」灯 -> Home Assistant 桥接（厂商云 WebSocket <-> MQTT）
v4：主灯/射灯/氛围灯 三路 + 光谱五通道 + 功率(grade) + 行程读取 + 跟随模式

命令表（逆向自官方 app-service.js，全部实测）：
  cmd5  0x05 灯具初始化（会重载设备存储状态，禁止用于轮询）
  cmd16 0x10 主灯设置 lampState|lampMode|r1|r2|R|G|W|B|F|restore(2B)|modeId|grade(2B)|r3|r4
  cmd17 0x11 射灯设置 lampState|lampMode|r1|r2|moodLamp|restore(2B)|grade(1B)|r3
  cmd30 0x1E 氛围灯设置（同 cmd17，需 followType=0 手动才生效）
  cmd34 0x22 氛围灯跟随 followType|channel|grade  (0手动/1随主灯亮/2主灯灭时亮)
  cmd21 0x15 查询状态（安全，只回 cmd20）
  cmd27 0x1B 读主灯配置 -> 设备回 cmd26
  cmd29 0x1D 读射灯配置 -> 设备回 cmd28
  cmd33 0x21 读氛围灯配置 -> 设备回 cmd32
  cmd37 0x25 按索引读： [1]->cmd18 主灯行程(10段)  [2]->cmd19 射灯行程(10段)  [0]->cmd32  [3]->cmd25
  cmd20 0x14 状态上报 master13B@0 / spot9B@13 / atmosphere9B@22
"""
import json, time, queue, traceback, os, threading, functools
import http.server
import urllib.request, urllib.parse, urllib.error
import paho.mqtt.client as mqtt
import websocket

CFG_PATH = os.environ.get("NETLEA_CONFIG", "/app/config.json")


def load_cfg():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


CFG = load_cfg()
WS_URL = CFG.get("ws_url") or "ws://v3grpc.netlea.com:8088/websocket"
TOK = CFG.get("token", "")
DEV = CFG.get("device_id", "")
MQ = CFG.get("mqtt") or {"host": "", "port": 1883, "username": "",
                         "password": "", "prefix": "homeassistant"}

B = "netlea/tank_light"
T_AVAIL = B + "/availability"
T_MSTATE, T_MCMD = B + "/state", B + "/set"
T_DBG_SEND = B + "/debug/send"
T_DBG_STATE = B + "/debug/state20"
T_DBG_RAW = B + "/debug/raw"

SPOT = "netlea/tank_spot"
T_SSTATE, T_SCMD = SPOT + "/state", SPOT + "/set"
ATMO = "netlea/tank_atmo"
T_ASTATE, T_ACMD = ATMO + "/state", ATMO + "/set"
T_FSTATE, T_FCMD = ATMO + "/follow/state", ATMO + "/follow/set"
T_SFSTATE, T_SFCMD = SPOT + "/follow/state", SPOT + "/follow/set"

# 开灯/关灯渐变时长（分钟，1-300）
FADE = {}
FADE_SET = {}
for _k, _base in (("master", B), ("spot", SPOT), ("atmo", ATMO)):
    for _d in ("on", "off"):
        FADE[(_k, _d)] = "%s/fade_%s/state" % (_base, _d)
        FADE_SET[(_k, _d)] = "%s/fade_%s/set" % (_base, _d)

T_GRADE = {"master": B + "/grade/state", "spot": SPOT + "/grade/state",
           "atmo": ATMO + "/grade/state"}
T_GRADE_SET = {"master": B + "/grade/set", "spot": SPOT + "/grade/set",
               "atmo": ATMO + "/grade/set"}
T_MODE = {"master": B + "/mode/state", "spot": SPOT + "/mode/state",
          "atmo": ATMO + "/mode/state"}
T_TRIP = {"master": B + "/trip/state", "spot": SPOT + "/trip/state",
          "atmo": ATMO + "/trip/state"}
T_TRIP_SET = {"master": B + "/trip/set", "spot": SPOT + "/trip/set",
              "atmo": ATMO + "/trip/set"}
T_TRIP_ATTR = {"master": B + "/trip/attr", "spot": SPOT + "/trip/attr",
               "atmo": ATMO + "/trip/attr"}

CHS = ["R", "G", "W", "B", "F"]
# 关键的物理槽位顺序：设备/官方表的四通道顺序是 R, W(白), G(绿), B —— 不是 R,G,W,B。
# 早期把第 2 槽当 G、第 3 槽当 W，导致 HA 里"绿/白"两个滑条互换，颜色调偏。
SER = ("R", "W", "G", "B", "F")
# 设备产品定义的通道量程是 0-110（APP 滑块上限 110，与 B 通道"最高110"一致）。
# 协议层虽然能收 255，但那是越界值，驱动会钳制，且四通道总和超限的帧会被静默丢弃。
DEV_MAX = 110
SAFE_TOTAL = 400
# 风扇：设备存的是 PWM，APP 界面是 0-10 档；本机型映射（云端 fansConfig fansList）
# 0→0  1→100 2→110 3→120 4→130 5→145 6→165 7→180 8→200 9→220 10→230
FAN_PWM = [0, 100, 110, 120, 130, 145, 165, 180, 200, 220, 230]


def f_gear(pwm):
    """PWM → 最接近的档位"""
    return min(range(len(FAN_PWM)), key=lambda i: abs(FAN_PWM[i] - int(pwm)))


def gear_pwm(gear):
    """档位 → PWM"""
    return FAN_PWM[max(0, min(10, int(gear)))]


def fan_min_pwm(total):
    """按四通道总功率给出风扇最低转速（散热兜底，只升不降）"""
    if total <= 100:
        return 0
    if total <= 200:
        return FAN_PWM[3]     # 3 档 120
    if total <= 300:
        return FAN_PWM[5]     # 5 档 145
    return FAN_PWM[7]         # 7 档 180


def ch_to_rgb(ch):
    """设备四通道 -> 屏幕色彩。W 是白光，同时加到 R/G/B 上"""
    k = 255.0 / DEV_MAX
    def cv(v):
        return max(0, min(255, int(round(v * k))))
    w = ch.get("W", 0)
    return (cv(ch.get("R", 0) + w), cv(ch.get("G", 0) + w), cv(ch.get("B", 0) + w))


def rgb_to_ch(r, g, b, dev):
    """屏幕色彩 -> 设备四通道：先按 110 上限等比缩放，再用白光吃掉共同部分"""
    k = DEV_MAX / 255.0
    R, G, B = [max(0, min(DEV_MAX, int(round(v * k)))) for v in (r, g, b)]
    W = min(R, G, B)
    R, G, B = R - W, G - W, B - W
    ch = {"R": R, "W": W, "G": G, "B": B}
    total = R + W + G + B
    if total > SAFE_TOTAL and total > 0:
        f = SAFE_TOTAL / float(total)
        for x in ("R", "W", "G", "B"):
            ch[x] = int(ch[x] * f)
        total = sum(ch[x] for x in ("R", "W", "G", "B"))
    ch["F"] = max(fan_min_pwm(total), dev.get("F", 0))
    return ch
T_CHSTATE = {c: "netlea/master/ch/%s/state" % c for c in CHS}
T_CHCMD = {c: "netlea/master/ch/%s/set" % c for c in CHS}

# 日出/日落渐变（cmd18 帧头：sunMoonDisable | sunTime(2B) | moonTime(2B)，单位分钟）
T_SUNMD_ST, T_SUNMD_SET = B + "/sunmoon_disable/state", B + "/sunmoon_disable/set"
T_SUN_ST, T_SUN_SET = B + "/sunrise_fade/state", B + "/sunrise_fade/set"
T_MOON_ST, T_MOON_SET = B + "/sunset_fade/state", B + "/sunset_fade/set"

FOLLOW_OPTS = ["手动控制", "随主灯亮", "主灯灭时亮"]
MODE_OPTS = {0: "手动控制", 3: "行程运行", 4: "临时开关"}
DEFAULT_CH = {"R": 39, "G": 24, "W": 50, "B": 32, "F": 130}
STEP_SEC = 60

# 主灯 12 个出厂光谱预设：名称/modeId 已按 APP 实际顺序核定
# modeId 0-3 原生(溪流/水陆/南美/爆藻)、4-7 自然(正白/高显指/夕阳/月光)、8-11 增艳(红焰/湖蓝/紫霞/蓝调)、12 自定义
PRESET_NAMES = ["原生·溪流", "原生·水陆", "原生·南美", "原生·爆藻",
                "自然·正白", "自然·高显指", "自然·夕阳", "自然·月光",
                "增艳·红焰", "增艳·湖蓝", "增艳·紫霞", "增艳·蓝调",
                "自定义色（当前通道）"]

# 原厂全档位表（来自官方 APK 内置机型数据 SG-500u-YDw11，非推算）
# 每档 = (R, G, W, B, 风扇档, 功率W)；档号从 1 开始，与设备里的 grade 完全一致
LEVELS = {
    0: ("原生·溪流", [(5,5,3,7,1,3),(6,6,4,8,1,5),(8,8,5,11,1,8),(8,9,7,16,2,10),(15,13,8,18,2,13),(16,16,10,23,2,15),(16,16,14,25,3,18),(18,17,15,30,3,20),(21,21,17,32,3,23),(23,23,19,36,3,25),(24,24,20,38,3,28),(26,25,21,42,4,30),(31,31,25,45,4,33),(32,32,26,46,4,35),(33,33,28,52,4,38),(33,34,29,53,4,40),(38,38,34,58,5,43),(39,39,36,59,5,45),(40,41,38,61,5,48),(41,46,40,67,5,50),]),
    1: ("原生·水陆", [(4,5,7,5,1,3),(5,6,8,6,1,5),(8,10,7,9,1,8),(8,10,10,9,2,10),(9,17,15,11,2,13),(13,18,17,13,2,15),(13,20,20,16,3,18),(14,22,26,16,3,20),(15,24,28,17,3,23),(16,30,29,18,3,25),(17,32,31,22,3,28),(23,34,33,23,4,30),(24,40,34,24,4,33),(26,47,34,25,4,35),(31,49,36,29,4,38),(32,54,37,30,4,40),(33,57,38,31,5,43),(34,63,39,32,5,45),(34,65,41,33,5,48),(38,70,48,37,5,50),]),
    2: ("原生·南美", [(5,3,8,0,1,3),(7,4,13,0,1,5),(12,6,16,0,1,8),(15,6,18,0,2,10),(17,7,23,0,2,13),(23,7,28,0,2,15),(25,8,32,0,3,18),(33,9,35,0,3,20),(37,10,38,0,3,23),(41,10,41,0,3,25),(46,15,45,0,3,28),(49,15,49,0,4,30),(56,16,50,0,4,33),(61,17,54,0,4,35),(65,17,58,0,4,38),(72,17,62,0,4,40),(74,23,64,0,5,43),(81,23,66,0,5,45),(88,23,72,0,5,48),(96,24,74,0,5,50),]),
    3: ("原生·爆藻", [(6,3,5,6,1,3),(8,4,8,9,1,5),(14,5,7,10,1,8),(16,5,11,15,2,10),(21,6,12,17,2,13),(25,7,13,20,2,15),(30,7,14,23,3,18),(34,8,16,26,3,20),(38,8,17,30,3,23),(43,9,18,32,3,25),(48,9,19,35,3,28),(52,10,19,38,4,30),(59,12,20,41,4,33),(64,12,23,45,4,35),(67,12,26,47,4,38),(74,12,28,50,4,40),(76,12,29,52,5,43),(82,12,34,54,5,45),(87,12,37,57,5,48),(91,12,41,61,5,50),]),
    4: ("自然·正白", [(3,4,11,5,1,3),(6,7,7,7,1,5),(8,9,8,9,1,8),(9,14,8,10,2,10),(14,16,12,11,2,13),(15,17,13,15,2,15),(17,24,15,17,3,18),(23,25,16,18,3,20),(24,32,16,23,3,23),(25,33,17,24,3,25),(31,40,18,25,3,28),(32,42,19,27,4,30),(32,46,21,30,4,33),(35,49,23,31,4,35),(39,50,27,32,4,38),(40,56,28,33,4,40),(41,59,30,37,5,43),(41,60,31,38,5,45),(44,65,34,39,5,48),(48,70,38,42,5,50),]),
    5: ("自然·高显指", [(2,4,17,4,1,5),(3,6,27,6,2,10),(3,7,36,7,2,15),(3,8,47,8,3,20),(5,10,52,9,3,25),(6,16,62,10,4,30),(7,18,70,11,4,35),(7,23,81,14,4,40),(7,24,91,16,5,45),(7,26,105,17,5,50),]),
    6: ("自然·夕阳", [(12,6,9,5,1,5),(17,7,13,6,2,10),(29,8,18,7,2,15),(41,10,23,8,3,20),(50,15,28,9,3,25),(58,15,38,9,4,30),(65,15,48,9,4,35),(77,16,55,10,4,40),(84,16,61,10,5,45),(95,16,74,10,5,50),]),
    7: ("自然·月光", [(0,0,2,1,0,1),(0,0,3,2,0,1),(0,0,3,2,0,1),(0,0,3,2,0,1),(0,0,3,2,0,1),]),
    8: ("增艳·红焰", [(5,4,5,5,1,3),(7,6,5,7,1,5),(9,8,6,9,1,8),(16,9,7,10,2,10),(20,12,9,13,2,13),(24,16,10,16,2,15),(31,17,11,17,3,18),(33,22,11,20,3,20),(39,25,12,23,3,23),(41,26,13,24,3,25),(46,31,15,28,3,28),(49,33,16,30,4,30),(53,37,17,32,4,33),(57,40,18,33,4,35),(61,41,19,35,4,38),(65,46,20,37,4,40),(71,47,21,38,5,43),(73,49,21,39,5,45),(79,55,22,43,5,48),(82,58,25,46,5,50),]),
    9: ("增艳·湖蓝", [(4,5,3,5,1,3),(5,6,5,6,1,5),(7,9,7,9,1,8),(9,15,5,10,2,10),(15,17,8,14,2,13),(16,22,10,16,2,15),(16,24,13,17,3,18),(17,29,15,21,3,20),(17,31,18,23,3,23),(19,33,20,24,3,25),(21,36,25,25,3,28),(23,38,28,29,4,30),(24,41,29,30,4,33),(25,46,30,31,4,35),(26,49,30,32,4,38),(31,54,35,36,4,40),(32,56,36,38,5,43),(33,58,38,38,5,45),(33,64,41,40,5,48),(34,68,44,43,5,50),]),
    10: ("增艳·紫霞", [(4,4,2,6,1,3),(5,5,5,8,1,5),(8,8,6,16,1,8),(8,7,11,17,2,10),(14,9,10,22,2,13),(15,10,11,24,2,15),(16,14,13,30,3,18),(20,16,15,32,3,20),(22,16,16,37,3,23),(25,20,16,42,3,25),(30,23,17,45,3,28),(31,24,18,50,4,30),(32,25,19,52,4,33),(35,27,20,55,4,35),(39,31,24,59,4,38),(40,32,24,60,4,40),(46,38,24,66,5,43),(48,40,24,68,5,45),(50,42,25,70,5,48),(51,48,27,76,5,50),]),
    11: ("增艳·蓝调", [(3,8,4,13,1,5),(3,10,9,23,2,10),(3,17,11,31,2,15),(3,24,13,39,3,20),(3,32,14,49,3,25),(4,39,15,59,4,30),(4,48,16,67,4,35),(4,54,18,74,4,40),(4,63,19,82,5,45),(4,73,20,90,5,50),]),
}

DEFAULT_LEVEL = {0: 6, 1: 10, 2: 12, 3: 16, 4: 9, 5: 4, 6: 4, 7: 2, 8: 9, 9: 9, 10: 9, 11: 4}


def level_list(mode_id):
    return LEVELS.get(mode_id, ("", []))[1]


def level_values(mode_id, n):
    lv = level_list(mode_id)
    if not lv:
        return None
    return lv[max(0, min(len(lv) - 1, int(n) - 1))]
T_PSTATE, T_PSET = B + "/preset/state", B + "/preset/set"
T_POWER = B + "/power/state"

MASTER = ["stateType", "startH", "startM", "lampState", "lampMode", "pattern",
          "R", "W", "G", "B", "F", "r1", "r2"]
GRP = ["stateType", "startH", "startM", "lampState", "lampMode", "pattern",
       "moodLamp", "followType", "r1"]
TRIP_M = ["tripDisable", "weekCycle", "startH", "startM", "endH", "endM",
          "R", "G", "W", "B", "F", "modeId"]
TRIP_S = ["tripDisable", "weekCycle", "startH", "startM", "endH", "endM",
          "moodLamp", "grade"]


def build(cmd, payload, devtype=1, extra=None):
    t = [0x5A, 0, devtype, cmd, 0, 0] + list(payload)
    if extra:
        extra(t)
    t[1] = len(t) + 1
    t.append(sum(t) % 256)
    return "".join("%02X" % b for b in t)


def cmd16(lampState, lampMode, chans, modeId, grade, restore=0):
    # 官方 app 布局（app-service.js: lastControlHex）：
    # lampState|lampMode|r1|r2|R|G|W|B|F| restore(4B) |modeId(1B)| grade(4B) |r3|r4
    body = [lampState, lampMode, 0, 0] + [chans.get(c, 0) for c in SER]
    body += [restore & 0xFF, (restore >> 8) & 0xFF, (restore >> 16) & 0xFF, (restore >> 24) & 0xFF,
             modeId & 0xFF,
             grade & 0xFF, (grade >> 8) & 0xFF, (grade >> 16) & 0xFF, (grade >> 24) & 0xFF,
             0, 0]
    return build(16, body)


def grp_cmd(cmd, lampState, lampMode, mood, grade=0, restore=0):
    return build(cmd, [lampState, lampMode, 0, 0, mood & 0xFF,
                       restore & 0xFF, (restore >> 8) & 0xFF, grade & 0xFF, 0])


def cmd18(sunMoonDisable, sunTime, moonTime, trips, trip_mode=0):
    body = [sunMoonDisable & 1, sunTime & 0xFF, (sunTime >> 8) & 0xFF,
            moonTime & 0xFF, (moonTime >> 8) & 0xFF]
    for i in range(10):
        t = trips[i] if i < len(trips) else {}
        body += [t.get(k, 0) for k in TRIP_M]
        g = int(t.get("grade", 0))
        body += [g & 0xFF, (g >> 8) & 0xFF]
    # 帧头 byte4(t[4]) = tripMode（APP 提交行程时置 1，使其成为"行程"而非临时值）
    return build(18, body, extra=(lambda t: t.__setitem__(4, 1)) if trip_mode else None)


def cmd19(trips, cmd=19):
    body = []
    for i in range(10):
        t = trips[i] if i < len(trips) else {}
        body += [t.get(k, 0) for k in TRIP_S]
    return build(cmd, body)


def cmd_follow(cmd, followType, channel=0, grade=0):
    return build(cmd, [followType & 0xFF, channel & 0xFF, grade & 0xFF])


def le16(p, off):
    return p[off] | (p[off + 1] << 8)


class Bridge:
    def __init__(self):
        self._stop = threading.Event()
        self.alive = False
        self.q = queue.Queue()
        self.ws = None
        self.online = False
        self.st = {"on": {"master": None, "spot": None, "atmo": None},
                   "mode": {"master": 0, "spot": 0, "atmo": 0},
                   "ch": dict(DEFAULT_CH),
                   "mood": {"spot": 50, "atmo": 58},
                   "follow": {"spot": 0, "atmo": 0},
                   "fade": {("master", "on"): 60, ("master", "off"): 60,
                            ("spot", "on"): 60, ("spot", "off"): 60,
                            ("atmo", "on"): 60, ("atmo", "off"): 60},
                   "grade": {"master": 4, "spot": 0, "atmo": 0},
                   "cfg": {"master": {"on2": 60, "off2": 60, "modeId": 1},
                           "spot": {"on2": 0, "off2": 1},
                           "atmo": {"on2": 0, "off2": 1}},
                   "trips": {"master": None, "spot": None, "atmo": None},
                   "sunmoon": {"disable": 0, "sun": 20, "moon": 20},
                   "preset": None,
                   "mode_id": 0,
                   "raw18": None}
        self.mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "netlea-bridge")
        self.mq.username_pw_set(MQ["username"], MQ["password"])
        self.mq.on_connect = self._on_connect
        self.mq.on_message = self._on_message
        self.mq.will_set(T_AVAIL, "offline", retain=True)

    # ---------- MQTT ----------
    def _on_connect(self, c, u, flags, rc, props=None):
        print("[mqtt] rc=%s" % rc, flush=True)
        if rc != 0:
            return
        subs = ([T_MCMD, T_SCMD, T_ACMD, T_FCMD, T_SFCMD, T_DBG_SEND, T_PSET]
                + list(T_CHCMD.values()) + list(T_GRADE_SET.values())
                + list(T_TRIP_SET.values()) + list(FADE_SET.values())
                + [T_SUNMD_SET, T_SUN_SET, T_MOON_SET])
        for t in subs:
            c.subscribe(t)
        self.publish_discovery()
        c.publish(T_AVAIL, "online" if self.online else "offline", retain=True)

    def _on_message(self, c, u, msg):
        p = msg.payload.decode(errors="ignore").strip()
        print("[mqtt<-] %s %s" % (msg.topic, p[:120]), flush=True)
        self.q.put((msg.topic, p))

    def publish_discovery(self):
        dev = {"identifiers": ["netlea_" + DEV], "name": "尼特利水族时光",
               "manufacturer": "Netlea", "model": "水族时光 WiFi", "sw_version": "bridge v7"}

        def pub(comp, oid, cfg):
            cfg.update({"unique_id": oid, "availability_topic": T_AVAIL, "device": dev})
            self.mq.publish("homeassistant/%s/%s/config" % (comp, oid),
                            json.dumps(cfg, ensure_ascii=False), retain=True)

        pub("light", "netlea_tank_light", {
            "name": "主灯", "schema": "json",
            "command_topic": T_MCMD, "state_topic": T_MSTATE,
            "supported_color_modes": ["rgb"], "color_mode": True,
            "icon": "mdi:lightbulb-spot"})
        pub("light", "netlea_tank_spot", {
            "name": "射灯", "schema": "json", "brightness": True, "brightness_scale": 100,
            "command_topic": T_SCMD, "state_topic": T_SSTATE})
        pub("light", "netlea_tank_atmo", {
            "name": "氛围灯", "schema": "json", "brightness": True, "brightness_scale": 100,
            "command_topic": T_ACMD, "state_topic": T_ASTATE})
        pub("select", "netlea_tank_atmo_follow", {
            "name": "氛围灯跟随模式", "options": FOLLOW_OPTS,
            "command_topic": T_FCMD, "state_topic": T_FSTATE, "icon": "mdi:link-variant"})
        pub("select", "netlea_tank_spot_follow", {
            "name": "射灯跟随模式", "options": FOLLOW_OPTS,
            "command_topic": T_SFCMD, "state_topic": T_SFSTATE, "icon": "mdi:link-variant"})
        pub("select", "netlea_master_preset", {
            "name": "主灯光谱预设", "options": PRESET_NAMES,
            "command_topic": T_PSET, "state_topic": T_PSTATE, "icon": "mdi:palette-swatch"})
        pub("switch", "netlea_master_sunmoon", {
            "name": "主灯日出日落渐变", "command_topic": T_SUNMD_SET, "state_topic": T_SUNMD_ST,
            "payload_on": "ON", "payload_off": "OFF", "icon": "mdi:weather-sunset-up"})
        pub("number", "netlea_master_sunrise_fade", {
            "name": "主灯日出渐变时长", "min": 0, "max": 300, "step": 1,
            "unit_of_measurement": "min", "mode": "box",
            "command_topic": T_SUN_SET, "state_topic": T_SUN_ST, "icon": "mdi:weather-sunset-up"})
        pub("number", "netlea_master_sunset_fade", {
            "name": "主灯日落渐变时长", "min": 0, "max": 300, "step": 1,
            "unit_of_measurement": "min", "mode": "box",
            "command_topic": T_MOON_SET, "state_topic": T_MOON_ST, "icon": "mdi:weather-sunset-down"})
        for _k, _label in (("master", "主灯"), ("spot", "射灯"), ("atmo", "氛围灯")):
            for _d, _dn in (("on", "开灯渐变"), ("off", "关灯渐变")):
                pub("number", "netlea_%s_fade_%s" % (_k, _d), {
                    "name": "%s%s时长" % (_label, _dn), "min": 0, "max": 300, "step": 1,
                    "unit_of_measurement": "min", "mode": "box",
                    "command_topic": FADE_SET[(_k, _d)], "state_topic": FADE[(_k, _d)],
                    "icon": "mdi:transition", "entity_category": "config"})
        CH_LABEL = {"R": "主灯光谱·红 R", "G": "主灯光谱·绿 G", "W": "主灯光谱·白 W",
                    "B": "主灯光谱·蓝 B"}
        for c in ("R", "G", "W", "B"):
            pub("number", "netlea_master_%s" % c.lower(), {
                "name": CH_LABEL[c], "min": 0, "max": DEV_MAX,
                "step": 1, "mode": "box",
                "command_topic": T_CHCMD[c], "state_topic": T_CHSTATE[c],
                "icon": "mdi:palette"})
        # 风扇只读：转速由功率档/预设推导，禁止手动调低（低风扇 + 高功率会烧灯）
        pub("sensor", "netlea_master_fan_gear", {
            "name": "主灯风扇档位", "unit_of_measurement": "档",
            "state_topic": T_CHSTATE["F"], "icon": "mdi:fan",
            "entity_category": "diagnostic"})
        # 功率档（= 设备里的 grade，1..各预设档数），换档即整套下发光谱，和 APP 拖"功率"完全一致
        pub("number", "netlea_master_grade", {
            "name": "主灯功率档", "min": 1, "max": 20, "step": 1, "mode": "slider",
            "command_topic": T_GRADE_SET["master"], "state_topic": T_GRADE["master"],
            "icon": "mdi:gauge"})
        pub("sensor", "netlea_master_power", {
            "name": "主灯功率", "unit_of_measurement": "W",
            "state_topic": T_POWER, "icon": "mdi:flash", "entity_category": "diagnostic"})
        for k, label in (("master", "主灯"), ("spot", "射灯"), ("atmo", "氛围灯")):
            # grade 实测只是"档位编号"，写它不改变任何输出，已不在 HA 暴露
            pub("sensor", "netlea_%s_mode" % k, {
                "name": "%s运行模式" % label, "state_topic": T_MODE[k],
                "icon": "mdi:cog", "entity_category": "diagnostic"})
        print("[mqtt->] discovery published (25 实体)", flush=True)

    def pub_all(self):
        s = self.st
        if s["on"]["master"] is not None:
            ch = s["ch"]
            r, g, b = ch_to_rgb(ch)
            self.mq.publish(T_MSTATE, json.dumps(
                {"state": "ON" if s["on"]["master"] else "OFF",
                 "color_mode": "rgb", "color": {"r": r, "g": g, "b": b}}), retain=True)
        for c in CHS:
            v = f_gear(s["ch"][c]) if c == "F" else s["ch"][c]
            self.mq.publish(T_CHSTATE[c], str(v), retain=True)
        for k in ("spot", "atmo"):
            if s["on"][k] is not None:
                self.mq.publish(T_SSTATE if k == "spot" else T_ASTATE,
                                json.dumps({"state": "ON" if s["on"][k] else "OFF",
                                            "brightness": s["mood"][k]}), retain=True)
            self.mq.publish(T_GRADE[k], str(s["grade"][k]), retain=True)
        self.mq.publish(T_GRADE["master"], str(s["grade"]["master"]), retain=True)
        for k in ("master", "spot", "atmo"):
            self.mq.publish(T_MODE[k], MODE_OPTS.get(s["mode"][k], str(s["mode"][k])), retain=True)
        self.mq.publish(T_FSTATE, FOLLOW_OPTS[min(s["follow"]["atmo"], 2)], retain=True)
        self.mq.publish(T_SFSTATE, FOLLOW_OPTS[min(s["follow"]["spot"], 2)], retain=True)
        if s["preset"]:
            self.mq.publish(T_PSTATE, s["preset"], retain=True)
        mid, gd = s.get("mode_id", 0), s["grade"]["master"]
        lv = level_list(mid)
        if lv:
            n = max(1, min(len(lv), int(gd) or 1))
            self.mq.publish(T_POWER, str(lv[n - 1][5]), retain=True)
        for (k, d), val in s["fade"].items():
            self.mq.publish(FADE[(k, d)], str(val), retain=True)
        sm = s["sunmoon"]
        self.mq.publish(T_SUNMD_ST, "ON" if sm["disable"] else "OFF", retain=True)
        self.mq.publish(T_SUN_ST, str(sm["sun"]), retain=True)
        self.mq.publish(T_MOON_ST, str(sm["moon"]), retain=True)

    # ---------- WS ----------
    def connect_ws(self):
        self.ws = websocket.create_connection(
            WS_URL, header=["token: " + TOK, "contentType: application/json"], timeout=10)
        self.ws.send(json.dumps({"msgType": "1001", "msgBody": None}))
        print("[ws] connected", flush=True)

    def send(self, frame, note=""):
        self.ws.send(json.dumps({"msgType": "1002", "msgBody": {
            "time": int(time.time() * 1000), "all": "1",
            "topic": "App/Control/" + DEV, "message": frame}}))
        print("[ws->] %s %s" % (frame[:90], note), flush=True)

    def read_all_cfg(self):
        self.send(build(21, []), "查询状态")
        self.send(build(27, [0]), "读主灯配置")
        self.send(build(29, [0]), "读射灯配置")
        self.send(build(33, [0]), "读氛围灯配置")
        self.send(build(37, [7]), "读三类行程 (bit0/1/2)")

    def handle(self, raw):
        try:
            o = json.loads(raw)
        except Exception:
            return
        t, body = o.get("msgType"), o.get("msgBody")
        if t == "1001" and isinstance(body, list):
            self.online = True
            self.mq.publish(T_AVAIL, "online", retain=True)
            print("[ws] online devices=%s" % body, flush=True)
            return
        if t != "1002" or not isinstance(body, dict) or "message" not in body:
            return
        hx = body["message"]
        b = [int(hx[i:i + 2], 16) for i in range(0, len(hx), 2)]
        cmd, p = b[3], b[6:-1]
        self.mq.publish(T_DBG_RAW, json.dumps({"cmd": cmd, "hex": hx}), retain=True)

        def tn(keys, off):
            return {k: p[off + i] for i, k in enumerate(keys)}

        if cmd == 20:
            m, s, a = tn(MASTER, 0), tn(GRP, 13), tn(GRP, 22)
            print("[ws<-20] 主灯 on=%d mode=%d R%d G%d W%d B%d F%d | 射灯 on=%d mood=%d | 氛围 on=%d mood=%d follow=%d"
                  % (m["lampState"], m["lampMode"], m["R"], m["G"], m["W"], m["B"], m["F"],
                     s["lampState"], s["moodLamp"], a["lampState"], a["moodLamp"],
                     a["followType"]), flush=True)
            self.st["on"]["master"] = bool(m["lampState"])
            self.st["mode"]["master"] = m["lampMode"]
            if m["lampState"]:
                self.st["ch"] = {c: m[c] for c in CHS}
            self.st["on"]["spot"] = bool(s["lampState"])
            self.st["mode"]["spot"] = s["lampMode"]
            self.st["follow"]["spot"] = s["followType"]
            if s["moodLamp"]:
                self.st["mood"]["spot"] = s["moodLamp"]
            self.st["on"]["atmo"] = bool(a["lampState"])
            self.st["mode"]["atmo"] = a["lampMode"]
            self.st["follow"]["atmo"] = a["followType"]
            if a["moodLamp"]:
                self.st["mood"]["atmo"] = a["moodLamp"]
            self.mq.publish(T_DBG_STATE, json.dumps(
                {"master": m, "spot": s, "atmosphere": a}, ensure_ascii=False), retain=True)
        elif cmd == 26:
            self.st["grade"]["master"] = le16(p, 10)
            self.st["cfg"]["master"] = {"on2": le16(p, 0), "off2": le16(p, 2), "modeId": p[9]}
            # 设备存的 modeId 即当前预设（0-11 预设 / 12 自定义），用它同步 select 状态
            mid = p[9]
            self.st["mode_id"] = mid
            if 0 <= mid <= 11:
                self.st["preset"] = PRESET_NAMES[mid]
            elif mid == 12:
                self.st["preset"] = PRESET_NAMES[-1]
            self.st["fade"][("master", "on")] = le16(p, 0)
            self.st["fade"][("master", "off")] = le16(p, 2)
            print("[ws<-26] 主灯 modeId=%d grade=%d 开灯渐变=%d 关灯渐变=%d"
                  % (p[9], le16(p, 10), le16(p, 0), le16(p, 2)), flush=True)
        elif cmd == 28:
            self.st["grade"]["spot"] = p[5] if len(p) > 5 else 0
            self.st["cfg"]["spot"] = {"on2": le16(p, 0), "off2": le16(p, 2)}
            self.st["fade"][("spot", "on")] = le16(p, 0)
            self.st["fade"][("spot", "off")] = le16(p, 2)
            print("[ws<-28] 射灯 mood=%d grade=%d 渐变=%d/%d"
                  % (p[4], p[5], le16(p, 0), le16(p, 2)), flush=True)
        elif cmd == 32:
            self.st["grade"]["atmo"] = p[5] if len(p) > 5 else 0
            self.st["cfg"]["atmo"] = {"on2": le16(p, 0), "off2": le16(p, 2)}
            self.st["fade"][("atmo", "on")] = le16(p, 0)
            self.st["fade"][("atmo", "off")] = le16(p, 2)
            print("[ws<-32] 氛围 mood=%d grade=%d 渐变=%d/%d"
                  % (p[4], p[5], le16(p, 0), le16(p, 2)), flush=True)
        elif cmd == 18:
            self.st["raw18"] = list(p)
            self.st["sunmoon"] = {"disable": p[0], "sun": le16(p, 1), "moon": le16(p, 3)}
            trips, off = [], 5
            for i in range(10):
                t = dict(tn(TRIP_M, off))
                t["grade"] = le16(p, off + len(TRIP_M))
                off += len(TRIP_M) + 2
                if any(t.values()):
                    t["start"] = "%02d:%02d" % (t["startH"], t["startM"])
                    t["end"] = "%02d:%02d" % (t["endH"], t["endM"])
                    t["enabled"] = bool(t["tripDisable"])
                    trips.append(t)
            self.st["trips"]["master"] = {"summary": "%d 段" % len(trips) if trips else "未设置",
                                          "trips": trips}
            print("[ws<-18] 主灯行程 %d 段 日出/日落=%d/%d 启用=%d"
                  % (len(trips), le16(p, 1), le16(p, 3), p[0]), flush=True)
        elif cmd in (19, 31):
            trips, off = [], 0
            for i in range(10):
                t = dict(tn(TRIP_S, off))
                off += len(TRIP_S)
                if any(t.values()):
                    t["start"] = "%02d:%02d" % (t["startH"], t["startM"])
                    t["end"] = "%02d:%02d" % (t["endH"], t["endM"])
                    trips.append(t)
            grp = "spot" if cmd == 19 else "atmo"
            self.st["trips"][grp] = {"summary": "%d 段" % len(trips) if trips else "未设置",
                                     "trips": trips}
            print("[ws<-%d] %s行程 %d 段" % (cmd, "射灯" if grp == "spot" else "氛围灯",
                                            len(trips)), flush=True)
        else:
            print("[ws<-] cmd=%d %s" % (cmd, hx[:80]), flush=True)
        self.pub_all()

    # ---------- 命令 ----------
    def do(self, topic, p):
        try:
            if topic == T_DBG_SEND:
                self.send(p.upper(), "debug")
            elif topic == T_MCMD:
                self.on_light_json(p)
            elif topic in (T_SCMD, T_ACMD):
                j = json.loads(p)
                on = j["state"].upper() == "ON" if "state" in j else True
                if topic == T_SCMD:
                    self.set_grp("spot", on, j.get("brightness"))
                else:
                    self.set_grp("atmo", on, j.get("brightness"))
            elif topic == T_PSET:
                self.set_preset(p)
            elif topic == T_FCMD:
                if p in FOLLOW_OPTS:
                    idx = FOLLOW_OPTS.index(p)
                    self.send(cmd_follow(34, idx, 0, 50), "氛围灯跟随=%d" % idx)
                    self.st["follow"]["atmo"] = idx
                    self.send(build(33, [0]), "读回氛围灯配置")
            elif topic == T_SFCMD:
                if p in FOLLOW_OPTS:
                    idx = FOLLOW_OPTS.index(p)
                    self.send(cmd_follow(22, idx, 0, self.st["grade"]["spot"]),
                              "射灯跟随=%d" % idx)
                    self.st["follow"]["spot"] = idx
                    self.send(build(29, [0]), "读回射灯配置")
            elif topic in FADE_SET.values():
                k, d = [x for x, t in FADE_SET.items() if t == topic][0]
                self.set_fade(k, d, int(float(p)))
            elif topic in (T_SUNMD_SET, T_SUN_SET, T_MOON_SET):
                sm = dict(self.st["sunmoon"])
                if topic == T_SUNMD_SET:
                    sm["disable"] = 1 if p.upper() == "ON" else 0
                elif topic == T_SUN_SET:
                    sm["sun"] = max(0, min(300, int(float(p))))
                else:
                    sm["moon"] = max(0, min(300, int(float(p))))
                self.st["sunmoon"] = sm
                self.send_sunmoon()
            elif topic in T_GRADE_SET.values():
                k = [x for x, t in T_GRADE_SET.items() if t == topic][0]
                self.set_grade(k, int(float(p)))
            elif topic in T_TRIP_SET.values():
                k = [x for x, t in T_TRIP_SET.items() if t == topic][0]
                self.set_trips(k, json.loads(p))
            else:
                for c in CHS:
                    if topic == T_CHCMD[c]:
                        self.set_channel(c, int(float(p)))
        except Exception as e:
            print("[cmd err] %s %r" % (topic, e), flush=True)

    def set_preset(self, name):
        """选中预设：按原厂表取该预设当前档位的光谱值下发（modeId + grade 一并写入）"""
        if name not in PRESET_NAMES:
            return
        if name.startswith("自定义"):
            self.st["preset"] = name
            self.st["mode_id"] = 12
            self.send(cmd16(1, 0, self.st["ch"], 12, self.st["grade"]["master"]),
                      "自定义色 %s" % self.st["ch"])
            return
        i = PRESET_NAMES.index(name)
        lv = level_list(i)
        n = int(self.st["grade"]["master"]) or DEFAULT_LEVEL.get(i, 1)
        n = max(1, min(len(lv), n))
        R, G, W, B, fg, P = level_values(i, n)
        # 风扇档必须跟着该档位走（厂商为该光谱配的散热转速），否则高档位会过热
        self.st["ch"] = {"R": R, "G": G, "W": W, "B": B, "F": gear_pwm(fg)}
        self._fan_guard(self.st["ch"])
        self.st["preset"] = name
        self.st["mode_id"] = i
        self.st["grade"]["master"] = n
        self.send(cmd16(1, 0, self.st["ch"], i, n),
                  "预设 %s 第%d/%d档 %dW (风扇%d档)" % (name, n, len(lv), P, f_gear(self.st["ch"]["F"])))
        # cmd16 只改实时输出，设备不落盘；档位必须走 cmd26 才持久（否则设备回读仍是旧档）
        self.write_cfg("master", grade=n)
        self.pub_all()          # v14：下发后立刻刷新 功率/风扇/预设 实体

    def on_light_json(self, raw):
        """主灯光源：开关 + 打开 HA 的色谱圆盘（rgb）。给颜色时按 R,W,G,B 四通道换算下发"""
        try:
            j = json.loads(raw)
        except Exception:
            j = {}
        if isinstance(j, dict) and isinstance(j.get("color"), dict):
            c = j["color"]
            ch = rgb_to_ch(c.get("r", 255), c.get("g", 255), c.get("b", 255), self.st["ch"])
            on = str(j.get("state", "ON")).upper() != "OFF"
            self.st["ch"] = ch
            self.st["on"]["master"] = on
            self.st["mode_id"] = 12
            self.st["preset"] = PRESET_NAMES[-1]
            self.send(cmd16(1 if on else 0, 0, ch if on else {}, 12,
                            self.st["grade"]["master"]),
                      "色谱选色 rgb(%d,%d,%d) -> R%d W%d G%d B%d" % (
                          c.get("r", 0), c.get("g", 0), c.get("b", 0),
                          ch["R"], ch["W"], ch["G"], ch["B"]))
            self.throttled_cfg()
            return
        on = str(j.get("state", "ON")).upper() == "ON" if isinstance(j, dict) else str(raw).upper().strip() == "ON"
        ch = dict(self.st["ch"])
        total = ch["R"] + ch["G"] + ch["W"] + ch["B"]
        need = fan_min_pwm(total)
        if need > ch["F"]:
            ch["F"] = need
        self.st["ch"] = ch
        self.st["on"]["master"] = on
        self.send(cmd16(1 if on else 0, 0, ch if on else {},
                        int(self.st.get("mode_id", 0) or 0), self.st["grade"]["master"]),
                  "主灯 %s (通道 %s, 风扇PWM=%d)" % ("ON" if on else "OFF", ch, ch["F"]))

    def throttled_cfg(self, gap=1.0):
        """拖色谱时每帧都写 cmd26 太吵，1 秒最多落盘一次（最后一次一定会落）"""
        now = time.time()
        if now - getattr(self, "_last_cfg", 0) >= gap:
            self._last_cfg = now
            self.write_cfg("master")

    def set_master(self, on):
        ch = self.st["ch"] if any(self.st["ch"].values()) else DEFAULT_CH
        self.send(cmd16(1 if on else 0, 0, ch if on else {},
                        int(self.st.get("mode_id", 0) or 0), self.st["grade"]["master"]),
                  "主灯 %s" % ("ON" if on else "OFF"))
        self.st["on"]["master"] = on
        self.pub_all()

    def set_grp(self, k, on, bright):
        mood = self.st["mood"][k] if bright is None else int(bright)
        cmd = 17 if k == "spot" else 30
        if k == "atmo" and self.st["follow"]["atmo"] != 0:
            self.send(build(34, [0, 0, 50]), "先把氛围灯切成手动")
            time.sleep(0.3)
        self.send(grp_cmd(cmd, 1 if on else 0, 0, mood if on else 0,
                          self.st["grade"][k]), "%s %s mood=%d" % (k, "ON" if on else "OFF", mood))

    def _fan_guard(self, ch):
        """风扇只能由功率/预设推导：按四通道总和给最低散热转速，绝不接受手动降低"""
        total = ch["R"] + ch["G"] + ch["W"] + ch["B"]
        need = fan_min_pwm(total)
        if need > ch["F"]:
            print("[fan] 四通总和 %d → 风扇强制升到 PWM %d（防低风扇高功率）" % (total, need),
                  flush=True)
            ch["F"] = need
        return ch

    def set_channel(self, c, val):
        if c == "F":
            print("[fan] 手动调风扇已禁用：档位跟随功率/预设", flush=True)
            return
        ch = dict(self.st["ch"])
        ch[c] = max(0, min(DEV_MAX, int(val)))
        total = ch["R"] + ch["G"] + ch["W"] + ch["B"]
        if total > SAFE_TOTAL:
            k = SAFE_TOTAL / total
            for x in ("R", "G", "W", "B"):
                ch[x] = int(ch[x] * k)
            print("[limit] 四通总和 %d 超 %d，按比例压到 %d" % (
                total, SAFE_TOTAL, ch["R"] + ch["G"] + ch["W"] + ch["B"]), flush=True)
        self._fan_guard(ch)
        self.st["ch"] = ch
        # 动四通道 = 走"自定义色"（和 APP 一致：滑条一动就切到自定义）
        self.st["mode_id"] = 12
        self.st["preset"] = PRESET_NAMES[-1]
        self.send(cmd16(1 if self.st["on"]["master"] else 0, 0, ch, 12,
                        self.st["grade"]["master"]), "光谱 %s=%d（自定义）" % (c, ch[c]))
        # 同预设一样：cmd16 不落盘，modeId=12 必须写进 cmd26，否则设备回读仍是旧预设
        self.write_cfg("master")
        self.pub_all()          # v14：下发后立刻刷新 功率/风扇/预设 实体

    def write_cfg(self, k, grade=None, on2=None, off2=None):
        """配置写入唯一通道：cmd26 主灯 / cmd28 射灯 / cmd32 氛围灯
        （cmd16/17/30 改不动 grade；写入后立刻读回校验）"""
        c = self.st["cfg"][k]
        on2 = c["on2"] if on2 is None else on2
        off2 = c["off2"] if off2 is None else off2
        if grade is None:
            grade = self.st["grade"][k]
        if k == "master":
            modeId = int(self.st.get("mode_id", 0) or 0)
            body = [on2 & 0xFF, on2 >> 8, off2 & 0xFF, off2 >> 8]
            body += [self.st["ch"][x] for x in SER] + [modeId, grade & 0xFF, (grade >> 8) & 0xFF]
            self.send(build(26, body),
                      "主灯写入 grade=%d 渐变=%d/%d (cmd26)" % (grade, on2, off2))
            self.send(build(27, [0]), "读回主灯配置")
        else:
            cmd, read = (28, 29) if k == "spot" else (32, 33)
            mood = self.st["mood"][k]
            body = [on2 & 0xFF, on2 >> 8, off2 & 0xFF, off2 >> 8, mood & 0xFF, grade & 0xFF]
            self.send(build(cmd, body),
                      "%s写入 mood=%d grade=%d 渐变=%d/%d (cmd%d)" % (k, mood, grade, on2, off2, cmd))
            self.send(build(read, [0]), "读回%s配置" % k)

    def set_grade(self, k, val):
        """主灯功率档：不是单纯记个号，而是按当前预设的原厂表整个档位下发"""
        if k != "master":
            self.write_cfg(k, grade=max(0, int(val)))
            return
        mid = int(self.st.get("mode_id", 0) or 0)
        lv = level_list(mid)
        if not lv:                    # 自定义色没有档位表，只记录
            self.write_cfg("master", grade=max(0, int(val)))
            return
        n = max(1, min(len(lv), int(val)))
        R, G, W, B, fg, P = level_values(mid, n)
        self.st["ch"] = {"R": R, "G": G, "W": W, "B": B, "F": gear_pwm(fg)}
        self._fan_guard(self.st["ch"])
        self.st["grade"]["master"] = n
        self.st["preset"] = PRESET_NAMES[mid]
        self.send(cmd16(1 if self.st["on"]["master"] else 0, 0, self.st["ch"], mid, n),
                  "%s 第%d/%d档 %dW (风扇%d档)" % (PRESET_NAMES[mid], n, len(lv), P, f_gear(self.st["ch"]["F"])))
        self.write_cfg("master", grade=n)
        self.pub_all()          # v14：下发后立刻刷新 功率/风扇/预设 实体

    def set_fade(self, k, d, val):
        """开灯渐变(on2) / 关灯渐变(off2)，单位分钟 1-300"""
        val = max(1, min(300, int(val)))
        if d == "on":
            self.write_cfg(k, on2=val)
        else:
            self.write_cfg(k, off2=val)

    def send_sunmoon(self):
        """日出/日落渐变写在 cmd18 帧头，必须连同现有 10 段行程一起整帧重发"""
        sm = self.st["sunmoon"]
        raw = self.st["raw18"]
        head = [sm["disable"] & 1, sm["sun"] & 0xFF, (sm["sun"] >> 8) & 0xFF,
                sm["moon"] & 0xFF, (sm["moon"] >> 8) & 0xFF]
        tail = raw[5:] if raw and len(raw) >= 5 else [0] * 140
        self.send(build(18, head + list(tail)),
                  "日出/日落渐变=%d/%d 启用=%d" % (sm["sun"], sm["moon"], sm["disable"]))
        self.send(build(37, [1]), "读回主灯行程")

    def set_trips(self, k, payload):
        trips = payload.get("trips", [])
        if k == "master":
            self.send(cmd18(payload.get("sunMoonDisable", 0), payload.get("sunTime", 0),
                            payload.get("moonTime", 0), trips,
                            payload.get("tripMode", 0)), "写主灯行程")
        elif k == "spot":
            self.send(cmd19(trips, 19), "写射灯行程")
        else:
            self.send(cmd19(trips, 31), "写氛围灯行程")
        self.send(build(37, [7]), "读回三类行程")

    # ---------- 主循环 ----------
    def stop(self):
        self.alive = False
        self._stop.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        try:
            self.mq.publish(T_AVAIL, "offline", retain=True)
            self.mq.loop_stop()
            self.mq.disconnect()
        except Exception:
            pass

    def run(self):
        if not (TOK and DEV and MQ.get("host")):
            print("[bridge] 配置不完整，等待网页配置（token/device_id/mqtt）", flush=True)
            return
        print("[bridge] 启动 dev=%s mqtt=%s" % (DEV, MQ.get("host")), flush=True)
        self.alive = True
        try:
            self.mq.connect(MQ["host"], MQ["port"], 30)
            self.mq.loop_start()
        except Exception as e:
            print("[mqtt] 连接失败 %r" % e, flush=True)
            self.alive = False
            return
        last = 0
        while not self._stop.is_set():
            try:
                if self.ws is None:
                    self.connect_ws()
                    self.read_all_cfg()
                    last = time.time()
                while True:
                    try:
                        self.do(*self.q.get_nowait())
                    except queue.Empty:
                        break
                if time.time() - last > STEP_SEC:
                    self.send(build(21, []), "轮询状态")
                    last = time.time()
                self.ws.settimeout(2)
                try:
                    raw = self.ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if raw:
                    self.handle(raw)
            except Exception as e:
                print("[err] %r" % e, flush=True)
                traceback.print_exc()
                try:
                    self.ws.close()
                except Exception:
                    pass
                self.ws = None
                self.online = False
                self.mq.publish(T_AVAIL, "offline", retain=True)
                time.sleep(5)


WEB_ROOT = os.environ.get("NETLEA_WWW", "/app/www")
V3API = "https://v3api.netlea.com/prod-api"
DEFAULT_WS = "ws://v3grpc.netlea.com:8088/websocket"

_BRIDGE = None
_BRIDGE_LOCK = threading.Lock()


# ---------- 厂商云 HTTP 代理（浏览器直连会被 CORS 挡掉，必须服务端转） ----------
def v3_call(method, path, token=None, body=None, timeout=20):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(V3API + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", token)
    try:
        txt = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
    except Exception as e:
        return {"code": -1, "msg": "网络失败: %r" % e}
    try:
        return json.loads(txt)
    except Exception:
        return {"code": -1, "msg": txt[:300]}


# ---------- 桥接生命周期（配置保存后热启动，无需重启容器） ----------
def bridge_get():
    return _BRIDGE


def apply_cfg(cfg):
    global CFG, WS_URL, TOK, DEV, MQ
    CFG = cfg
    WS_URL = cfg.get("ws_url") or DEFAULT_WS
    TOK = cfg.get("token", "")
    DEV = cfg.get("device_id", "")
    MQ = cfg.get("mqtt") or {"host": "", "port": 1883, "username": "",
                             "password": "", "prefix": "homeassistant"}


def bridge_stop():
    global _BRIDGE
    with _BRIDGE_LOCK:
        b, _BRIDGE = _BRIDGE, None
    if b:
        try:
            b.stop()
        except Exception:
            pass
        print("[bridge] 已停止", flush=True)
    return b


def bridge_start():
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is not None:
            return _BRIDGE
        b = Bridge()
        _BRIDGE = b
    threading.Thread(target=b.run, daemon=True, name="bridge").start()
    return b


def _ready():
    return bool(TOK and DEV and (MQ or {}).get("host") and CFG.get("accept_risk") is True)


def _status():
    b = _BRIDGE
    st = {}
    if b:
        st = {"online": bool(b.online),
              "on": b.st["on"]["master"],
              "grade": b.st["grade"]["master"],
              "preset": b.st.get("preset"),
              "mode_id": b.st.get("mode_id"),
              "ch": dict(b.st["ch"]),
              "fan_gear": f_gear(b.st["ch"].get("F", 0))}
    running = _BRIDGE is not None and getattr(_BRIDGE, "alive", False)
    return {"configured": _ready(), "running": running,
            "accept_risk": CFG.get("accept_risk") is True,
            "device_id": DEV, "token_set": bool(TOK),
            "mqtt_host": (MQ or {}).get("host"), "mqtt_port": (MQ or {}).get("port"),
            "presets": PRESET_NAMES, "state": st}


def _save_cfg(cfg):
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG_PATH)


def _handle_cmd(d):
    b = _BRIDGE
    if not b:
        return {"ok": False, "msg": "桥接未启动（配置不完整或正在重连）"}
    act = d.get("action")
    try:
        if act == "on":
            b.set_master(True)
        elif act == "off":
            b.set_master(False)
        elif act == "preset":
            b.set_preset(d.get("name"))
        elif act == "grade":
            b.set_grade("master", int(d.get("value")))
        elif act == "fan":
            return {"ok": False, "msg": "风扇已改为跟随功率/预设，不支持手动设置"}
        elif act == "channel":
            b.set_channel(d.get("ch"), int(d.get("value")))
        else:
            return {"ok": False, "msg": "未知动作 %r" % act}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "msg": "%r" % e}
    return {"ok": True, "msg": "%s 已下发" % act}


class WebHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "netlea-bridge"

    def end_headers(self):
        # HA 前端与这里跨域（8123 vs 8099），ES module 必须带 CORS 头，否则整卡加载失败
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-cache, max-age=0")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/api/"):
            return self._api_get(p)
        if p in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/api/"):
            return self._api_post(p, self._read_json())
        self._json({"ok": False, "msg": "not found"}, 404)

    def _api_get(self, p):
        if p == "/api/status":
            return self._json(_status())
        return self._json({"ok": False, "msg": "not found"}, 404)

    def _api_post(self, p, d):
        if p == "/api/sms":
            phone = str(d.get("phone") or "").strip()
            if not phone:
                return self._json({"ok": False, "msg": "请填手机号"})
            r = v3_call("GET", "/V3/Api/captchaMsg?captchaMsgFlag=3&phonenumber="
                        + urllib.parse.quote(phone))
            ok = r.get("code") == 200 and r.get("uuid")
            return self._json({"ok": bool(ok), "uuid": r.get("uuid"),
                               "msg": r.get("msg")})

        if p == "/api/login":
            body = {"username": str(d.get("phone") or "").strip(),
                    "uuid": d.get("uuid") or "",
                    "code": str(d.get("code") or "").strip()}
            r = v3_call("POST", "/V3/Api/appuser/loginBycode", body=body)
            tok = r.get("token") or r.get("data", {}).get("token") if isinstance(r, dict) else None
            return self._json({"ok": bool(tok), "token": tok, "msg": r.get("msg"),
                               "raw": None if tok else r})

        if p == "/api/verify":   # 校验用户手填的 token 是否有效
            tok = str(d.get("token") or "").strip()
            if not tok:
                return self._json({"ok": False, "msg": "请填 token"})
            r = v3_call("GET", "/V3/Api/equipmentSysList", token=tok)
            return self._json({"ok": r.get("code") == 200,
                               "msg": r.get("msg") or ("token 有效" if r.get("code") == 200 else "token 无效"),
                               "total": r.get("total")})

        if p == "/api/save":
            if d.get("accept_risk") is not True:
                return self._json({"ok": False, "msg": "请先勾选「我已阅读并同意免责声明」，否则不能部署/启用"})
            cfg = {
                "accept_risk": True,
                "ws_url": str(d.get("ws_url") or DEFAULT_WS),
                "token": str(d.get("token") or "").strip(),
                "device_id": str(d.get("device_id") or "").strip().upper(),
                "mqtt": {
                    "host": str(d.get("mqtt_host") or "").strip(),
                    "port": int(d.get("mqtt_port") or 1883),
                    "username": str(d.get("mqtt_user") or ""),
                    "password": str(d.get("mqtt_pass") or ""),
                    "prefix": str(d.get("prefix") or "homeassistant"),
                },
            }
            if not (cfg["token"] and cfg["device_id"] and cfg["mqtt"]["host"]):
                return self._json({"ok": False, "msg": "token / 设备ID / MQTT 地址 都必填"})
            try:
                _save_cfg(cfg)
            except Exception as e:
                return self._json({"ok": False, "msg": "写配置失败: %r" % e})
            apply_cfg(cfg)
            bridge_stop()
            time.sleep(0.5)
            bridge_start()
            return self._json({"ok": True, "msg": "已保存并启动桥接"})

        if p == "/api/cmd":
            return self._json(_handle_cmd(d))

        if p == "/api/restart":
            cfg = dict(CFG)
            if not _ready():
                return self._json({"ok": False, "msg": "配置不完整"})
            apply_cfg(cfg)
            bridge_stop()
            time.sleep(0.5)
            bridge_start()
            return self._json({"ok": True, "msg": "桥接已重启"})

        return self._json({"ok": False, "msg": "not found"}, 404)


class WebServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_web(port=8099):
    """8099：配置向导 / 控制页 / API 代理 + HA 自定义卡片静态文件"""
    if not os.path.isdir(WEB_ROOT):
        os.makedirs(WEB_ROOT, exist_ok=True)
    handler = functools.partial(WebHandler, directory=WEB_ROOT)
    try:
        srv = WebServer(("0.0.0.0", port), handler)
    except Exception as e:
        print("[web] 启动失败 %r" % e, flush=True)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("[web] 配置/控制台 http://0.0.0.0:%d/ -> %s" % (port, WEB_ROOT), flush=True)


if __name__ == "__main__":
    print("=" * 68, flush=True)
    print("【免责声明】本项目为个人非官方逆向/桥接工具，与尼特利（Netlea）无关。", flush=True)
    print("使用本工具可能：影响灯具硬件（过载/过热/加速老化/损坏）、违反厂商服务条款")
    print("甚至导致账号或设备被限制/封禁、失去保修。所有风险与后果由使用者自行承担，")
    print("作者与贡献者不承担任何责任。仅在阅读并同意完整声明（www/disclaimer.html、")
    print("仓库 DISCLAIMER.md）后才可继续部署：网页向导里必须勾选同意才能启用桥接。")
    print("=" * 68, flush=True)
    start_web(int(os.environ.get("NETLEA_WEB_PORT", 8099)))
    if _ready():
        bridge_start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        print("[bridge] 未配置：打开 http://<NAS-IP>:8099/ 填验证码/设备/MQTT", flush=True)
        if CFG and CFG.get("accept_risk") is not True:
            print("[bridge] 注意：配置文件缺少「同意免责声明」标记，请重新在向导页勾选同意后保存", flush=True)
        while True:
            time.sleep(5)
            if _ready():
                bridge_start()
