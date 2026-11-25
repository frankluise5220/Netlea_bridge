/* 尼特利主灯 · 色谱调色卡
 * 用法：type: custom:netlea-spectrum-card
 *       entity: light.netlea_tank_light
 * 拖三条滑条（色相 / 饱和 / 明度）即时改灯色，落到设备的 R,W,G,B 四通道。
 */
(function () {
  const TAG = "netlea-spectrum-card";

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
    return [
      Math.round((r + m) * 255),
      Math.round((g + m) * 255),
      Math.round((b + m) * 255),
    ];
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
    ":host{display:block}",
    ".card{background:var(--ha-card-background,var(--card-background-color,#fff));",
    "border-radius:var(--ha-card-border-radius,12px);padding:12px 16px 14px;",
    "border:var(--ha-card-border-width,1px) solid var(--ha-card-border-color,transparent);",
    "box-shadow:var(--ha-card-box-shadow,none)}",
    ".top{display:flex;align-items:center;gap:10px;margin-bottom:10px}",
    ".sw{width:26px;height:26px;border-radius:50%;flex:0 0 auto;",
    "box-shadow:0 0 0 2px rgba(0,0,0,.08) inset}",
    ".nm{font-weight:600;font-size:15px;color:var(--primary-text-color)}",
    ".st{margin-left:auto;font-size:12px;color:var(--secondary-text-color)}",
    ".row{display:flex;align-items:center;gap:10px;margin:8px 0}",
    ".lb{width:30px;font-size:12px;color:var(--secondary-text-color);text-align:right}",
    ".val{width:34px;font-size:12px;color:var(--secondary-text-color);",
    "text-align:right;font-variant-numeric:tabular-nums}",
    "input[type=range]{-webkit-appearance:none;appearance:none;flex:1 1 auto;",
    "height:26px;background:transparent;margin:0;outline:none;cursor:pointer}",
    "input[type=range]::-webkit-slider-runnable-track{height:26px;border-radius:8px}",
    "input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;",
    "margin-top:5px;border-radius:50%;background:#fff;border:2px solid rgba(0,0,0,.35);",
    "box-shadow:0 1px 3px rgba(0,0,0,.35)}",
    "input[type=range]::-moz-range-track{height:26px;border-radius:8px}",
    "input[type=range]::-moz-range-thumb{width:16px;height:16px;border-radius:50%;",
    "background:#fff;border:2px solid rgba(0,0,0,.35)}",
    "#hue::-webkit-slider-runnable-track{background:linear-gradient(to right,",
    "hsl(0,100%,50%),hsl(30,100%,50%),hsl(60,100%,50%),hsl(90,100%,50%),",
    "hsl(120,100%,50%),hsl(150,100%,50%),hsl(180,100%,50%),hsl(210,100%,50%),",
    "hsl(240,100%,50%),hsl(270,100%,50%),hsl(300,100%,50%),hsl(330,100%,50%),",
    "hsl(360,100%,50%))}",
    "#hue::-moz-range-track{background:linear-gradient(to right,",
    "hsl(0,100%,50%),hsl(30,100%,50%),hsl(60,100%,50%),hsl(90,100%,50%),",
    "hsl(120,100%,50%),hsl(150,100%,50%),hsl(180,100%,50%),hsl(210,100%,50%),",
    "hsl(240,100%,50%),hsl(270,100%,50%),hsl(300,100%,50%),hsl(330,100%,50%),",
    "hsl(360,100%,50%))}",
  ].join("");

  class NetleaSpectrumCard extends HTMLElement {
    constructor() {
      super();
      this._built = false;
      this._touchAt = 0;
      this._pending = null;
      this._lastSend = 0;
    }

    setConfig(config) {
      this._config = config || {};
      this._entity = this._config.entity || "light.netlea_tank_light";
      this._name = this._config.name || "";
      if (this._built) this._paintAll();
    }

    getCardSize() { return 3; }

    connectedCallback() {
      if (this._built) return;
      const root = this.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = CSS;
      root.appendChild(style);
      const wrap = document.createElement("div");
      wrap.className = "card";
      wrap.innerHTML =
        '<div class="top"><div class="sw" id="sw"></div>' +
        '<div class="nm" id="nm">主灯光谱</div><div class="st" id="st"></div></div>' +
        '<div class="row"><span class="lb">色相</span>' +
        '<input id="hue" type="range" min="0" max="360" step="1" value="0">' +
        '<span class="val" id="hv">0</span></div>' +
        '<div class="row"><span class="lb">饱和</span>' +
        '<input id="sat" type="range" min="0" max="100" step="1" value="100">' +
        '<span class="val" id="sv">100</span></div>' +
        '<div class="row"><span class="lb">明度</span>' +
        '<input id="val" type="range" min="2" max="100" step="1" value="100">' +
        '<span class="val" id="vv">100</span></div>';
      root.appendChild(wrap);
      this._root = root;
      ["hue", "sat", "val"].forEach((id) => {
        this._$(id).addEventListener("input", () => this._onSlide());
      });
      this._built = true;
      this._paintAll();
    }

    set hass(h) {
      this._hass = h;
      if (!this._built) return;
      this._paintState();
      if (Date.now() - this._touchAt > 1500) this._syncFromState();
    }

    _$ (id) { return this._root.querySelector("#" + id); }

    _state() {
      return this._hass && this._hass.states ? this._hass.states[this._entity] : null;
    }

    _hsv() {
      return [
        Number(this._$("hue").value),
        Number(this._$("sat").value) / 100,
        Number(this._$("val").value) / 100,
      ];
    }

    _paintState() {
      const s = this._state();
      if (!s) { this._$("st").textContent = "实体不存在"; return; }
      this._$("st").textContent = s.state === "on" ? "开" : "关";
      this._$("nm").textContent =
        this._name || (s.attributes && s.attributes.friendly_name) || "主灯光谱";
    }

    _paintGradients() {
      const [h, s, v] = this._hsv();
      const base = "hsl(" + h + ",100%,50%)";
      this._$("sat").style.background =
        "linear-gradient(to right, hsl(" + h + ",0%,62%), " + base + ")";
      this._$("val").style.background = "linear-gradient(to right, #000, " + base + ")";
      this._$("hv").textContent = Math.round(h);
      this._$("sv").textContent = Math.round(s * 100);
      this._$("vv").textContent = Math.round(v * 100);
      this._$("sw").style.background = "rgb(" + hsv2rgb(h, s, v).join(",") + ")";
    }

    _paintAll() { this._paintState(); this._paintGradients(); }

    _syncFromState() {
      const s = this._state();
      if (!s || !s.attributes) return;
      const rgb = s.attributes.rgb_color;
      if (!rgb) return;
      const hsv = rgb2hsv(rgb[0], rgb[1], rgb[2]);
      this._$("hue").value = Math.round(hsv[0]);
      this._$("sat").value = Math.round(hsv[1] * 100);
      this._$("val").value = Math.max(2, Math.round(hsv[2] * 100));
      this._paintGradients();
    }

    _onSlide() {
      this._touchAt = Date.now();
      this._paintGradients();
      const gap = 110;
      if (this._pending) clearTimeout(this._pending);
      if (Date.now() - this._lastSend >= gap) this._send();
      else {
        this._pending = setTimeout(() => {
          this._pending = null;
          this._send();
        }, gap);
      }
    }

    _send() {
      if (!this._hass) return;
      this._lastSend = Date.now();
      const hsv = this._hsv();
      const rgb = hsv2rgb(hsv[0], hsv[1], hsv[2]);
      this._hass.callService("light", "turn_on", {
        entity_id: this._entity,
        rgb_color: rgb,
      });
    }
  }

  if (!customElements.get(TAG)) customElements.define(TAG, NetleaSpectrumCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: TAG,
    name: "尼特利色谱调色卡",
    description: "三条滑条（色相/饱和/明度）实时调节鱼缸灯光谱",
    preview: true,
  });
})();
