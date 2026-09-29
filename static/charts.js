/**
 * @file Canvas chart engine for Finlify.
 *
 * Renders area, donut, bar, and sparkline charts with no external dependencies.
 * Each chart draws to a canvas element and adapts to its container through a
 * ResizeObserver when one is available.
 */

(function () {
  "use strict";

  var DPR = function () { return Math.min(window.devicePixelRatio || 1, 2); };

  /**
   * Report whether the user has asked for reduced motion.
   *
   * Guarded because some embedded and webview contexts lack matchMedia
   * entirely, in which case motion is treated as permitted.
   *
   * @returns {boolean} True when animations should be suppressed.
   */
  function prefersReducedMotion() {
    try {
      return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    } catch (e) {
      return false;
    }
  }

  var reduceMotion = prefersReducedMotion();

  /**
   * Locale aware euro formatter, falling back to a plain string when the Intl
   * API is unavailable.
   */
  var currency = (function () {
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency", currency: "EUR", maximumFractionDigits: 2,
      });
    } catch (e) {
      return { format: function (n) { return "€" + Number(n).toFixed(2); } };
    }
  })();

  /**
   * Display formatting helpers shared by the dashboard and the charts.
   *
   * These format numbers as text for presentation only. They must never be
   * used for monetary arithmetic, which is performed in integer cents on the
   * server.
   */
  var FX = {
    money: function (n) { return currency.format(Number(n) || 0); },
    compact: function (n) {
      var v = Number(n) || 0;
      var abs = Math.abs(v);
      var sign = v < 0 ? "-" : "";
      if (abs >= 1e6) return sign + "€" + (abs / 1e6).toFixed(abs >= 1e7 ? 0 : 1) + "M";
      if (abs >= 1e3) return sign + "€" + (abs / 1e3).toFixed(abs >= 1e4 ? 0 : 1) + "k";
      return sign + "€" + abs.toFixed(0);
    },
    signed: function (n) {
      var v = Number(n) || 0;
      return (v > 0 ? "+" : "") + FX.compact(v);
    },
    pct: function (n, digits) {
      var d = digits === undefined ? 1 : digits;
      return (Number(n) || 0).toFixed(d) + "%";
    },
    int: function (n) { return new Intl.NumberFormat().format(Number(n) || 0); },
    date: function (iso, opts) {
      if (!iso) return "—";
      var parts = String(iso).split("-");
      var d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
      return d.toLocaleDateString(undefined, opts || { day: "2-digit", month: "short" });
    },
    dayShort: function (iso) {
      var parts = String(iso).split("-");
      var d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
      return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
    },
    clamp: function (v, lo, hi) { return Math.max(lo, Math.min(hi, v)); },
    lerp: function (a, b, t) { return a + (b - a) * t; },
    prefersReducedMotion: prefersReducedMotion,
  };

  /**
   * Cubic ease out, used to drive the draw-in progress of a chart.
   *
   * @param {number} t Progress in the range 0 to 1.
   * @returns {number} The eased progress.
   */
  function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

  /**
   * Expand a hex colour to an rgba() string.
   *
   * @param {string} hex A three or six digit hex colour such as "#0af".
   * @param {number} alpha Opacity in the range 0 to 1.
   * @returns {string} The colour as an rgba() CSS string.
   */
  function hexToRgba(hex, alpha) {
    var h = hex.replace("#", "");
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    var num = parseInt(h, 16);
    return "rgba(" + ((num >> 16) & 255) + "," + ((num >> 8) & 255) + "," + (num & 255) + "," + alpha + ")";
  }

  /**
   * Trace a smooth Catmull-Rom style curve through a series of points onto the
   * current path. The caller is responsible for beginning and stroking the path.
   *
   * @param {CanvasRenderingContext2D} ctx Target 2D context.
   * @param {Array<{x: number, y: number}>} pts Points to pass through.
   * @param {number} [tension=0.2] Curve tension; higher values round more.
   */
  function smoothPath(ctx, pts, tension) {
    if (!pts.length) return;
    var t = tension === undefined ? 0.2 : tension;
    ctx.moveTo(pts[0].x, pts[0].y);
    for (var i = 0; i < pts.length - 1; i++) {
      var p0 = pts[i - 1] || pts[i];
      var p1 = pts[i];
      var p2 = pts[i + 1];
      var p3 = pts[i + 2] || p2;
      ctx.bezierCurveTo(
        p1.x + (p2.x - p0.x) * t, p1.y + (p2.y - p0.y) * t,
        p2.x - (p3.x - p1.x) * t, p2.y - (p3.y - p1.y) * t,
        p2.x, p2.y
      );
    }
  }

  /**
   * Round a value up to the next visually tidy axis maximum.
   *
   * @param {number} value The raw maximum to round.
   * @returns {number} A rounded maximum suitable for a chart axis.
   */
  function niceCeil(value) {
    if (value <= 0) return 10;
    var exp = Math.floor(Math.log10(value));
    var base = Math.pow(10, exp);
    var norm = value / base;
    var step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
    return step * base;
  }

  /**
   * Shared behaviour for canvas charts: sizing, device pixel ratio handling,
   * pointer tracking, and the draw-in animation.
   *
   * Subclasses override {@link BaseChart#draw} and, when they need hit testing,
   * {@link BaseChart#handlePointer}.
   *
   * @constructor
   * @param {HTMLCanvasElement} canvas The canvas to render into.
   * @param {Object} [opts] Options such as colours, duration, and an onHover
   *   callback invoked with the hovered datum or null.
   */
  function BaseChart(canvas, opts) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.opts = opts || {};
    this.data = [];
    this.progress = reduceMotion ? 1 : 0;
    this.pointer = null;
    this.hoverIndex = -1;
    this.w = 0;
    this.h = 0;

    var self = this;
    this._onResize = function () { self.resize(); };
    this._onMove = function (e) {
      var r = self.canvas.getBoundingClientRect();
      self.pointer = { x: e.clientX - r.left, y: e.clientY - r.top };
      self.handlePointer();
    };
    this._onLeave = function () {
      self.pointer = null;
      self.hoverIndex = -1;
      self.draw();
      if (self.opts.onHover) self.opts.onHover(null);
    };

    canvas.addEventListener("pointermove", this._onMove);
    canvas.addEventListener("pointerleave", this._onLeave);

    if (window.ResizeObserver) {
      this.ro = new ResizeObserver(this._onResize);
      this.ro.observe(canvas.parentElement || canvas);
    } else {
      window.addEventListener("resize", this._onResize);
    }

    this.resize();
  }

  /**
   * Size the backing store to the container and redraw. Accounts for the
   * device pixel ratio so lines stay crisp on high density displays.
   */
  BaseChart.prototype.resize = function () {
    var host = this.canvas.parentElement || this.canvas;
    var rect = host.getBoundingClientRect();
    var w = Math.max(20, Math.floor(rect.width));
    var h = Math.max(20, Math.floor(rect.height));
    if (!w || !h) return;
    var dpr = DPR();
    this.w = w;
    this.h = h;
    this.canvas.width = Math.floor(w * dpr);
    this.canvas.height = Math.floor(h * dpr);
    this.canvas.style.width = w + "px";
    this.canvas.style.height = h + "px";
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.draw();
  };

  /**
   * Replace the chart's data and animate the transition into view.
   *
   * @param {Array} data The dataset to render.
   */
  BaseChart.prototype.setData = function (data) {
    this.data = data || [];
    this.animate();
  };

  /**
   * Schedule the draw-in animation, or draw immediately when the user prefers
   * reduced motion.
   */
  BaseChart.prototype.animate = function () {
    if (this._raf) cancelAnimationFrame(this._raf);
    if (reduceMotion) {
      this.progress = 1;
      this.draw();
      return;
    }
    var duration = this.opts.duration || 900;
    var start = performance.now();
    var self = this;
    var step = function (now) {
      var p = Math.min(1, (now - start) / duration);
      self.progress = easeOutCubic(p);
      self.draw();
      if (p < 1) self._raf = requestAnimationFrame(step);
    };
    this._raf = requestAnimationFrame(step);
  };

  /**
   * Convert a pointer position into a hovered datum. The base implementation
   * does nothing; subclasses override it for hit testing.
   */
  BaseChart.prototype.handlePointer = function () { this.draw(); };

  /** Render the chart. The base implementation only clears the canvas. */
  BaseChart.prototype.draw = function () { this.ctx.clearRect(0, 0, this.w, this.h); };

  /**
   * Line and area chart for a small monthly series.
   *
   * Renders income and expenses as smoothed, glowing lines with a gradient
   * fill and a dashed hover guideline. Either series can be hidden through the
   * {@link BaseChart} `hidden` option.
   *
   * @constructor
   * @extends BaseChart
   * @param {HTMLCanvasElement} canvas The canvas to render into.
   * @param {Object} [opts] Options, including `incomeColor`, `expenseColor`,
   *   `hidden`, and `onHover`.
   */
  function AreaChart(canvas, opts) {
    BaseChart.call(this, canvas, opts);
  }
  AreaChart.prototype = Object.create(BaseChart.prototype);
  AreaChart.prototype.constructor = AreaChart;

  /** Render the income and expense series onto the canvas. */
  AreaChart.prototype.draw = function () {
    var ctx = this.ctx, w = this.w, h = this.h, data = this.data;
    ctx.clearRect(0, 0, w, h);
    if (!data.length) return this.drawEmpty();

    var padL = 54, padR = 14, padT = 16, padB = 26;
    var plotW = Math.max(10, w - padL - padR);
    var plotH = Math.max(10, h - padT - padB);

    var series = [
      { key: "income", color: this.opts.incomeColor || "#22d3ee", label: "Income", on: true },
      { key: "expenses", color: this.opts.expenseColor || "#f472b6", label: "Expenses", on: true },
    ];
    if (this.opts.hidden) {
      series.forEach(function (s) { s.on = this.opts.hidden.indexOf(s.key) === -1; }, this);
    }

    var max = 0;
    series.forEach(function (s) {
      if (!s.on) return;
      data.forEach(function (d) { if (d[s.key] > max) max = d[s.key]; });
    });
    var top = niceCeil(max * 1.12) || 10;

    function yFor(v) { return padT + plotH - (v / top) * plotH; }
    function xFor(i) { return padL + (data.length === 1 ? plotW / 2 : (i / (data.length - 1)) * plotW); }

    ctx.font = "500 10px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (var g = 0; g <= 4; g++) {
      var gy = padT + (plotH / 4) * g;
      ctx.beginPath();
      ctx.strokeStyle = "rgba(255,255,255,0.06)";
      ctx.lineWidth = 1;
      ctx.moveTo(padL, gy + 0.5);
      ctx.lineTo(padL + plotW, gy + 0.5);
      ctx.stroke();
      ctx.fillStyle = "rgba(255,255,255,0.32)";
      ctx.fillText(FX.compact(top - (top / 4) * g), padL - 10, gy);
    }

    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    var stepLabel = Math.max(1, Math.ceil(data.length / 7));
    data.forEach(function (d, i) {
      if (i % stepLabel !== 0 && i !== data.length - 1) return;
      ctx.fillStyle = "rgba(255,255,255,0.3)";
      ctx.fillText(d.label, xFor(i), padT + plotH + 8);
    });

    var self = this;
    series.forEach(function (s) {
      if (!s.on) return;
      var pts = data.map(function (d, i) { return { x: xFor(i), y: yFor(d[s.key]) }; });
      if (!pts.length) return;

      var reveal = self.progress;
      var n = Math.max(1, Math.ceil(pts.length * reveal));
      var shown = pts.slice(0, n);

      ctx.beginPath();
      smoothPath(ctx, shown);
      var last = shown[shown.length - 1];
      var firstX = shown[0].x;
      var fill = ctx.createLinearGradient(0, padT, 0, padT + plotH);
      fill.addColorStop(0, hexToRgba(s.color, 0.22));
      fill.addColorStop(1, hexToRgba(s.color, 0.01));
      ctx.save();
      ctx.lineTo(last.x, padT + plotH);
      ctx.lineTo(firstX, padT + plotH);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.restore();

      ctx.save();
      ctx.beginPath();
      smoothPath(ctx, shown);
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 1.8;
      ctx.lineJoin = "round";
      ctx.shadowColor = hexToRgba(s.color, 0.85);
      ctx.shadowBlur = 6;
      ctx.stroke();
      ctx.restore();

      if (self.hoverIndex >= 0 && self.hoverIndex < data.length && reveal > 0.98) {
        var hp = pts[self.hoverIndex];
        ctx.beginPath();
        ctx.arc(hp.x, hp.y, 4.5, 0, Math.PI * 2);
        ctx.fillStyle = "#07080c";
        ctx.fill();
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 2.5;
        ctx.stroke();
      }
    });

    if (self.hoverIndex >= 0 && self.hoverIndex < data.length && this.progress > 0.98) {
      var hx = xFor(this.hoverIndex);
      ctx.beginPath();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "rgba(255,255,255,0.22)";
      ctx.lineWidth = 1;
      ctx.moveTo(hx, padT);
      ctx.lineTo(hx, padT + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  };

  /** Draw the placeholder shown when the series is empty. */
  AreaChart.prototype.drawEmpty = function () {
    var ctx = this.ctx;
    ctx.fillStyle = "rgba(255,255,255,0.28)";
    ctx.font = "500 12px ui-sans-serif, system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("No data for this range", this.w / 2, this.h / 2);
  };

  /** Resolve the pointer position to the nearest data point. */
  AreaChart.prototype.handlePointer = function () {
    var padL = 54, padR = 14;
    if (!this.data.length || !this.pointer) { this.hoverIndex = -1; return this.draw(); }
    var plotW = Math.max(10, this.w - padL - padR);
    var rel = FX.clamp((this.pointer.x - padL) / plotW, 0, 1);
    var idx = Math.round(rel * (this.data.length - 1));
    var changed = idx !== this.hoverIndex;
    this.hoverIndex = idx;
    this.draw();
    if (changed && this.opts.onHover) this.opts.onHover(this.data[idx]);
  };

  /**
   * Donut chart for category shares of a total.
   *
   * Renders each slice with a gradient and a small gap, lifts the hovered
   * slice, and prints the overall total in the centre.
   *
   * @constructor
   * @extends BaseChart
   * @param {HTMLCanvasElement} canvas The canvas to render into.
   * @param {Object} [opts] Options, including `onHover` with the hovered
   *   category's datum.
   */
  function DonutChart(canvas, opts) {
    BaseChart.call(this, canvas, opts);
    this.slices = [];
  }
  DonutChart.prototype = Object.create(BaseChart.prototype);
  DonutChart.prototype.constructor = DonutChart;

  /** Render the donut, storing slice angles for hover hit testing. */
  DonutChart.prototype.draw = function () {
    var ctx = this.ctx, w = this.w, h = this.h;
    ctx.clearRect(0, 0, w, h);
    var data = this.data;
    if (!data.length) {
      ctx.fillStyle = "rgba(255,255,255,0.28)";
      ctx.font = "500 12px ui-sans-serif, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("No spending yet", w / 2, h / 2);
      this.slices = [];
      return;
    }

    var cx = w / 2, cy = h / 2;
    var outer = Math.min(w, h) / 2 - 8;
    var inner = outer * 0.63;
    var total = data.reduce(function (s, d) { return s + d.total; }, 0) || 1;

    var gap = data.length > 1 ? 0.014 : 0;
    var angle = -Math.PI / 2;
    var reveal = this.progress;
    this.slices = [];

    data.forEach(function (d, i) {
      var sweep = (d.total / total) * Math.PI * 2;
      var a0 = angle + gap / 2;
      var a1 = angle + Math.max(0, sweep - gap / 2);
      angle += sweep;

      var shownSweep = Math.max(0, (a1 - a0) * reveal);
      if (shownSweep <= 0.0001) return;

      var isHover = this.hoverIndex === i;
      var r = outer + (isHover ? 5 : 0);
      this.slices.push({ a0: a0, a1: a1, index: i, category: d.category });

      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, r, a0, a0 + shownSweep);
      ctx.arc(cx, cy, inner, a0 + shownSweep, a0, true);
      ctx.closePath();
      var grad = ctx.createLinearGradient(cx - r, cy - r, cx + r, cy + r);
      grad.addColorStop(0, hexToRgba(d.color, isHover ? 1 : 0.92));
      grad.addColorStop(1, hexToRgba(d.color, isHover ? 0.85 : 0.62));
      ctx.fillStyle = grad;
      if (isHover) {
        ctx.shadowColor = hexToRgba(d.color, 0.8);
        ctx.shadowBlur = 9;
      }
      ctx.fill();
      ctx.restore();
    }, this);

    ctx.save();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "rgba(255,255,255,0.34)";
    ctx.font = "600 10px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.fillText("TOTAL", cx, cy - 12);
    ctx.fillStyle = "#e9edf5";
    ctx.font = "500 18px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(FX.compact(total), cx, cy + 8);
    ctx.restore();
  };

  /** Resolve the pointer angle to a slice within the ring's radius. */
  DonutChart.prototype.handlePointer = function () {
    if (!this.pointer || !this.slices.length) return this.draw();
    var cx = this.w / 2, cy = this.h / 2;
    var dx = this.pointer.x - cx, dy = this.pointer.y - cy;
    var dist = Math.sqrt(dx * dx + dy * dy);
    if (dist < 10 || dist > Math.min(this.w, this.h) / 2 + 8) {
      if (this.hoverIndex !== -1) {
        this.hoverIndex = -1;
        this.draw();
        if (this.opts.onHover) this.opts.onHover(null);
      }
      return;
    }
    var ang = Math.atan2(dy, dx);
    if (ang < -Math.PI / 2) ang += Math.PI * 2;
    var found = -1;
    for (var i = 0; i < this.slices.length; i++) {
      var s = this.slices[i];
      if (ang >= s.a0 && ang <= s.a1) { found = s.index; break; }
    }
    if (found !== this.hoverIndex) {
      this.hoverIndex = found;
      this.draw();
      if (this.opts.onHover) this.opts.onHover(found >= 0 ? this.data[found] : null);
    }
  };

  /**
   * Bar chart for daily net cash flow.
   *
   * Bars grow up for positive net days and down for negative ones, with a
   * highlighted value label on the hovered bar.
   *
   * @constructor
   * @extends BaseChart
   * @param {HTMLCanvasElement} canvas The canvas to render into.
   * @param {Object} [opts] Options, including `onHover` with the hovered day.
   */
  function BarChart(canvas, opts) {
    BaseChart.call(this, canvas, opts);
  }
  BarChart.prototype = Object.create(BaseChart.prototype);
  BarChart.prototype.constructor = BarChart;

  /** Render the signed daily bars around a zero baseline. */
  BarChart.prototype.draw = function () {
    var ctx = this.ctx, w = this.w, h = this.h, data = this.data;
    ctx.clearRect(0, 0, w, h);
    if (!data.length) return;

    var padL = 46, padR = 8, padT = 12, padB = 22;
    var plotW = Math.max(10, w - padL - padR);
    var plotH = Math.max(10, h - padT - padB);

    var maxAbs = 0;
    data.forEach(function (d) {
      maxAbs = Math.max(maxAbs, Math.abs(d.net));
    });
    var top = niceCeil(maxAbs * 1.15) || 10;
    var zeroY = padT + plotH / 2;

    ctx.font = "500 9px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    [
      { y: padT, v: top },
      { y: zeroY, v: 0 },
      { y: padT + plotH, v: -top },
    ].forEach(function (ln) {
      ctx.beginPath();
      ctx.strokeStyle = ln.v === 0 ? "rgba(140,170,255,0.22)" : "rgba(140,170,255,0.08)";
      ctx.lineWidth = 1;
      ctx.moveTo(padL, ln.y + 0.5);
      ctx.lineTo(padL + plotW, ln.y + 0.5);
      ctx.stroke();
      ctx.fillStyle = "rgba(255,255,255,0.3)";
      ctx.fillText(FX.compact(ln.v), padL - 8, ln.y);
    });

    var slot = plotW / data.length;
    var barW = Math.max(2, Math.min(14, slot * 0.62));

    data.forEach(function (d, i) {
      var x = padL + slot * i + (slot - barW) / 2;
      var ratio = FX.clamp(Math.abs(d.net) / top, 0, 1);
      var barH = ratio * (plotH / 2);
      var isHover = this.hoverIndex === i;
      var positive = d.net >= 0;
      var color = positive ? "#22d3ee" : "#f472b6";

      ctx.save();
      ctx.beginPath();
      var y = positive ? zeroY - barH : zeroY;
      var r = Math.min(barW / 2, 3);
      if (ctx.roundRect) ctx.roundRect(x, y, barW, Math.max(barH, 1.5), r);
      else ctx.rect(x, y, barW, Math.max(barH, 1.5));
      ctx.fillStyle = hexToRgba(color, isHover ? 1 : 0.72);
      if (isHover) {
        ctx.shadowColor = hexToRgba(color, 0.8);
        ctx.shadowBlur = 6;
      }
      ctx.fill();
      ctx.restore();
    }, this);

    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    var every = Math.max(1, Math.ceil(data.length / 6));
    data.forEach(function (d, i) {
      if (i % every !== 0 && i !== data.length - 1) return;
      ctx.fillStyle = "rgba(150,170,215,0.5)";
      ctx.fillText(FX.dayShort(d.date), padL + slot * i + slot / 2, padT + plotH + 6);
    });

    if (this.hoverIndex >= 0 && this.data[this.hoverIndex]) {
      var d2 = this.data[this.hoverIndex];
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillStyle = "#e6edff";
      ctx.font = "600 10px ui-monospace, SFMono-Regular, Menlo, monospace";
      ctx.fillText(FX.compact(d2.net), padL + slot * this.hoverIndex + slot / 2, zeroY - (d2.net >= 0 ? Math.abs(d2.net) / top * (plotH / 2) : 0) - 6);
      ctx.font = "500 9px ui-monospace, SFMono-Regular, Menlo, monospace";
    }
  };

  /** Resolve the pointer position to a bar slot. */
  BarChart.prototype.handlePointer = function () {
    if (!this.data.length || !this.pointer) { this.hoverIndex = -1; return this.draw(); }
    var padL = 46, padR = 8;
    var plotW = Math.max(10, this.w - padL - padR);
    var slot = plotW / this.data.length;
    var idx = Math.floor((this.pointer.x - padL) / slot);
    idx = FX.clamp(idx, 0, this.data.length - 1);
    var changed = idx !== this.hoverIndex;
    this.hoverIndex = idx;
    this.draw();
    if (changed && this.opts.onHover) this.opts.onHover(this.data[idx]);
  };

  /**
   * Compact trend sparkline for a single value series.
   *
   * Unlike the other charts this one is not animated and has no axes or hover
   * behaviour; it exists to give a stat tile a sense of direction.
   *
   * @constructor
   * @param {HTMLCanvasElement} canvas The canvas to render into.
   * @param {Object} [opts] Options, including `color`.
   */
  function Sparkline(canvas, opts) {
    this.ctx = canvas.getContext("2d");
    this.canvas = canvas;
    this.opts = opts || {};
    this.data = [];
    this.w = 0;
    this.h = 0;
    var self = this;
    if (window.ResizeObserver) {
      this.ro = new ResizeObserver(function () { self.resize(); });
      this.ro.observe(canvas.parentElement || canvas);
    }
    this.resize();
  }

  /** Size the backing store to the container and redraw. */
  Sparkline.prototype.resize = function () {
    var host = this.canvas.parentElement || this.canvas;
    var rect = host.getBoundingClientRect();
    var w = Math.max(10, Math.floor(rect.width));
    var h = Math.max(10, Math.floor(rect.height));
    if (!w || !h) return;
    var dpr = DPR();
    this.w = w; this.h = h;
    this.canvas.width = Math.floor(w * dpr);
    this.canvas.height = Math.floor(h * dpr);
    this.canvas.style.width = w + "px";
    this.canvas.style.height = h + "px";
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.draw();
  };

  /**
   * Replace the sparkline values.
   *
   * @param {Array<number>} values The series to plot.
   */
  Sparkline.prototype.setData = function (values) {
    this.data = (values || []).map(Number);
    this.draw();
  };

  /** Render the sparkline as a filled curve with a highlighted head. */
  Sparkline.prototype.draw = function () {
    var ctx = this.ctx, w = this.w, h = this.h, data = this.data;
    ctx.clearRect(0, 0, w, h);
    if (data.length < 2) return;

    var color = this.opts.color || "#22d3ee";
    var max = Math.max.apply(null, data);
    var min = Math.min.apply(null, data);
    var span = max - min || 1;
    var pts = data.map(function (v, i) {
      return {
        x: (i / (data.length - 1)) * (w - 2) + 1,
        y: h - 2 - ((v - min) / span) * (h - 4),
      };
    });

    ctx.beginPath();
    smoothPath(ctx, pts, 0.18);
    ctx.lineTo(pts[pts.length - 1].x, h);
    ctx.lineTo(pts[0].x, h);
    ctx.closePath();
    var fill = ctx.createLinearGradient(0, 0, 0, h);
    fill.addColorStop(0, hexToRgba(color, 0.3));
    fill.addColorStop(1, hexToRgba(color, 0));
    ctx.fillStyle = fill;
    ctx.fill();

    ctx.beginPath();
    smoothPath(ctx, pts, 0.18);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = "round";
    ctx.shadowColor = hexToRgba(color, 0.7);
    ctx.shadowBlur = 4;
    ctx.stroke();
    ctx.shadowBlur = 0;

    var head = pts[pts.length - 1];
    ctx.beginPath();
    ctx.arc(head.x, head.y, 2.4, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
  };

  window.FX = FX;
  window.FinlifyCharts = {
    AreaChart: AreaChart,
    DonutChart: DonutChart,
    BarChart: BarChart,
    Sparkline: Sparkline,
  };
})();
