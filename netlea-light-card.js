/* 尼特利主灯 · 一体化灯控卡（圆环 + 色谱 + 功率/风扇）
 * 用法：
 *   type: custom:netlea-light-card
 *   entity: light.netlea_tank_light          # 主灯光源（rgb）
 *   power_entity: sensor.ni_te_li_shui_zu_shi_guang_zhu_deng_gong_lu
 *   fan_entity: sensor.ni_te_li_shui_zu_shi_guang_zhu_deng_feng_shan_dang_wei  # 只读，风扇跟随功率
 *   preset_entity: select.netlea_master_preset
 *
 * 圆环：点圆心 = 开关；沿圆环拖动 = 明度（0-100%）
 * 彩虹条：色相；灰条：饱和
 * 底部：实时功率 / 风扇档位
 */
(function () {
  const TAG = "netlea-light-card";
  const R = 46;                 // 圆环半径（viewBox 单位）
  const C = 2 * Math.PI * R;    // 周长

  function hsv2rgb(h, s, v) {
    h = ((h % 360) + 360) % 360;
    s = Math.max(0, Math.min(1, s));
    v = Math.max(0, Math.min(1, v));
    const c = v * s;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = v - c;
    let r = 0, g = 0, b = 0;
    if (h < 60) { r = c; g = x; }
    else if (h < 120) { r = x; g = c; }
    else if (h < 180) { g = c; b = x; }
    else if (h < 240) { g = x; b = c; }
    else if (h < 300) { r = x; b = c; }
    else { r = c; b = x; }
    return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
  }

  function rgb2hsv(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    let h = 0;
    if (d !== 0) {
      if (mx === r) h = 60 * (((g - b) / d) % 6);
      else if (mx === g) h = 60 * ((b - r) / d + 2);
      else h = 60 * ((r - g) / d + 4);
    }
    if (h < 0) h += 360;
    return [h, mx === 0 ? 0 : d / mx, mx];
  }

  const CSS = [
    ":host{display:block;height:100%}",
    "ha-card.card{padding:10px 12px 9px;position:relative;display:flex;flex-direction:column;justify-content:space-between;height:100%;",
    "width:100%;max-width:100%;box-sizing:border-box;margin:0;overflow:hidden}",
    ".card > .hd{margin:0}",
    ".hd{display:flex;align-items:baseline;gap:8px}",
    ".hd .t{font-size:14px;font-weight:600;color:var(--primary-text-color)}",
    ".hd .p{margin-left:auto;font-size:11px;max-width:52%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--secondary-text-color)}",
    ".ring{position:relative;width:100%;display:flex;justify-content:center;margin:2px 0 0}",
    ".ring svg{width:100%;max-width:152px;aspect-ratio:1/1;height:auto;touch-action:none;cursor:pointer}",
    ".mid{position:absolute;left:0;right:0;top:0;bottom:0;display:flex;flex-direction:column;",
    "align-items:center;justify-content:center;pointer-events:none;gap:0}",
    ".pct{font-size:24px;font-weight:700;color:var(--primary-text-color);line-height:1.1}",
    ".watt{font-size:11px;color:var(--secondary-text-color);font-variant-numeric:tabular-nums}",
    ".onoff{font-size:10px;letter-spacing:.06em;color:var(--secondary-text-color);margin-top:1px}",
    ".row{display:flex;align-items:center;gap:6px;margin-top:7px}",
    ".lb{width:22px;font-size:10px;color:var(--secondary-text-color);text-align:right;flex:0 0 auto}",
    "input[type=range]{-webkit-appearance:none;appearance:none;flex:1 1 auto;height:17px;",
    "margin:0;background:transparent;outline:none;cursor:pointer}",
    "input[type=range]::-webkit-slider-runnable-track{height:17px;border-radius:5px}",
    "input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:13px;height:13px;",
    "margin-top:2px;border-radius:50%;background:#fff;border:2px solid rgba(0,0,0,.35);",
    "box-shadow:0 1px 2px rgba(0,0,0,.3)}",
    "input[type=range]::-moz-range-track{height:17px;border-radius:5px}",
    "input[type=range]::-moz-range-thumb{width:13px;height:13px;border-radius:50%;",
    "background:#fff;border:2px solid rgba(0,0,0,.35)}",
    "#hue::-webkit-slider-runnable-track{background:linear-gradient(to right,",
    "hsl(0,100%,50%),hsl(30,100%,50%),hsl(60,100%,50%),hsl(90,100%,50%),",
    "hsl(120,100%,50%),hsl(150,100%,50%),hsl(180,100%,50%),hsl(210,100%,50%),",
    "hsl(240,100%,50%),hsl(270,100%,50%),hsl(300,100%,50%),hsl(330,100%,50%),hsl(360,100%,50%))}",
    "#hue::-moz-range-track{background:linear-gradient(to right,",
    "hsl(0,100%,50%),hsl(30,100%,50%),hsl(60,100%,50%),hsl(90,100%,50%),",
    "hsl(120,100%,50%),hsl(150,100%,50%),hsl(180,100%,50%),hsl(210,100%,50%),",
    "hsl(240,100%,50%),hsl(270,100%,50%),hsl(300,100%,50%),hsl(330,100%,50%),hsl(360,100%,50%))}",
    ".sec{display:flex;align-items:center;gap:6px;margin:9px 0 4px;font-size:10px;white-space:nowrap;",
    "color:var(--secondary-text-color);letter-spacing:.04em}",
    ".psw{display:flex;flex-wrap:wrap;gap:5px;align-items:center}\n    #grp{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none}\n    #grp::-webkit-scrollbar{display:none}",
    ".psw+.psw{margin-top:5px}",
    "#itm{flex-wrap:nowrap;overflow-x:auto;padding-bottom:2px;scrollbar-width:none;",
    "-webkit-overflow-scrolling:touch}",
    "#itm::-webkit-scrollbar{display:none}",
    "#itm .pchip{flex:0 0 auto}",
    ".gchip{white-space:nowrap;flex:0 0 auto;border:none;background:none;font:inherit;font-size:12px;line-height:1.2;",
    "color:var(--secondary-text-color);padding:1px 1px;margin-right:7px;cursor:pointer;",
    "border-bottom:2px solid transparent;transition:color .15s,border-color .15s}",
    ".gchip.on{color:var(--primary-color,#0aa);border-bottom-color:var(--primary-color,#0aa);font-weight:600}",
    ".pchip{border:1px solid var(--divider-color,rgba(0,0,0,.15));background:transparent;",
    "color:var(--primary-text-color);font:inherit;font-size:12px;line-height:1.2;",
    "white-space:nowrap;padding:4px 9px;border-radius:8px;cursor:pointer;transition:background .15s,color .15s}",
    ".pchip:active{transform:scale(.97)}",
    ".pchip.on{background:var(--primary-color,#0aa);color:var(--text-primary-color,#fff);",
    "border-color:transparent;font-weight:600}",
    ".ft{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}",
    ".chip{flex:1 1 0;min-width:0;background:var(--secondary-background-color,rgba(0,0,0,.05));",
    "border-radius:8px;padding:4px 8px;display:flex;align-items:center;gap:4px}",
    ".chip ha-icon{--mdc-icon-size:15px;color:var(--secondary-text-color)}",
    ".chip .k{font-size:11px;color:var(--secondary-text-color);white-space:nowrap}",
    ".chip .v{font-size:13px;font-weight:600;color:var(--primary-text-color);",
    "font-variant-numeric:tabular-nums;margin-left:auto}",
  ].join("");

  class NetleaLightCard extends HTMLElement {
    constructor() {
      super();
      this._built = false;
      this._touchAt = 0;
      this._lastSend = 0;
      this._pending = null;
      this._dragging = false;
    }

    setConfig(config) {
      const c = config || {};
      this._entity = c.entity || "light.netlea_tank_light";
      this._powerEntity = c.power_entity || "sensor.ni_te_li_shui_zu_shi_guang_zhu_deng_gong_lu";
      this._fanEntity = c.fan_entity || "sensor.ni_te_li_shui_zu_shi_guang_zhu_deng_feng_shan_dang_wei";
      this._presetEntity = c.preset_entity || "select.netlea_master_preset";
      this._name = c.name || "主灯";
      if (this._built) this._paintAll();
    }

    set hass(h) {
      this._hass = h;
      if (!this._built) return;
      try {
        this._paintChips();
        if (!this._dragging && Date.now() - this._touchAt > 1500) this._syncFromState();
        else this._paintRing();
      } catch (err) {
        console.error("netlea-light-card:", err);
      }
    }

    getCardSize() { return 5; }

    connectedCallback() {
      if (this._built) return;
      const root = this.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = CSS;
      root.appendChild(style);

      const wrap = document.createElement("ha-card");
      wrap.className = "card";
      wrap.innerHTML =
        '<div class="hd"><span class="t"></span><span class="p" id="sub"></span></div>' +
        '<div class="ring"><svg viewBox="0 0 120 120" id="svg">' +
        '<circle id="track" cx="60" cy="60" r="' + R + '" fill="none" stroke-width="9"/>' +
        '<circle id="arc" cx="60" cy="60" r="' + R + '" fill="none" stroke-width="9" ' +
        'stroke-linecap="round"/></svg>' +
        '<div class="mid"><div class="pct" id="pct">--</div>' +
        '<div class="watt" id="watt"></div><div class="onoff" id="onoff"></div></div></div>' +
        '<div class="row"><span class="lb">色相</span><input id="hue" type="range" min="0" max="360" step="1" value="0"></div>' +
        '<div class="row"><span class="lb">饱和</span><input id="sat" type="range" min="0" max="100" step="1" value="100"></div>' +
        '<div class="sec">预设光谱</div>' +
        '<div class="psw" id="grp"></div><div class="psw" id="itm"></div>' +
        '<div class="ft">' +
        '<div class="chip"><ha-icon icon="mdi:flash"></ha-icon><span class="k">功率</span><span class="v" id="pw">-</span></div>' +
        '<div class="chip"><ha-icon icon="mdi:fan"></ha-icon><span class="k">风扇</span><span class="v" id="fan">-</span></div>' +
        '</div>';
      root.appendChild(wrap);
      this._root = root;

      const svg = this._$("svg");
      svg.addEventListener("pointerdown", (e) => this._onDown(e));
      svg.addEventListener("pointermove", (e) => this._onMove(e));
      svg.addEventListener("pointerup", (e) => this._onUp(e));
      svg.addEventListener("pointercancel", () => this._onUp(null));
      this._$("hue").addEventListener("input", () => this._onStrip());
      this._$("sat").addEventListener("input", () => this._onStrip());

      this._built = true;
      try { this._paintAll(); } catch (err) { console.error("netlea-light-card:", err); }
    }

    _$ (id) { return this._root.querySelector("#" + id); }

    _st(id) {
      return this._hass && this._hass.states ? this._hass.states[id] : null;
    }

    get _on() {
      const s = this._st(this._entity);
      return !!(s && s.state === "on");
    }

    get _hsv() {
      const s = this._st(this._entity);
      const rgb = s && s.attributes ? s.attributes.rgb_color : null;
      if (!rgb) return [0, 1, 1];
      return rgb2hsv(rgb[0], rgb[1], rgb[2]);
    }

    _paintAll() {
      this._paintChips();
      this._syncFromState();
    }

    _paintChips() {
      const p = this._st(this._powerEntity);
      this._$("pw").textContent = p && p.state !== "unknown" && p.state !== "unavailable" ? p.state + " W" : "-";
      const f = this._st(this._fanEntity);
      this._$("fan").textContent = f ? f.state + " 档" : "-";
      const ps = this._st(this._presetEntity);
      this._$("sub").textContent = ps ? ps.state : "";
      const nm = this._root.querySelector(".hd .t");
      nm.textContent = this._name;
      this._paintPresets(ps);
    }

    _paintPresets(ps) {
      const grpBox = this._$("grp");
      const itmBox = this._$("itm");
      if (!grpBox || !ps) return;
      const opts = (ps.attributes && ps.attributes.options) || [];
      const sig = opts.join("|");
      if (this._psSig !== sig) {                    // 选项变了才重建按钮
        this._psSig = sig;
        this._psMap = {};                           // {组: [{opt,label}]}
        opts.forEach((o) => {
          let g, lab;
          if (o.indexOf("·") >= 0) { g = o.split("·")[0]; lab = o.split("·")[1]; }
          else { g = "自定义"; lab = o.replace(/（.*$/, ""); }
          (this._psMap[g] = this._psMap[g] || []).push({ opt: o, label: lab });
        });
        grpBox.innerHTML = "";
        itmBox.innerHTML = "";
        Object.keys(this._psMap).forEach((g) => {
          const b = document.createElement("button");
          b.className = "gchip";
          b.dataset.grp = g;
          b.textContent = g;
          b.addEventListener("click", () => {
            const cur = this._st(this._presetEntity);
            const inside = cur && this._psMap[g].some((x) => x.opt === cur.state);
            this._grp = g;
            this._renderItems();
            if (!inside) this._pick(this._psMap[g][0].opt);   // 点组=切到该组第一个
          });
          grpBox.appendChild(b);
        });
        this._grp = this._groupOf(ps.state);
        this._renderItems();
      }
      if (!this._grp || !this._psMap[this._grp]) this._grp = this._groupOf(ps.state);
      if (this._psLast !== ps.state) {              // 状态被外部改（自动化/APP）时跟随到对应组
        this._psLast = ps.state;
        const g = this._groupOf(ps.state);
        if (g && g !== this._grp) { this._grp = g; this._renderItems(); }
      }
      const cur = ps.state;
      grpBox.querySelectorAll(".gchip").forEach((b) => {
        b.classList.toggle("on", b.dataset.grp === this._grp);
      });
      itmBox.querySelectorAll(".pchip").forEach((b) => {
        b.classList.toggle("on", b.dataset.opt === cur);
      });
    }

    _groupOf(state) {
      for (const g of Object.keys(this._psMap || {})) {
        if (this._psMap[g].some((x) => x.opt === state)) return g;
      }
      return Object.keys(this._psMap || {})[0];
    }

    _renderItems() {
      const box = this._$("itm");
      if (!box) return;
      box.innerHTML = "";
      (this._psMap[this._grp] || []).forEach((x) => {
        const b = document.createElement("button");
        b.className = "pchip";
        b.dataset.opt = x.opt;
        b.textContent = x.label;
        b.addEventListener("click", () => this._pick(x.opt));
        box.appendChild(b);
      });
    }

    _pick(opt) {
      this._touchAt = Date.now();
      this._hass.callService("select", "select_option", {
        entity_id: this._presetEntity,
        option: opt,
      });
    }

    _syncFromState() {
      const hsv = this._hsv;
      this._$("hue").value = Math.round(hsv[0]);
      this._$("sat").value = Math.round(hsv[1] * 100);
      this._paintRing();
    }

    _paintRing() {
      const hsv = this._hsv;
      const on = this._on;
      const v = hsv[2];
      const hue = hsv[0], s = hsv[1];
      const base = "hsl(" + hue + "," + Math.round(s * 100) + "%,52%)";
      const arc = this._$("arc");
      const len = Math.max(0.06, on ? v : 0) * C;
      arc.setAttribute("stroke-dasharray", len.toFixed(1) + " " + C.toFixed(1));
      arc.setAttribute("stroke-dashoffset", "0");
      arc.setAttribute("stroke", on ? base : "var(--secondary-text-color)");
      arc.style.transform = "rotate(-90deg)";
      arc.style.transformOrigin = "60px 60px";
      const track = this._$("track");
      track.setAttribute("stroke", "var(--divider-color, rgba(0,0,0,.12))");
      this._$("pct").textContent = on ? Math.round(v * 100) + "%" : "关";
      const pw = this._$("pw").textContent;
      this._$("watt").textContent = on ? pw : "";
      this._$("onoff").textContent = on ? "已开" : "已关";
      this._$("sat").style.background =
        "linear-gradient(to right, hsl(" + hue + ",0%,62%), " + base + ")";
    }

    _angleFrac(ev) {
      const svg = this._$("svg");
      const box = svg.getBoundingClientRect();
      const cx = box.left + box.width / 2;
      const cy = box.top + box.height / 2;
      const dx = ev.clientX - cx;
      const dy = ev.clientY - cy;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const norm = dist / (box.width / 2);         // 0 = 中心, 1 = 外沿
      let deg = (Math.atan2(dy, dx) * 180) / Math.PI;  // 0 = 右侧，顺时针增大
      deg = (deg + 90 + 360) % 360;                // 归零到正上方
      return { frac: deg / 360, norm: norm };
    }

    _onDown(e) {
      const { norm } = this._angleFrac(e);
      if (norm < 0.42) {                            // 圆心区域 → 开关
        this._touchAt = Date.now();
        const on = this._on;
        this._hass.callService("light", on ? "turn_off" : "turn_on", { entity_id: this._entity });
        return;
      }
      this._dragging = true;
      this._touchAt = Date.now();
      this._$("svg").setPointerCapture(e.pointerId);
      this._setBright(e);
    }

    _onMove(e) {
      if (!this._dragging) return;
      this._touchAt = Date.now();
      this._setBright(e);
    }

    _onUp(e) {
      if (!this._dragging) return;
      this._dragging = false;
      try { this._$("svg").releasePointerCapture(e ? e.pointerId : 0); } catch (_) { }
      this._commit();
    }

    _setBright(e) {
      const { frac } = this._angleFrac(e);
      const v = 0.02 + Math.max(0, Math.min(1, frac)) * 0.98;
      const hue = Number(this._$("hue").value);
      const sat = Number(this._$("sat").value) / 100;
      const rgb = hsv2rgb(hue, sat, v);
      // 环上的圆弧跟着手指走
      const arc = this._$("arc");
      const len = v * C;
      arc.setAttribute("stroke-dasharray", len.toFixed(1) + " " + C.toFixed(1));
      arc.setAttribute("stroke", "hsl(" + hue + "," + Math.round(sat * 100) + "%,52%)");
      this._$("pct").textContent = Math.round(v * 100) + "%";
      this._send(rgb);
    }

    _onStrip() {
      this._touchAt = Date.now();
      this._paintRing();
      const hue = Number(this._$("hue").value);
      const sat = Number(this._$("sat").value) / 100;
      const v = Math.max(0.02, this._hsv[2]);
      this._send(hsv2rgb(hue, sat, v));
    }

    _commit() { this._touchAt = Date.now(); this._send(this._lastRgb); }

    _send(rgb) {
      if (!this._hass) return;
      if (!rgb) rgb = hsv2rgb(Number(this._$("hue").value), Number(this._$("sat").value) / 100, Math.max(0.02, this._hsv[2]));
      this._lastRgb = rgb;
      const now = Date.now();
      const gap = 110;
      if (this._pending) clearTimeout(this._pending);
      if (now - this._lastSend >= gap) {
        this._lastSend = now;
        this._hass.callService("light", "turn_on", { entity_id: this._entity, rgb_color: rgb });
      } else {
        this._pending = setTimeout(() => {
          this._pending = null;
          this._lastSend = Date.now();
          this._hass.callService("light", "turn_on", { entity_id: this._entity, rgb_color: this._lastRgb });
        }, gap);
      }
    }
  }

  if (!customElements.get(TAG)) customElements.define(TAG, NetleaLightCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: TAG,
    name: "尼特利灯控（圆环版）",
    description: "圆环开关+明度、色谱滑条、功率与风扇档位，一卡搞定",
    preview: true,
  });
})();






