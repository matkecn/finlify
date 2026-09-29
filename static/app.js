/**
 * @file Dashboard controller for Finlify.
 *
 * Wires the single page interface to the JSON API: loading and rendering the
 * summary, charts, budgets, insights and ledger, plus the mutations for
 * transactions, categories, budgets, and backup import and export.
 *
 * All rendered figures come from the server, which owns the authoritative
 * integer cent arithmetic. This file formats them for display only.
 */

(function () {
  "use strict";

  var FX = window.FX;
  var C = window.FinlifyCharts;

  var state = {
    months: 6,
    hiddenSeries: [],
    breakdownType: "expense",
    modalType: "expense",
    editingId: null,
    offset: 0,
    limit: 25,
    total: 0,
    categories: [],
    accounts: [],
    filters: { search: "", type: "", category: "", account: "", sort: "date", order: "desc" },
  };

  var charts = {};
  var $ = function (id) { return document.getElementById(id); };

  var ICON_EDIT =
    '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';

  var ICON_TRASH =
    '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>';

  /**
   * Perform a JSON request and reject with the server's error detail on failure.
   *
   * @param {string} path Request path, including any query string.
   * @param {RequestInit} [options] Fetch options such as method and body.
   * @returns {Promise<Object>} The parsed response body.
   */
  function api(path, options) {
    return fetch(path, options).then(function (res) {
      var isJson = (res.headers.get("content-type") || "").indexOf("application/json") !== -1;
      return (isJson ? res.json() : res.text()).then(function (body) {
        if (!res.ok) {
          var msg = "Request failed (" + res.status + ")";
          if (body && typeof body === "object" && body.detail) {
            msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
          }
          throw new Error(msg);
        }
        return body;
      });
    });
  }

  /**
   * Build a query string from an object, omitting empty values.
   *
   * @param {Object} params Key and value pairs to serialise.
   * @returns {string} A query string beginning with "?", or an empty string.
   */
  function qs(params) {
    var pairs = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v !== "" && v !== null && v !== undefined) {
        pairs.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
      }
    });
    return pairs.length ? "?" + pairs.join("&") : "";
  }

  /**
   * Update the connection status pill in the header.
   *
   * @param {string} kind Optional state class such as "is-live" or "is-error".
   * @param {string} text The label to display.
   */
  function setStatus(kind, text) {
    var pill = $("statusPill");
    pill.classList.remove("is-live", "is-error");
    if (kind) pill.classList.add(kind);
    pill.querySelector(".status-text").textContent = text;
  }

  /**
   * Show a transient notification.
   *
   * @param {string} message The message text.
   * @param {string} [kind] One of "info", "success", or "error".
   */
  function toast(message, kind) {
    var el = document.createElement("div");
    el.className = "toast " + (kind || "info");
    el.innerHTML = '<span class="toast-dot"></span><span></span>';
    el.lastChild.textContent = message;
    $("toasts").appendChild(el);
    setTimeout(function () {
      el.classList.add("is-out");
      setTimeout(function () { el.remove(); }, 300);
    }, 3800);
  }

  /**
   * Animate a numeric element from its previous value to a new one.
   *
   * @param {HTMLElement} el The element whose text is updated.
   * @param {number} to The target value.
   * @param {function(number): string} format Formats each intermediate value.
   */
  function countUp(el, to, format) {
    var from = Number(el.dataset.value || 0);
    el.dataset.value = String(to);
    if (FX.prefersReducedMotion() || from === to) { el.textContent = format(to); return; }

    var start = performance.now();
    var dur = 700;
    function frame(now) {
      var p = Math.min(1, (now - start) / dur);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = format(from + (to - from) * eased);
      if (p < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  /**
   * Render a month over month delta badge.
   *
   * @param {HTMLElement} el The badge element.
   * @param {number|null} value Percentage change, or null when undefined.
   * @param {boolean} [invert] When true, a fall is treated as good.
   */
  function setDelta(el, value, invert) {
    if (!el) return;
    if (value === null || value === undefined) {
      el.className = "delta flat";
      el.textContent = "no prior month";
      return;
    }
    var up = value > 0;
    var good = invert ? !up : up;
    el.className = "delta " + (Math.abs(value) < 0.05 ? "flat" : good ? "up" : "down");
    var arrow = Math.abs(value) < 0.05 ? "→" : up ? "▲" : "▼";
    el.textContent = arrow + " " + Math.abs(value).toFixed(1) + "%";
  }

  /** Reveal panels with a staggered fade as they scroll into view. */
  function observeReveals() {
    var targets = document.querySelectorAll(".stat, .panel");
    if (!window.IntersectionObserver) {
      Array.prototype.forEach.call(targets, function (t) { t.classList.add("reveal", "is-in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry, i) {
        if (!entry.isIntersecting) return;
        setTimeout(function () { entry.target.classList.add("is-in"); }, i * 45);
        io.unobserve(entry.target);
      });
    }, { threshold: 0.05, rootMargin: "0px 0px -30px 0px" });

    Array.prototype.forEach.call(targets, function (t, i) {
      t.classList.add("reveal");
      t.style.transitionDelay = Math.min(i * 35, 280) + "ms";
      io.observe(t);
    });
  }

  /** Instantiate every chart on the page. */
  function initCharts() {
    charts.cashflow = new C.AreaChart($("cashflowChart"), {
      incomeColor: "#5eead4",
      expenseColor: "#f0abfc",
      hidden: state.hiddenSeries,
      onHover: showCashflowTip,
    });
    charts.category = new C.DonutChart($("categoryChart"), {
      onHover: function (item) { highlightLegend(item ? item.category : null); },
    });
    charts.daily = new C.BarChart($("dailyChart"), { onHover: showDailyTip });
    charts.sparks = {
      balance: new C.Sparkline($("sparkBalance"), { color: "#5eead4" }),
      income: new C.Sparkline($("sparkIncome"), { color: "#a78bfa" }),
      expenses: new C.Sparkline($("sparkExpenses"), { color: "#f0abfc" }),
      savings: new C.Sparkline($("sparkSavings"), { color: "#bef264" }),
    };
  }

  /**
   * Position and fill the cash flow tooltip for the hovered month.
   *
   * @param {Object|null} item The hovered month datum, or null to hide.
   */
  function showCashflowTip(item) {
    var tip = $("cashflowTip");
    if (!item) { tip.hidden = true; return; }
    var net = item.net;
    tip.innerHTML =
      '<div class="tip-row"><span class="tip-key">' + item.label + " " + String(item.year).slice(2) + "</span></div>" +
      '<div class="tip-row"><span class="tip-key">Income</span><strong>' + FX.money(item.income) + "</strong></div>" +
      '<div class="tip-row"><span class="tip-key">Expenses</span><strong>' + FX.money(item.expenses) + "</strong></div>" +
      '<div class="tip-row"><span class="tip-key">Net</span><span class="tip-val ' + (net >= 0 ? "up" : "down") + '">' + FX.signed(net) + "</span></div>";
    if (charts.cashflow.hoverIndex >= 0 && charts.cashflow.data[charts.cashflow.hoverIndex] === item) {
      var padL = 54, padR = 14;
      var plotW = Math.max(10, charts.cashflow.w - padL - padR);
      var n = charts.cashflow.data.length;
      var x = padL + (n === 1 ? plotW / 2 : (charts.cashflow.hoverIndex / (n - 1)) * plotW);
      tip.style.left = FX.clamp(x, 70, charts.cashflow.w - 70) + "px";
      tip.style.top = "18px";
      tip.hidden = false;
    }
  }

  /**
   * Position and fill the daily pulse tooltip for the hovered day.
   *
   * @param {Object|null} item The hovered day datum, or null to hide.
   */
  function showDailyTip(item) {
    var tip = $("dailyTip");
    if (!item) { tip.hidden = true; return; }
    tip.innerHTML =
      '<div class="tip-row"><span class="tip-key">' +
      FX.date(item.date, { day: "numeric", month: "short", year: "numeric" }) + "</span></div>" +
      '<div class="tip-row"><span class="tip-key">Net</span><span class="tip-val ' +
      (item.net >= 0 ? "up" : "down") + '">' + FX.signed(item.net) + "</span></div>";
    if (charts.daily.hoverIndex >= 0) {
      tip.style.left = FX.clamp(
        charts.daily.w * ((charts.daily.hoverIndex + 0.5) / charts.daily.data.length),
        80, charts.daily.w - 80
      ) + "px";
      tip.style.top = "18px";
      tip.hidden = false;
    }
  }

  /**
   * Highlight the legend row matching a hovered donut slice.
   *
   * @param {string|null} category The category name, or null to clear.
   */
  function highlightLegend(category) {
    Array.prototype.forEach.call(document.querySelectorAll(".legend-item"), function (li) {
      li.classList.toggle("is-active", !!category && li.dataset.category === category);
    });
  }

  /**
   * Render the hero figures, deltas, and headline copy.
   *
   * @param {Object} data The summary payload from the server.
   */
  function renderSummary(data) {
    countUp($("statBalance"), data.balance, FX.money);
    countUp($("statIncome"), data.income, FX.money);
    countUp($("statExpenses"), data.expenses, FX.money);
    countUp($("statSavings"), data.savings_rate, function (v) { return v.toFixed(1) + "%"; });

    setDelta($("statIncomeDelta"), data.month.income_delta, false);
    setDelta($("statExpensesDelta"), data.month.expenses_delta, true);

    var bd = $("statBalanceDelta");
    bd.className = "delta " + (data.month.net >= 0 ? "up" : "down");
    bd.textContent = (data.month.net >= 0 ? "▲ " : "▼ ") + FX.compact(Math.abs(data.month.net)) + " this month";

    $("statBalanceMeta").textContent = FX.int(data.transaction_count) + " entries";
    $("statIncomeMeta").textContent = data.month.label + " inflow";
    $("statExpensesMeta").textContent = data.month.label + " outflow";
    $("statSavingsMeta").textContent = data.month.savings_rate !== 0
      ? FX.pct(data.month.savings_rate) + " this month"
      : "of gross income";

    var hour = new Date().getHours();
    $("greeting").textContent =
      hour < 5 ? "Still up" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

    var bits = [];
    if (data.classified_count === 0) {
      bits.push("No transactions yet — add your first one with the Add button");
    } else {
      bits.push(FX.int(data.classified_count) + " transactions");
      if (data.month.projected_expenses > 0) {
        bits.push("projecting " + FX.money(data.month.projected_expenses) + " in " + data.month.label);
      }
    }
    if (data.unclassified_count > 0) {
      bits.push(data.unclassified_count + " legacy row(s) excluded from totals");
    }
    $("heroSub").textContent = bits.join(" · ");
    $("footCount").textContent =
      FX.int(data.transaction_count) + " rows · " + FX.int(data.classified_count) + " counted";
  }

  /**
   * Render the cash flow chart and the stat tile sparklines.
   *
   * @param {Array<Object>} series Monthly buckets from the timeseries endpoint.
   */
  function renderSeries(series) {
    charts.cashflow.opts.hidden = state.hiddenSeries;
    charts.cashflow.setData(series);
    charts.sparks.balance.setData(series.map(function (s) { return s.net; }));
    charts.sparks.income.setData(series.map(function (s) { return s.income; }));
    charts.sparks.expenses.setData(series.map(function (s) { return s.expenses; }));
    charts.sparks.savings.setData(series.map(function (s) {
      return s.income ? (s.net / s.income) * 100 : 0;
    }));
    $("cashflowSub").textContent = "Income vs expenses · last " + series.length + " months";
  }

  /**
   * Render the donut chart and its legend.
   *
   * @param {Array<Object>} categories Per category totals and shares.
   */
  function renderBreakdown(categories) {
    charts.category.setData(categories);
    var legend = $("categoryLegend");
    legend.innerHTML = "";

    if (!categories.length) {
      legend.innerHTML = '<li class="empty">Nothing recorded for this range yet.</li>';
      $("breakdownSub").textContent = "By category";
      return;
    }

    categories.forEach(function (c) {
      var li = document.createElement("li");
      li.className = "legend-item";
      li.dataset.category = c.category;
      li.innerHTML =
        '<span class="legend-dot" style="background:' + c.color + '"></span>' +
        '<span class="legend-name"></span>' +
        '<span class="legend-value">' + FX.money(c.total) + "</span>" +
        '<span class="legend-pct">' + c.pct.toFixed(1) + "%</span>";
      li.querySelector(".legend-name").textContent = c.category;
      legend.appendChild(li);
    });

    var total = categories.reduce(function (s, c) { return s + c.total; }, 0);
    $("breakdownSub").textContent =
      categories.length + " categories · " + FX.money(total) + " total";
  }

  /**
   * Render the daily pulse bar chart and its summary figures.
   *
   * @param {Array<Object>} series Daily buckets from the daily endpoint.
   */
  function renderDaily(series) {
    charts.daily.setData(series);
    var avg = series.reduce(function (s, d) { return s + d.net; }, 0) / (series.length || 1);
    $("pulseAvg").textContent = FX.signed(avg);
    var peak = series.reduce(function (best, d) {
      return d.net > best.net ? d : best;
    }, series[0] || { net: 0, date: null });
    $("pulsePeak").textContent = peak && peak.date
      ? FX.signed(peak.net) + " · " + FX.dayShort(peak.date) : "—";
  }

  /**
   * Render the budget list with progress bars and threshold states.
   *
   * @param {Array<Object>} budgets Per budget spend status from the summary.
   */
  function renderBudgets(budgets) {
    var list = $("budgetList");
    list.innerHTML = "";
    if (!budgets || !budgets.length) {
      list.innerHTML = '<li class="empty">No budgets set. Pick a category below to add one.</li>';
      return;
    }

    budgets.forEach(function (b) {
      var li = document.createElement("li");
      li.className = "budget-item" +
        (b.state === "warn" ? " is-warn" : b.state === "over" ? " is-over" : "");

      var top = document.createElement("div");
      top.className = "budget-top";

      var name = document.createElement("span");
      name.className = "budget-name";
      var dot = document.createElement("span");
      dot.className = "legend-dot";
      dot.style.background = b.color;
      var nameText = document.createElement("span");
      nameText.textContent = b.category;
      name.appendChild(dot);
      name.appendChild(nameText);

      var nums = document.createElement("span");
      nums.className = "budget-nums";
      nums.textContent = FX.money(b.spent) + " / " + FX.money(b.limit) + " · " + b.pct.toFixed(0) + "%";

      top.appendChild(name);
      top.appendChild(nums);

      var track = document.createElement("div");
      track.className = "budget-track";
      var fill = document.createElement("div");
      fill.className = "budget-fill";
      track.appendChild(fill);

      li.appendChild(top);
      li.appendChild(track);
      list.appendChild(li);
      requestAnimationFrame(function () { fill.style.width = FX.clamp(b.pct, 0, 100) + "%"; });
    });
  }

  /**
   * Build an insight list item.
   *
   * @param {string} icon A short glyph shown at the start of the row.
   * @param {string} html The row body, which may contain markup.
   * @param {string} [kind] A state class such as "good", "warn", or "bad".
   * @returns {HTMLLIElement} The constructed list item.
   */
  function insight(icon, html, kind) {
    var li = document.createElement("li");
    li.className = "insight " + (kind || "neutral");
    li.innerHTML = '<span class="insight-icon"></span><span></span>';
    li.firstChild.textContent = icon;
    li.lastChild.innerHTML = html;
    return li;
  }

  /**
   * Render the derived insight list.
   *
   * @param {Object} data The summary payload from the server.
   */
  function renderInsights(data) {
    var list = $("insightList");
    list.innerHTML = "";
    var m = data.month;

    if (data.classified_count === 0) {
      list.appendChild(insight("＋", "Add a transaction to start building your picture. Categories come from the <b>Categories</b> panel.", "neutral"));
      if (data.unclassified_count > 0) {
        list.appendChild(insight("!", "<b>" + data.unclassified_count + "</b> legacy row(s) have an unrecognised type and are excluded from every total. Delete them in the ledger to clean up.", "warn"));
      }
      return;
    }

    if (data.top_category) {
      list.appendChild(insight("◈", "<b>" + esc(data.top_category.category) + "</b> leads " +
        esc(m.label) + " spending at " + FX.money(data.top_category.total) +
        " (" + data.top_category.pct.toFixed(0) + "%).", "neutral"));
    }
    if (data.biggest_jump) {
      list.appendChild(insight("↑", "<b>" + esc(data.biggest_jump.category) + "</b> is up " +
        FX.money(data.biggest_jump.delta) + " versus last month" +
        (data.biggest_jump.pct !== null ? " (" + data.biggest_jump.pct.toFixed(0) + "%)" : "") + ".", "warn"));
    }
    if (m.expenses_delta !== null && m.expenses_delta !== undefined) {
      list.appendChild(insight(m.expenses_delta > 0 ? "▲" : "▼", "Outflow is <b>" +
        FX.pct(Math.abs(m.expenses_delta)) + "</b> " + (m.expenses_delta > 0 ? "higher" : "lower") +
        " than " + esc(priorMonthLabel(m.key)) + ".", m.expenses_delta > 0 ? "bad" : "good"));
    }
    if (data.savings_rate >= 30) {
      list.appendChild(insight("★", "Strong savings rate of <b>" + FX.pct(data.savings_rate) + "</b> across recorded income.", "good"));
    } else if (data.savings_rate < 10) {
      list.appendChild(insight("!", "Savings rate is only <b>" + FX.pct(data.savings_rate) + "</b> — income is nearly fully consumed.", "warn"));
    } else {
      list.appendChild(insight("★", "Savings rate sits at <b>" + FX.pct(data.savings_rate) + "</b>.", "neutral"));
    }
    if (data.largest_expense) {
      list.appendChild(insight("◆", "Largest single expense: <b>" + FX.money(data.largest_expense.amount) +
        "</b> on " + esc(data.largest_expense.category) + " (" +
        FX.date(data.largest_expense.date, { day: "numeric", month: "short", year: "numeric" }) + ").", "neutral"));
    }

    var over = (data.budgets || []).filter(function (b) { return b.state === "over"; });
    var warn = (data.budgets || []).filter(function (b) { return b.state === "warn"; });
    if (over.length) {
      list.appendChild(insight("✕", "<b>" + over.length + " budget" + (over.length > 1 ? "s" : "") +
        "</b> exceeded: " + over.map(function (b) { return esc(b.category); }).join(", ") + ".", "bad"));
    } else if (warn.length) {
      list.appendChild(insight("!", "Approaching limit on <b>" +
        warn.map(function (b) { return esc(b.category); }).join(", ") + "</b>.", "warn"));
    } else if (data.budgets && data.budgets.length) {
      list.appendChild(insight("✓", "All <b>" + data.budgets.length + "</b> budgets are within limits.", "good"));
    }
    if (data.unclassified_count > 0) {
      list.appendChild(insight("◇", "<b>" + data.unclassified_count +
        "</b> legacy row(s) excluded from all totals.", "warn"));
    }
  }

  /**
   * Render the projection and runway list.
   *
   * @param {Object} data The summary payload from the server.
   */
  function renderForecast(data) {
    var list = $("forecastList");
    list.innerHTML = "";
    var m = data.month;

    if (data.classified_count === 0) {
      list.appendChild(insight("—", "Forecasts appear once you have some transactions.", "neutral"));
      return;
    }

    if (m.projected_expenses > 0) {
      list.appendChild(insight("→", "At <b>" + FX.money(m.avg_daily_spend) + "/day</b>, " +
        esc(m.label) + " closes near <b>" + FX.money(m.projected_expenses) + "</b> across " +
        m.days_in_month + " days.", "neutral"));
    }
    if (m.net !== 0) {
      list.appendChild(insight(m.net >= 0 ? "↑" : "↓", "Month-to-date you are <b>" +
        FX.money(Math.abs(m.net)) + "</b> " + (m.net >= 0 ? "in the black" : "in the red") + ".", m.net >= 0 ? "good" : "bad"));
    } else {
      list.appendChild(insight("=", "Nothing recorded in " + esc(m.label) + " yet.", "neutral"));
    }

    var burn = m.avg_daily_spend;
    if (burn > 0 && data.balance > 0) {
      var days = Math.floor(data.balance / burn);
      list.appendChild(insight("◷", "Runway is roughly <b>" + FX.int(days) + " days</b> at the current " +
        FX.money(burn) + "/day pace.", days < 45 ? "bad" : days < 120 ? "warn" : "good"));
    }
    if (m.income > 0) {
      list.appendChild(insight("◐", "Income covers <b>" + (m.expenses / m.income * 100).toFixed(0) +
        "%</b> of this month's outflow.", "neutral"));
    }
  }

  /**
   * Render the ledger table and pagination state.
   *
   * @param {Object} data The paginated transactions payload.
   */
  function renderLedger(data) {
    var body = $("ledgerBody");
    body.innerHTML = "";
    state.total = data.total;

    var accountColours = {};
    state.accounts.forEach(function (a) { accountColours[a.name] = a.color; });

    if (!data.items.length) {
      body.innerHTML = '<tr><td colspan="6"><div class="empty">No transactions match these filters.</div></td></tr>';
    } else {
      data.items.forEach(function (t) {
        var tr = document.createElement("tr");
        var signed = t.type === "income" ? "+" : t.type === "expense" ? "−" : "";
        tr.innerHTML =
          '<td class="col-date">' + FX.date(t.date, { day: "2-digit", month: "short", year: "numeric" }) + "</td>" +
          '<td class="col-desc"></td>' +
          '<td><span class="tag"></span></td>' +
          '<td class="col-account"><span class="acct"><i class="acct-dot"></i><span class="acct-name"></span></span></td>' +
          '<td class="num col-amount ' + t.type + '">' + signed + FX.money(t.amount) + "</td>" +
          '<td class="col-actions">' +
            '<button class="icon-btn js-edit" aria-label="Edit" title="Edit">' + ICON_EDIT + "</button>" +
            '<button class="icon-btn js-del" aria-label="Delete" title="Delete">' + ICON_TRASH + "</button>" +
          "</td>";
        tr.querySelector(".col-desc").textContent = t.description || "—";
        tr.querySelector(".tag").textContent = t.category;
        var acct = tr.querySelector(".acct");
        acct.querySelector(".acct-dot").style.setProperty("--c", accountColours[t.account] || "var(--text-3)");
        acct.querySelector(".acct-name").textContent = t.account || "—";

        tr.querySelector(".js-edit").addEventListener("click", function (e) {
          e.stopPropagation(); openModal(t);
        });
        tr.querySelector(".js-del").addEventListener("click", function (e) {
          e.stopPropagation(); removeTransaction(t.id);
        });
        tr.addEventListener("click", function () { openModal(t); });
        body.appendChild(tr);
      });
    }

    var page = Math.floor(state.offset / state.limit) + 1;
    var pages = Math.max(1, Math.ceil(data.total / state.limit));
    $("pageInfo").textContent = data.total
      ? "Page " + page + " of " + pages + " · " + FX.int(data.total) + " rows"
      : "0 rows";
    $("pagePrev").disabled = state.offset <= 0;
    $("pageNext").disabled = !data.has_more;
    $("ledgerSub").textContent = activeFilterText();

    var select = $("filterCategory");
    var current = select.value;
    var known = data.categories || [];
    if (select.dataset.sig !== known.join("|")) {
      select.dataset.sig = known.join("|");
      select.innerHTML = '<option value="">All categories</option>';
      known.forEach(function (c) {
        var opt = document.createElement("option");
        opt.value = c; opt.textContent = c;
        select.appendChild(opt);
      });
      select.value = current;
    }
  }

  /**
   * Describe the active ledger filters for the panel subtitle.
   *
   * @returns {string} A human readable filter summary.
   */
  function activeFilterText() {
    var f = state.filters;
    var parts = [];
    if (f.type) parts.push(f.type === "income" ? "income" : "expenses");
    if (f.category) parts.push(f.category);
    if (f.account) parts.push("in " + f.account);
    if (f.search) parts.push('"' + f.search + '"');
    return parts.length ? "Filtered: " + parts.join(" · ") : "All transactions";
  }

  /** Render the category manager, grouped by expense and income. */
  function renderCategoryManager() {
    var wrap = $("categoryLists");
    wrap.innerHTML = "";
    ["expense", "income"].forEach(function (kind) {
      var group = document.createElement("div");
      group.className = "cat-group";

      var heading = document.createElement("div");
      heading.className = "cat-group-head";
      heading.textContent = kind === "expense" ? "Expense" : "Income";
      group.appendChild(heading);

      var items = state.categories.filter(function (c) { return c.kind === kind; });
      if (!items.length) {
        var none = document.createElement("p");
        none.className = "empty";
        none.textContent = "None defined.";
        group.appendChild(none);
      } else {
        var ul = document.createElement("ul");
        ul.className = "cat-list";
        items.forEach(function (c) {
          var li = document.createElement("li");
          li.className = "cat-item" + (c.is_archived ? " is-archived" : "");

          var dot = document.createElement("span");
          dot.className = "legend-dot";
          dot.style.background = c.color;

          var label = document.createElement("span");
          label.className = "cat-name";
          label.textContent = c.name;

          var actions = document.createElement("span");
          actions.className = "cat-actions";

          var archive = document.createElement("button");
          archive.className = "cat-btn";
          archive.type = "button";
          archive.textContent = c.is_archived ? "Restore" : "Archive";
          archive.addEventListener("click", function () {
            patchCategory(c.id, { is_archived: !c.is_archived });
          });

          var del = document.createElement("button");
          del.className = "cat-btn is-danger";
          del.type = "button";
          del.textContent = "Delete";
          del.addEventListener("click", function () { deleteCategory(c); });

          actions.appendChild(archive);
          actions.appendChild(del);
          li.appendChild(dot);
          li.appendChild(label);
          li.appendChild(actions);
          ul.appendChild(li);
        });
        group.appendChild(ul);
      }
      wrap.appendChild(group);
    });
  }

  /**
   * Render the account cards and refresh the net worth headline.
   *
   * @param {Array<Object>} accounts Accounts with balances from the server.
   * @param {number} netWorth The summed balance across every account.
   */
  function renderAccounts(accounts, netWorth) {
    var list = $("accountList");
    list.innerHTML = "";
    countUp($("netWorth"), netWorth || 0, FX.money);

    if (!accounts.length) {
      list.innerHTML = '<li class="empty">No accounts yet. Add one below to start organising your money.</li>';
      $("accountsSub").textContent = "Where your money lives";
      return;
    }

    accounts.forEach(function (a) {
      var li = document.createElement("li");
      li.className = "account-card" + (a.is_archived ? " is-archived" : "");
      li.style.setProperty("--c", a.color || "#5eead4");

      var top = document.createElement("div");
      top.className = "account-top";
      var name = document.createElement("span");
      name.className = "account-name";
      name.textContent = a.name;
      var kind = document.createElement("span");
      kind.className = "account-kind";
      kind.textContent = a.kind;
      top.appendChild(name);
      top.appendChild(kind);

      var balance = document.createElement("div");
      balance.className = "account-balance" + (a.balance < 0 ? " is-neg" : "");
      balance.textContent = FX.money(a.balance);

      var meta = document.createElement("div");
      meta.className = "account-meta";
      meta.textContent = FX.int(a.transaction_count) + " entries · " +
        FX.signed(a.net) + " net";

      var actions = document.createElement("div");
      actions.className = "account-actions";
      var archive = document.createElement("button");
      archive.type = "button";
      archive.className = "cat-btn";
      archive.textContent = a.is_archived ? "Restore" : "Archive";
      archive.addEventListener("click", function () {
        patchAccount(a.id, { is_archived: !a.is_archived });
      });
      var del = document.createElement("button");
      del.type = "button";
      del.className = "cat-btn is-danger";
      del.textContent = "Delete";
      del.addEventListener("click", function () { deleteAccount(a); });
      actions.appendChild(archive);
      actions.appendChild(del);

      li.appendChild(top);
      li.appendChild(balance);
      li.appendChild(meta);
      li.appendChild(actions);
      list.appendChild(li);
    });

    var active = accounts.filter(function (a) { return !a.is_archived; }).length;
    $("accountsSub").textContent = active + " active · " + accounts.length + " total";
  }

  /** Populate the modal account select and the ledger account filter. */
  function renderAccountPickers() {
    var accounts = state.accounts;

    var tx = $("txAccount");
    var txCurrent = tx.value;
    tx.innerHTML = "";
    accounts.forEach(function (a) {
      var opt = document.createElement("option");
      opt.value = a.name;
      opt.textContent = a.is_archived ? a.name + " (archived)" : a.name;
      tx.appendChild(opt);
    });
    if (txCurrent && accounts.some(function (a) { return a.name === txCurrent; })) {
      tx.value = txCurrent;
    }

    var filter = $("filterAccount");
    var current = filter.value;
    var known = accounts.map(function (a) { return a.name; });
    if (filter.dataset.sig !== known.join("|")) {
      filter.dataset.sig = known.join("|");
      filter.innerHTML = '<option value="">All accounts</option>';
      accounts.forEach(function (a) {
        var opt = document.createElement("option");
        opt.value = a.name;
        opt.textContent = a.name;
        filter.appendChild(opt);
      });
      filter.value = current;
    }
  }

  /** Populate the transaction category datalist and the budget category select. */
  function renderCategoryPickers() {
    var list = $("categoryPresets");
    list.innerHTML = "";
    state.categories
      .filter(function (c) { return c.kind === state.modalType && !c.is_archived; })
      .forEach(function (c) {
        var opt = document.createElement("option");
        opt.value = c.name;
        list.appendChild(opt);
      });

    var select = $("budgetCategory");
    var current = select.value;
    var expenseCats = state.categories.filter(function (c) { return c.kind === "expense" && !c.is_archived; });
    select.innerHTML = '<option value="">Select category…</option>';
    expenseCats.forEach(function (c) {
      var opt = document.createElement("option");
      opt.value = c.name;
      opt.textContent = c.name;
      select.appendChild(opt);
    });
    select.value = current;
  }

  /** Fetch every dataset the dashboard needs and render the page. */
  function loadAll() {
    setStatus("", "syncing");
    var monthQ = qs({ months: state.months });

    return Promise.all([
      api("/api/summary"),
      api("/api/timeseries" + monthQ),
      api("/api/breakdown" + qs({ months: state.months, type: state.breakdownType })),
      api("/api/daily" + qs({ days: 30 })),
      api("/api/transactions" + qs({
        limit: state.limit,
        offset: state.offset,
        type: state.filters.type,
        category: state.filters.category,
        account: state.filters.account,
        search: state.filters.search,
        sort: state.filters.sort,
        order: state.filters.order,
      })),
      api("/api/categories" + qs({ include_archived: true })),
      api("/api/accounts" + qs({ include_archived: true })),
    ]).then(function (res) {
      state.categories = res[5].categories || [];
      state.accounts = res[6].accounts || [];
      renderSummary(res[0]);
      renderSeries(res[1].series);
      renderBreakdown(res[2].categories);
      renderDaily(res[3].series);
      renderLedger(res[4]);
      renderInsights(res[0]);
      renderForecast(res[0]);
      renderBudgets(res[0].budgets);
      renderAccounts(state.accounts, res[6].net_worth);
      renderCategoryManager();
      renderCategoryPickers();
      renderAccountPickers();
      setStatus("is-live", "live");
    }).catch(function (err) {
      setStatus("is-error", "error");
      toast(err.message, "error");
    });
  }

  /**
   * Delete a transaction and refresh the dashboard.
   *
   * @param {number} id The transaction id.
   */
  function removeTransaction(id) {
    api("/api/transactions/" + id, { method: "DELETE" })
      .then(function () {
        toast("Transaction deleted", "success");
        if (state.offset > 0 && state.offset >= state.total) {
          state.offset = Math.max(0, state.offset - state.limit);
        }
        return loadAll();
      })
      .catch(function (e) { toast(e.message, "error"); });
  }

  /**
   * Validate and submit the transaction form, creating or updating as needed.
   *
   * @param {SubmitEvent} event The form submission event.
   */
  function submitTransaction(event) {
    event.preventDefault();
    var error = $("txError");
    error.hidden = true;

    var raw = $("txAmount").value.replace(",", ".").trim();
    var payload = {
      amount: raw,
      type: state.modalType,
      category: $("txCategory").value,
      account: $("txAccount").value,
      description: $("txDescription").value.trim() || null,
      date: $("txDate").value,
    };

    if (!raw || isNaN(Number(raw)) || Number(raw) <= 0) {
      error.textContent = "Enter an amount greater than zero.";
      error.hidden = false;
      return;
    }
    if (!payload.category.trim()) {
      error.textContent = "Choose a category.";
      error.hidden = false;
      return;
    }
    if (!payload.account) {
      error.textContent = "Choose an account.";
      error.hidden = false;
      return;
    }
    if (!payload.date) {
      error.textContent = "Pick a date.";
      error.hidden = false;
      return;
    }

    var isEdit = state.editingId !== null;
    var url = isEdit ? "/api/transactions/" + state.editingId : "/api/transactions";
    var method = isEdit ? "PUT" : "POST";
    var btn = $("txSubmit");
    btn.disabled = true;
    btn.textContent = isEdit ? "Updating…" : "Saving…";

    api(url, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function () {
      closeModal();
      state.offset = 0;
      toast(isEdit ? "Transaction updated"
        : (state.modalType === "income" ? "Income added" : "Expense added"), "success");
      return loadAll();
    }).catch(function (e) {
      error.textContent = e.message;
      error.hidden = false;
      btn.disabled = false;
      btn.textContent = isEdit ? "Save changes" : "Save transaction";
    });
  }

  /**
   * Create a category from the category form.
   *
   * @param {SubmitEvent} event The form submission event.
   */
  function createCategory(event) {
    event.preventDefault();
    var name = $("catName").value.trim();
    if (!name) return;

    api("/api/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: name,
        kind: $("catKind").value,
        color: $("catColor").value,
      }),
    }).then(function (c) {
      toast("Category '" + c.name + "' created", "success");
      $("catName").value = "";
      return loadAll();
    }).catch(function (e) { toast(e.message, "error"); });
  }

  /**
   * Apply a partial update to a category.
   *
   * @param {number} id The category id.
   * @param {Object} patch Fields to change.
   */
  function patchCategory(id, patch) {
    api("/api/categories/" + id, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }).then(function () {
      toast("Category updated", "success");
      return loadAll();
    }).catch(function (e) { toast(e.message, "error"); });
  }

  /**
   * Confirm and delete a category.
   *
   * @param {Object} category The category to delete.
   */
  function deleteCategory(category) {
    if (!window.confirm("Delete category '" + category.name + "'? This cannot be undone.")) return;
    api("/api/categories/" + category.id, { method: "DELETE" })
      .then(function () {
        toast("Category '" + category.name + "' deleted", "success");
        return loadAll();
      })
      .catch(function (e) { toast(e.message, "error"); });
  }

  /**
   * Create an account from the account form.
   *
   * @param {SubmitEvent} event The form submission event.
   */
  function createAccount(event) {
    event.preventDefault();
    var name = $("accountName").value.trim();
    if (!name) return;

    var opening = $("accountOpening").value.replace(",", ".").trim();
    api("/api/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: name,
        kind: $("accountKind").value,
        color: $("accountColor").value,
        opening_balance: opening === "" ? "0" : opening,
      }),
    }).then(function (a) {
      toast("Account '" + a.name + "' created", "success");
      $("accountName").value = "";
      $("accountOpening").value = "";
      return loadAll();
    }).catch(function (e) { toast(e.message, "error"); });
  }

  /**
   * Apply a partial update to an account, such as archiving it.
   *
   * @param {number} id The account id.
   * @param {Object} patch Fields to change.
   */
  function patchAccount(id, patch) {
    api("/api/accounts/" + id, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }).then(function () {
      toast("Account updated", "success");
      return loadAll();
    }).catch(function (e) { toast(e.message, "error"); });
  }

  /**
   * Confirm and delete an account.
   *
   * @param {Object} account The account to delete.
   */
  function deleteAccount(account) {
    if (!window.confirm("Delete account '" + account.name + "'? This cannot be undone.")) return;
    api("/api/accounts/" + account.id, { method: "DELETE" })
      .then(function () {
        toast("Account '" + account.name + "' deleted", "success");
        return loadAll();
      })
      .catch(function (e) { toast(e.message, "error"); });
  }

  /**
   * Set the monthly limit for the selected budget category.
   *
   * @param {SubmitEvent} event The form submission event.
   */
  function submitBudget(event) {
    event.preventDefault();
    var category = $("budgetCategory").value;
    var limit = $("budgetLimit").value.replace(",", ".").trim();
    if (!category || limit === "" || Number(limit) < 0) return;

    api("/api/budgets/" + encodeURIComponent(category), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: limit }),
    }).then(function () {
      toast("Budget set for " + category, "success");
      $("budgetLimit").value = "";
      return loadAll();
    }).catch(function (e) { toast(e.message, "error"); });
  }

  /** Download a full JSON backup of the user's data. */
  function exportBackup() {
    toast("Preparing backup…", "info");
    window.location.href = "/api/export";
  }

  /**
   * Read a selected JSON backup and import it, merging rather than replacing.
   *
   * @param {Event} event The change event from the hidden file input.
   */
  function importBackup(event) {
    var file = event.target.files && event.target.files[0];
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      var parsed;
      try {
        parsed = JSON.parse(String(reader.result));
      } catch (e) {
        toast("That file is not valid JSON", "error");
        return;
      }
      var payload = {
        replace: false,
        accounts: parsed.accounts || [],
        categories: parsed.categories || [],
        transactions: parsed.transactions || [],
        budgets: parsed.budgets || [],
      };
      if (!payload.transactions.length && !payload.categories.length && !payload.accounts.length) {
        toast("No transactions, categories, or accounts found in that file", "error");
        return;
      }
      api("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function (stats) {
        toast("Imported " + stats.transactions + " transactions, " +
          stats.categories + " categories" +
          (stats.skipped ? " (" + stats.skipped + " skipped)" : ""), "success");
        state.offset = 0;
        return loadAll();
      }).catch(function (e) { toast(e.message, "error"); });
    };
    reader.readAsText(file);
    event.target.value = "";
  }

  /**
   * Open the transaction modal, prefilled when editing an existing row.
   *
   * @param {Object} [transaction] The transaction to edit, or omit to create.
   */
  function openModal(transaction) {
    $("modalBackdrop").hidden = false;

    if (transaction) {
      state.editingId = transaction.id;
      state.modalType = transaction.type === "income" ? "income" : "expense";
      $("txAmount").value = String(transaction.amount);
      $("txCategory").value = transaction.category;
      $("txAccount").value = transaction.account || "Main";
      $("txDescription").value = transaction.description || "";
      $("txDate").value = transaction.date;
      $("modalTitle").textContent = "Edit transaction";
      $("txSubmit").textContent = "Save changes";
      setModalType(state.modalType);
    } else {
      state.editingId = null;
      $("txForm").reset();
      var today = new Date();
      $("txDate").value = today.getFullYear() + "-" +
        String(today.getMonth() + 1).padStart(2, "0") + "-" +
        String(today.getDate()).padStart(2, "0");
      $("modalTitle").textContent = "New transaction";
      $("txSubmit").textContent = "Save transaction";
      setModalType("expense");
    }
    $("txError").hidden = true;
    $("txAmount").focus();
    $("txAmount").select();
  }

  /** Close and reset the transaction modal. */
  function closeModal() {
    $("modalBackdrop").hidden = true;
    $("txForm").reset();
    $("txError").hidden = true;
    state.editingId = null;
    $("modalTitle").textContent = "New transaction";
    $("txSubmit").textContent = "Save transaction";
  }

  /**
   * Switch the modal between expense and income, refreshing its categories.
   *
   * @param {string} type Either "expense" or "income".
   */
  function setModalType(type) {
    state.modalType = type;
    Array.prototype.forEach.call(document.querySelectorAll(".type-toggle .seg"), function (s) {
      s.classList.toggle("is-active", s.dataset.type === type);
    });
    renderCategoryPickers();
  }

  /**
   * Return the month name preceding a "YYYY-MM" key.
   *
   * @param {string} key The current month key.
   * @returns {string} The previous month's name.
   */
  function priorMonthLabel(key) {
    var parts = key.split("-");
    var d = new Date(Number(parts[0]), Number(parts[1]) - 2, 1);
    return d.toLocaleDateString(undefined, { month: "long" });
  }

  /**
   * Escape a string for safe insertion into HTML.
   *
   * @param {*} value The value to escape.
   * @returns {string} The HTML escaped string.
   */
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /** Bind the ledger search, filter, and pagination controls. */
  function bindFilters() {
    var timer = null;
    $("filterSearch").addEventListener("input", function (e) {
      clearTimeout(timer);
      var value = e.target.value;
      timer = setTimeout(function () {
        state.filters.search = value;
        state.offset = 0;
        loadAll();
      }, 260);
    });

    [
      ["filterType", "type"], ["filterCategory", "category"],
      ["filterAccount", "account"], ["filterSort", "sort"], ["filterOrder", "order"],
    ].forEach(function (pair) {
      $(pair[0]).addEventListener("change", function (e) {
        state.filters[pair[1]] = e.target.value;
        state.offset = 0;
        loadAll();
      });
    });

    $("pagePrev").addEventListener("click", function () {
      state.offset = Math.max(0, state.offset - state.limit);
      loadAll();
    });
    $("pageNext").addEventListener("click", function () {
      state.offset += state.limit;
      loadAll();
    });
  }

  /** Bind the time range, series toggle, and section navigation controls. */
  function bindRanges() {
    var segs = document.querySelectorAll(".hero-controls .seg");
    Array.prototype.forEach.call(segs, function (seg) {
      seg.addEventListener("click", function () {
        Array.prototype.forEach.call(segs, function (s) { s.classList.remove("is-active"); });
        seg.classList.add("is-active");
        state.months = Number(seg.dataset.months);
        loadAll();
      });
    });

    var chips = document.querySelectorAll(".chip[data-series]");
    Array.prototype.forEach.call(chips, function (chip) {
      chip.addEventListener("click", function () {
        var key = chip.dataset.series;
        var idx = state.hiddenSeries.indexOf(key);
        if (idx === -1) state.hiddenSeries.push(key);
        else state.hiddenSeries.splice(idx, 1);
        chip.classList.toggle("is-active", idx !== -1);
        charts.cashflow.opts.hidden = state.hiddenSeries;
        charts.cashflow.draw();
      });
    });

    var navLinks = document.querySelectorAll(".topnav-link");
    Array.prototype.forEach.call(navLinks, function (link) {
      link.addEventListener("click", function () {
        Array.prototype.forEach.call(navLinks, function (l) { l.classList.remove("is-active"); });
        link.classList.add("is-active");
      });
    });
  }

  /** Start the header clock, updating once a second. */
  function startClock() {
    var el = $("clock");
    function tick() {
      var d = new Date();
      el.textContent = [d.getHours(), d.getMinutes(), d.getSeconds()]
        .map(function (n) { return String(n).padStart(2, "0"); }).join(":");
    }
    tick();
    setInterval(tick, 1000);
  }

  /** Initialise charts, listeners, and the first data load. */
  function init() {
    initCharts();
    bindFilters();
    bindRanges();
    startClock();
    observeReveals();

    $("openModal").addEventListener("click", function () { openModal(); });
    $("closeModal").addEventListener("click", closeModal);
    $("cancelModal").addEventListener("click", closeModal);
    $("modalBackdrop").addEventListener("click", function (e) {
      if (e.target === $("modalBackdrop")) closeModal();
    });
    $("txForm").addEventListener("submit", submitTransaction);
    $("budgetForm").addEventListener("submit", submitBudget);
    $("categoryForm").addEventListener("submit", createCategory);
    $("accountForm").addEventListener("submit", createAccount);
    $("exportBtn").addEventListener("click", exportBackup);
    $("importBtn").addEventListener("click", function () { $("importFile").click(); });
    $("importFile").addEventListener("change", importBackup);

    Array.prototype.forEach.call(document.querySelectorAll(".type-toggle .seg"), function (seg) {
      seg.addEventListener("click", function () { setModalType(seg.dataset.type); });
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !$("modalBackdrop").hidden) closeModal();
    });

    loadAll();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
