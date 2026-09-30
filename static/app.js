/**
 * @file Dashboard controller for Finlify.
 *
 * Wires the single page interface to the JSON API: loading and rendering the
 * summary, charts, budgets, insights and ledger, plus the mutations for
 * transactions, categories, budgets, and backup import and export.
 *
 * All rendered figures come from the server, which owns the authoritative
 * integer cent arithmetic. This file formats them for display only.
 *
 * The dashboard is only started once `auth.js` reports a signed-in user, so
 * every request below already carries a session.
 */

(function () {
  "use strict";

  var FX = window.FX;
  var C = window.FinlifyCharts;
  var auth = window.FinlifyAuth;

  var state = {
    user: null,
    months: 6,
    hiddenSeries: [],
    breakdownType: "expense",
    modalType: "expense",
    editingId: null,
    editingGoalId: null,
    offset: 0,
    limit: 25,
    total: 0,
    categories: [],
    accounts: [],
    goals: [],
    seriesColours: null,
    filters: {
      search: "", type: "", category: "", account: "",
      from: "", to: "", min: "", max: "",
      sort: "date", order: "desc",
    },
  };

  var started = false;
  var charts = {};
  var chartsReady = true;
  var dialogReturnFocus = null;
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
   * Session expiry is handled centrally by the gate, which is also why every
   * 401 shows the sign-in screen instead of a dashboard full of errors.
   *
   * @param {string} path Request path, including any query string.
   * @param {RequestInit} [options] Fetch options such as method and body.
   * @returns {Promise<Object>} The parsed response body.
   */
  function api(path, options) {
    return auth.request(path, options);
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
   * Compute the initials shown inside the header avatar.
   *
   * @param {Object} user The signed-in user record.
   * @returns {string} One or two letters derived from the display name.
   */
  function avatarInitials(user) {
    var name = (user.display_name || user.username || "?") .trim();
    var parts = name.split(/\s+/).filter(Boolean);
    var first = (parts[0] || "?").charAt(0);
    var last = parts.length > 1 ? parts[parts.length - 1].charAt(0) : "";
    return (first + last).toUpperCase();
  }

  /**
   * Mirror a user's identity into the header chip and the account popover.
   *
   * @param {Object} user The signed-in user record.
   */
  function renderUser(user) {
    var label = user.display_name || user.username || "Account";
    var initials = avatarInitials(user);
    $("userAvatar").textContent = initials;
    $("userAvatarLarge").textContent = initials;
    $("userName").textContent = label;
    $("userPopName").textContent = label;
    $("userPopUsername").textContent = "@" + user.username;
  }

  /**
   * Open the account popover beneath the header chip.
   */
  function openUserPop() {
    $("userPop").hidden = false;
    $("userChip").setAttribute("aria-expanded", "true");
  }

  /**
   * Close the account popover.
   */
  function closeUserPop() {
    $("userPop").hidden = true;
    $("userChip").setAttribute("aria-expanded", "false");
  }

  /**
   * Toggle the account popover.
   */
  function toggleUserPop() {
    if ($("userPop").hidden) openUserPop();
    else closeUserPop();
  }

  /**
   * Open the settings sheet and fill it from the signed-in user.
   */
  function openSettings() {
    if ($("settingsBackdrop").hidden) dialogReturnFocus = document.activeElement;
    closeUserPop();
    var user = state.user;
    $("settingsName").value = user.display_name || "";
    $("settingsUsername").value = user.username;
    auth.fillCurrencies("settingsCurrency");
    auth.markTheme(user.theme);
    $("passwordError").hidden = true;
    $("adminBlock").hidden = !user.is_admin;
    if (user.is_admin) loadPeople();
    $("settingsBackdrop").hidden = false;
    setBackgroundInert(true);
    $("settingsName").focus();
  }

  /**
   * Close the settings sheet.
   */
  function closeSettings() {
    $("settingsBackdrop").hidden = true;
    setBackgroundInert(false);
    if (dialogReturnFocus && document.contains(dialogReturnFocus)) {
      dialogReturnFocus.focus();
    }
    dialogReturnFocus = null;
  }

  /**
   * Save the profile and preference fields.
   */
  function savePreferences() {
    var user = state.user;
    var theme = "dark";
    Array.prototype.forEach.call(
      document.querySelectorAll("#settingsThemes .seg"),
      function (seg) {
        if (seg.classList.contains("is-active")) theme = seg.dataset.themeChoice;
      }
    );
    auth.saveSettings({
      display_name: $("settingsName").value.trim() || null,
      currency: $("settingsCurrency").value || user.currency,
      theme: theme,
    })
      .then(function () {
        toast("Preferences saved", "success");
        renderUser(state.user);
      })
      .catch(function (e) { toast(e.message, "error"); });
  }

  /**
   * Change the signed-in user's password.
   */
  function changePassword() {
    var error = $("passwordError");
    error.hidden = true;
    api("/api/settings/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current_password: $("passwordCurrent").value,
        new_password: $("passwordNext").value,
      }),
    })
      .then(function () {
        $("passwordForm").reset();
        toast("Password changed", "success");
      })
      .catch(function (e) {
        error.textContent = e.message;
        error.hidden = false;
      });
  }

  /**
   * Load every local account for the administrator's people list.
   */
  function loadPeople() {
    api("/api/users")
      .then(function (payload) { renderPeople(payload.users || []); })
      .catch(function (e) { toast(e.message, "error"); });
  }

  /**
   * Render the people list with reset and delete controls.
   *
   * @param {Array} users Every local account, self included.
   */
  function renderPeople(users) {
    var list = $("peopleList");
    list.innerHTML = "";
    users.forEach(function (u) {
      var li = document.createElement("li");
      li.className = "person" + (u.id === state.user.id ? " is-self" : "");
      li.dataset.userId = u.id;

      var id = document.createElement("div");
      id.className = "person-id";
      var name = document.createElement("span");
      name.className = "person-name";
      name.textContent = u.display_name || u.username;
      var sub = document.createElement("span");
      sub.className = "person-sub";
      sub.textContent = "@" + u.username;
      id.appendChild(name);
      id.appendChild(sub);
      li.appendChild(id);

      if (u.is_admin) {
        var badge = document.createElement("span");
        badge.className = "person-badge";
        badge.textContent = "owner";
        li.appendChild(badge);
      }

      var actions = document.createElement("div");
      actions.className = "person-actions";
      if (u.id !== state.user.id) {
        var reset = document.createElement("button");
        reset.className = "btn btn-mini";
        reset.type = "button";
        reset.textContent = "Reset password";
        reset.addEventListener("click", function () {
          var input = li.querySelector(".person-password input");
          if (input) {
            var row = li.querySelector(".person-password");
            row.hidden = !row.hidden;
            if (!row.hidden) input.focus();
          }
        });
        actions.appendChild(reset);

        var del = document.createElement("button");
        del.className = "btn btn-mini btn-danger";
        del.type = "button";
        del.textContent = "Delete";
        del.addEventListener("click", function () {
          var keep = window.confirm(
            "Delete the account \"" + (u.display_name || u.username) +
            "\" and every transaction, category, budget, and account it owns?"
          );
          if (!keep) return;
          api("/api/users/" + u.id, { method: "DELETE" })
            .then(function () {
              toast("Account deleted", "success");
              loadPeople();
            })
            .catch(function (e) { toast(e.message, "error"); });
        });
        actions.appendChild(del);

        var row = document.createElement("div");
        row.className = "person-password";
        row.hidden = true;
        var input = document.createElement("input");
        input.type = "password";
        input.placeholder = "New password, 8+ characters";
        input.autocomplete = "new-password";
        input.minLength = 8;
        var save = document.createElement("button");
        save.className = "btn btn-mini btn-primary";
        save.type = "button";
        save.textContent = "Save";
        save.addEventListener("click", function () {
          if (!input.value || input.value.length < 8) {
            toast("New passwords need at least 8 characters", "error");
            return;
          }
          api("/api/users/" + u.id + "/password", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password: input.value }),
          })
            .then(function () {
              toast("Password updated for " + (u.display_name || u.username), "success");
              row.hidden = true;
              input.value = "";
            })
            .catch(function (e) { toast(e.message, "error"); });
        });
        row.appendChild(input);
        row.appendChild(save);
        li.appendChild(row);
      }
      li.appendChild(actions);
      list.appendChild(li);
    });
  }

  /**
   * Create a new local account from the people form.
   */
  function createPerson() {
    var error = $("peopleError");
    error.hidden = true;
    api("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: $("personName").value.trim(),
        password: $("personPassword").value,
      }),
    })
      .then(function () {
        $("peopleForm").reset();
        toast("Account created", "success");
        loadPeople();
      })
      .catch(function (e) {
        error.textContent = e.message;
        error.hidden = false;
      });
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
   * Report a non-fatal failure to the user and to the console.
   *
   * @param {string} message What went wrong, in the user's terms.
   * @param {Error} [err] The underlying error, logged for diagnosis.
   */
  function logError(message, err) {
    if (err) console.error(message, err);
    toast(message, "error");
  }

  /**
   * Show a transient notification, optionally with an action button.
   *
   * A toast carrying an action lingers longer and pauses its own dismissal while
   * the pointer is over it, so the action cannot vanish from under the cursor.
   *
   * @param {string} message The message text.
   * @param {string} [kind] One of "info", "success", or "error".
   * @param {{label: string, onClick: function(): void}} [action] Optional action.
   */
  function toast(message, kind, action) {
    var el = document.createElement("div");
    el.className = "toast " + (kind || "info");
    el.innerHTML = '<span class="toast-dot"></span><span class="toast-msg"></span>';
    el.querySelector(".toast-msg").textContent = message;

    var timer = null;
    var dismissed = false;
    function dismiss() {
      if (dismissed) return;
      dismissed = true;
      clearTimeout(timer);
      el.classList.add("is-out");
      setTimeout(function () { el.remove(); }, 300);
    }

    if (action) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "toast-action";
      btn.textContent = action.label;
      btn.addEventListener("click", function () {
        dismiss();
        action.onClick();
      });
      el.appendChild(btn);
      el.addEventListener("mouseenter", function () { clearTimeout(timer); });
      el.addEventListener("mouseleave", function () { timer = setTimeout(dismiss, 2000); });
    }

    $("toasts").appendChild(el);
    timer = setTimeout(dismiss, action ? 8000 : 3800);
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

  /**
   * Render a change in a percentage rate as percentage points.
   *
   * A rate moving from 10% to 20% is a gain of ten points, not a gain of one
   * hundred percent, so this deliberately does not reuse {@link setDelta}, which
   * reports a relative change.
   *
   * @param {HTMLElement} el The badge element.
   * @param {number|null} value Signed change in percentage points, or null when
   *   there is no prior month to compare against.
   */
  function setRateDelta(el, value) {
    if (!el) return;
    if (value === null || value === undefined || !isFinite(value)) {
      el.className = "delta flat";
      el.textContent = "no prior month";
      return;
    }
    var flat = Math.abs(value) < 0.05;
    var up = value > 0;
    el.className = "delta " + (flat ? "flat" : up ? "up" : "down");
    el.textContent = (flat ? "→ " : up ? "▲ " : "▼ ") + Math.abs(value).toFixed(1) + " pts";
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
    var series = {
      income: FX.token("--series-1", "#5eead4"),
      violet: FX.token("--series-2", "#a78bfa"),
      pink: FX.token("--series-3", "#f0abfc"),
      lime: FX.token("--series-4", "#bef264"),
    };
    state.seriesColours = series;

    charts.cashflow = new C.AreaChart($("cashflowChart"), {
      incomeColor: series.income,
      expenseColor: series.pink,
      hidden: state.hiddenSeries,
      onHover: showCashflowTip,
    });
    charts.category = new C.DonutChart($("categoryChart"), {
      onHover: function (item) { highlightLegend(item ? item.category : null); },
    });
    charts.daily = new C.BarChart($("dailyChart"), { onHover: showDailyTip });
    charts.daily.opts.color = series.violet;
    charts.networth = new C.LineChart($("netWorthChart"), {
      color: series.lime,
      emptyText: "No net worth recorded yet",
      onHover: showNetWorthTip,
    });
    charts.sparks = {
      balance: new C.Sparkline($("sparkBalance"), { color: series.income }),
      income: new C.Sparkline($("sparkIncome"), { color: series.violet }),
      expenses: new C.Sparkline($("sparkExpenses"), { color: series.pink }),
      savings: new C.Sparkline($("sparkSavings"), { color: series.lime }),
    };
  }

  /**
   * Whether the canvas charts are usable.
   *
   * Charts are the one part of the interface that can fail to construct, so the
   * renderers that touch them check this and skip. The tables, totals, and forms
   * around them carry on working.
   *
   * @returns {boolean} True when the charts were built successfully.
   */
  function hasCharts() {
    return chartsReady && !!charts.cashflow;
  }

  /**
   * Repaint every chart after the theme changed under it.
   *
   * Series colours come from theme tokens, so they are re-read here and pushed
   * into the existing charts rather than rebuilding them.
   */
  function repaintCharts() {
    if (!hasCharts() || !state.seriesColours) return;
    var series = state.seriesColours;
    series.income = FX.token("--series-1", series.income);
    series.violet = FX.token("--series-2", series.violet);
    series.pink = FX.token("--series-3", series.pink);
    series.lime = FX.token("--series-4", series.lime);
    charts.cashflow.opts.incomeColor = series.income;
    charts.cashflow.opts.expenseColor = series.pink;
    if (charts.daily) charts.daily.opts.color = series.violet;
    if (charts.networth) charts.networth.opts.color = series.lime;
    if (charts.sparks) {
      charts.sparks.balance.opts.color = series.income;
      charts.sparks.income.opts.color = series.violet;
      charts.sparks.expenses.opts.color = series.pink;
      charts.sparks.savings.opts.color = series.lime;
    }
    C.refresh();
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
    if (hasCharts() && charts.cashflow.hoverIndex >= 0 && charts.cashflow.data[charts.cashflow.hoverIndex] === item) {
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
   * Position and fill the net worth tooltip for the hovered month.
   *
   * @param {Object|null} item The hovered month datum, or null to hide.
   */
  function showNetWorthTip(item) {
    var tip = $("netWorthTip");
    if (!item) { tip.hidden = true; return; }
    tip.innerHTML =
      '<div class="tip-row"><span class="tip-key">' + item.label + " " + String(item.year).slice(2) + "</span></div>" +
      '<div class="tip-row"><span class="tip-key">Net worth</span><strong>' + FX.money(item.value) + "</strong></div>" +
      '<div class="tip-row"><span class="tip-key">Change</span><span class="tip-val ' +
      (item.change >= 0 ? "up" : "down") + '">' + FX.signed(item.change) + "</span></div>";
    if (hasCharts() && charts.networth && charts.networth.hoverIndex >= 0 &&
        charts.networth.data[charts.networth.hoverIndex] === item) {
      var scale = charts.networth.scale();
      tip.style.left = FX.clamp(scale.xFor(charts.networth.hoverIndex), 70, charts.networth.w - 70) + "px";
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
    if (hasCharts() && charts.daily.hoverIndex >= 0) {
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

    var hasPriorMonth = data.month.savings_rate !== 0 || data.month.prev_savings_rate !== 0;
    setRateDelta($("statSavingsDelta"), hasPriorMonth
      ? data.month.savings_rate - data.month.prev_savings_rate
      : null);

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

    var now = new Date();
    $("heroEyebrow").textContent =
      "Your private ledger · " +
      now.toLocaleDateString(undefined, { month: "long", year: "numeric" });

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
    if (hasCharts()) {
      charts.cashflow.opts.hidden = state.hiddenSeries;
      charts.cashflow.setData(series);
      charts.sparks.balance.setData(series.map(function (s) { return s.net; }));
      charts.sparks.income.setData(series.map(function (s) { return s.income; }));
      charts.sparks.expenses.setData(series.map(function (s) { return s.expenses; }));
      charts.sparks.savings.setData(series.map(function (s) {
        return s.income ? (s.net / s.income) * 100 : 0;
      }));
    }
    $("cashflowSub").textContent = "Income vs expenses · last " + series.length + " months";
    $("cashflowChart").setAttribute(
      "aria-label",
      "Cash flow: income versus expenses over the last " + series.length + " months"
    );
  }

  /**
   * Render the net worth line chart and the headline change beside it.
   *
   * @param {Array<Object>} series One net worth point per month.
   */
  function renderNetWorth(series) {
    var points = series.map(function (p) {
      return {
        label: p.label,
        year: p.year,
        value: p.net_worth,
        change: p.change,
        change_cents: p.change_cents,
      };
    });
    if (hasCharts() && charts.networth) charts.networth.setData(points);

    var last = series[series.length - 1];
    var change = $("netWorthChange");
    if (last) {
      change.textContent = FX.signed(last.change);
      change.className = "accounts-net-value " + (last.change > 0 ? "is-up" : last.change < 0 ? "is-neg" : "");
    } else {
      change.textContent = "—";
      change.className = "accounts-net-value";
    }

    $("netWorthSub").textContent = "What you hold · last " + series.length + " months";
    $("netWorthChart").setAttribute(
      "aria-label",
      "Net worth over the last " + series.length + " months. " + (last
        ? "Currently " + FX.money(last.net_worth) + ", " +
          (last.change > 0 ? "up " : last.change < 0 ? "down " : "unchanged ") +
          FX.money(Math.abs(last.change)) + " since last month."
        : "Nothing recorded yet.")
    );
  }

  /**
   * Render the donut chart and its legend.
   *
   * @param {Array<Object>} categories Per category totals and shares.
   */
  function renderBreakdown(categories) {
    if (hasCharts()) charts.category.setData(categories);
    var legend = $("categoryLegend");
    legend.innerHTML = "";

    if (!categories.length) {
      legend.innerHTML = '<li class="empty">Nothing recorded for this range yet.</li>';
      $("breakdownSub").textContent = "By category";
      $("categoryChart").setAttribute(
        "aria-label", "Spending mix by category. Nothing recorded for this range."
      );
      return;
    }

    categories.forEach(function (c) {
      var color = FX.safeColor(c.color, "#5eead4");
      var li = document.createElement("li");
      li.className = "legend-item";
      li.dataset.category = c.category;
      li.style.setProperty("--accent", color);

      var dot = document.createElement("span");
      dot.className = "legend-dot";
      dot.style.background = color;

      var name = document.createElement("span");
      name.className = "legend-name";
      name.textContent = c.category;

      var value = document.createElement("span");
      value.className = "legend-value";
      value.textContent = FX.money(c.total);

      var pct = document.createElement("span");
      pct.className = "legend-pct";
      pct.textContent = c.pct.toFixed(1) + "%";

      li.appendChild(dot);
      li.appendChild(name);
      li.appendChild(value);
      li.appendChild(pct);
      legend.appendChild(li);
    });

    var total = categories.reduce(function (s, c) { return s + c.total; }, 0);
    $("breakdownSub").textContent =
      categories.length + " categories · " + FX.money(total) + " total";
    $("categoryChart").setAttribute(
      "aria-label",
      "Spending mix by category. Total " + FX.money(total) + ". Largest: " +
        categories[0].category + " at " + categories[0].pct.toFixed(1) + " percent."
    );
  }

  /**
   * Render the daily pulse bar chart and its summary figures.
   *
   * @param {Array<Object>} series Daily buckets from the daily endpoint.
   */
  function renderDaily(series) {
    if (hasCharts()) charts.daily.setData(series);
    var avg = series.reduce(function (s, d) { return s + d.net; }, 0) / (series.length || 1);
    $("pulseAvg").textContent = FX.signed(avg);
    var peak = series.reduce(function (best, d) {
      return d.net > best.net ? d : best;
    }, series[0] || { net: 0, date: null });
    $("pulsePeak").textContent = peak && peak.date
      ? FX.signed(peak.net) + " · " + FX.dayShort(peak.date) : "—";
    $("dailyChart").setAttribute(
      "aria-label",
      "Daily net spending for the last " + series.length + " days. Average " +
        FX.signed(avg) + " per day."
    );
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
      dot.style.background = FX.safeColor(b.color, "#5eead4");
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
   * Render the goal list with progress bars, deadline states, and row actions.
   *
   * Archived goals are hidden here rather than filtered out upstream, because
   * the same list also reports the totals in its subtitle.
   *
   * @param {Array<Object>} goals Goal rows with their live progress.
   */
  function renderGoals(goals) {
    var list = $("goalList");
    list.innerHTML = "";
    var all = goals || [];
    var live = all.filter(function (g) { return !g.is_archived; });

    if (!all.length) {
      list.innerHTML = '<li class="empty">No goals yet. Add one to start tracking what you are saving towards.</li>';
      $("goalsSub").textContent = "What you are saving towards";
      return;
    }

    all.forEach(function (g) {
      var li = document.createElement("li");
      li.className = "goal-item" +
        (g.state === "reached" ? " is-reached" : g.state === "overdue" ? " is-overdue" : "") +
        (g.is_archived ? " is-archived" : "");
      li.style.setProperty("--c", FX.safeColor(g.color, "#bef264"));

      var top = document.createElement("div");
      top.className = "goal-top";

      var name = document.createElement("span");
      name.className = "goal-name";
      var dot = document.createElement("span");
      dot.className = "legend-dot";
      dot.style.background = FX.safeColor(g.color, "#bef264");
      var nameText = document.createElement("span");
      nameText.textContent = g.name;
      name.appendChild(dot);
      name.appendChild(nameText);

      var badge = document.createElement("span");
      badge.className = "goal-badge";
      badge.textContent = goalBadge(g);
      name.appendChild(badge);

      var nums = document.createElement("span");
      nums.className = "goal-nums";
      nums.textContent = FX.money(g.saved) + " / " + FX.money(g.target);

      top.appendChild(name);
      top.appendChild(nums);

      var meta = document.createElement("span");
      meta.className = "goal-meta";
      meta.textContent = goalMeta(g);

      var track = document.createElement("div");
      track.className = "budget-track";
      var fill = document.createElement("div");
      fill.className = "budget-fill";
      track.appendChild(fill);

      var actions = document.createElement("div");
      actions.className = "goal-actions";

      var edit = document.createElement("button");
      edit.type = "button";
      edit.className = "cat-btn";
      edit.innerHTML = ICON_EDIT;
      edit.setAttribute("aria-label", "Edit " + g.name);
      edit.addEventListener("click", function () { openGoal(g); });

      var archive = document.createElement("button");
      archive.type = "button";
      archive.className = "cat-btn";
      archive.textContent = g.is_archived ? "Restore" : "Archive";
      archive.addEventListener("click", function () {
        patchGoal(g.id, { is_archived: !g.is_archived });
      });

      var del = document.createElement("button");
      del.type = "button";
      del.className = "cat-btn is-danger";
      del.textContent = "Delete";
      del.addEventListener("click", function () { deleteGoal(g); });

      actions.appendChild(edit);
      actions.appendChild(archive);
      actions.appendChild(del);

      li.appendChild(top);
      li.appendChild(track);
      li.appendChild(meta);
      li.appendChild(actions);
      list.appendChild(li);
      requestAnimationFrame(function () { fill.style.width = FX.clamp(g.pct, 0, 100) + "%"; });
    });

    var reached = all.filter(function (g) { return g.state === "reached"; }).length;
    $("goalsSub").textContent = live.length + " active · " + reached + " reached";
  }

  /**
   * The short state label shown beside a goal name.
   *
   * @param {Object} g One goal row.
   * @returns {string} The badge text.
   */
  function goalBadge(g) {
    if (g.is_archived) return "Archived";
    if (g.state === "reached") return "Reached";
    if (g.state === "overdue") return "Overdue";
    if (g.deadline && g.days_left <= 30) return g.days_left + "d left";
    return FX.pct(g.pct, 0);
  }

  /**
   * The supporting line under a goal, explaining what is being measured.
   *
   * @param {Object} g One goal row.
   * @returns {string} The meta line.
   */
  function goalMeta(g) {
    var parts = [];
    parts.push(g.account ? "Tracking " + g.account : "Tracking everything you hold");
    if (g.state === "reached") {
      parts.push("Target met");
    } else if (g.deadline && g.days_left < 0) {
      parts.push("Deadline passed " + FX.date(g.deadline, { day: "numeric", month: "short", year: "numeric" }));
    } else if (g.deadline) {
      parts.push("Due " + FX.date(g.deadline, { day: "numeric", month: "short", year: "numeric" }));
      if (g.needed_per_month) parts.push(FX.money(g.needed_per_month) + " a month to get there");
    } else {
      parts.push(FX.money(g.remaining) + " to go");
    }
    return parts.join(" · ");
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
          e.stopPropagation(); removeTransaction(t);
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
          dot.style.background = FX.safeColor(c.color, "#5eead4");

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
      li.style.setProperty("--c", FX.safeColor(a.color, "#5eead4"));

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

  /**
   * Refill the goal dialog's account picker.
   *
   * The empty option is the meaningful default: a goal with no account measures
   * the whole ledger, which is what someone saving for their first goal wants.
   *
   * @param {string} [keep] An account name to leave selected.
   */
  function renderGoalPickers(keep) {
    var select = $("goalAccount");
    var current = keep === undefined ? select.value : keep;
    select.innerHTML = "";
    var any = document.createElement("option");
    any.value = "";
    any.textContent = "Everything I hold";
    select.appendChild(any);
    state.accounts.forEach(function (a) {
      var opt = document.createElement("option");
      opt.value = a.name;
      opt.textContent = a.is_archived ? a.name + " (archived)" : a.name;
      select.appendChild(opt);
    });
    select.value = current || "";
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

  /**
   * Find an existing category by name, ignoring case and surrounding space.
   *
   * The server normalises names (collapse whitespace, title case), so the name a
   * person types rarely equals the stored one byte for byte.
   *
   * @param {string} name The typed category name.
   * @returns {Object|null} The matching category, or null.
   */
  function findCategory(name) {
    var needle = String(name || "").trim().toLowerCase();
    if (!needle) return null;
    var match = null;
    state.categories.forEach(function (c) {
      if (c.name.toLowerCase() === needle) match = c;
    });
    return match;
  }

  /** Reload the category list and repopulate the pickers that depend on it. */
  function refreshCategories() {
    return api("/api/categories" + qs({ include_archived: true })).then(function (res) {
      state.categories = res.categories || [];
      renderCategoryPickers();
    });
  }

  /**
   * Resolve a typed category name, creating the category if it is new.
   *
   * Saving a transaction used to dead-end on "Unknown category … Create it
   * first." Instead of making the person leave the dialog, this creates the
   * category inline and returns the server's canonical name so the transaction
   * references the right row.
   *
   * @param {string} name The name typed into the category field.
   * @returns {Promise<string>} The canonical name to store on the transaction.
   */
  function ensureCategory(name) {
    var existing = findCategory(name);
    if (existing) return Promise.resolve(existing.name);

    return api("/api/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, kind: state.modalType }),
    }).then(function (created) {
      state.categories.push(created);
      renderCategoryPickers();
      toast("Category '" + created.name + "' created", "success");
      return created.name;
    }).catch(function (e) {
      // A 409 means it already exists, which can happen if another tab created
      // it or the typed spelling only differs by case. Resolve and carry on.
      if (e.status !== 409) throw e;
      return refreshCategories().then(function () {
        var found = findCategory(name);
        if (!found) throw e;
        return found.name;
      });
    });
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
        date_from: state.filters.from,
        date_to: state.filters.to,
        amount_min: state.filters.min,
        amount_max: state.filters.max,
        sort: state.filters.sort,
        order: state.filters.order,
      })),
      api("/api/categories" + qs({ include_archived: true })),
      api("/api/accounts" + qs({ include_archived: true })),
      api("/api/networth" + monthQ),
      api("/api/goals" + qs({ include_archived: true })),
    ]).then(function (res) {
      state.categories = res[5].categories || [];
      state.accounts = res[6].accounts || [];
      state.goals = res[8].goals || [];
      renderSummary(res[0]);
      renderSeries(res[1].series);
      renderNetWorth(res[7].series);
      renderGoals(state.goals);
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
      renderGoalPickers();
      setStatus("is-live", "live");
    }).catch(function (err) {
      setStatus("is-error", "error");
      toast(err.message, "error");
    });
  }

  /**
   * Delete a transaction and refresh the dashboard, offering an undo.
   *
   * Deletion is immediate, and the toast's Undo action recreates the row from
   * the data already in hand. Recreating mints a new id rather than restoring
   * the old one, which is a fair trade for not having to hold the delete open.
   *
   * @param {Object} t The transaction that is being removed.
   */
  function removeTransaction(t) {
    api("/api/transactions/" + t.id, { method: "DELETE" })
      .then(function () {
        if (state.offset > 0 && state.offset >= state.total) {
          state.offset = Math.max(0, state.offset - state.limit);
        }
        return loadAll();
      })
      .then(function () {
        toast("Transaction deleted", "success", {
          label: "Undo",
          onClick: function () { restoreTransaction(t); },
        });
      })
      .catch(function (e) { toast(e.message, "error"); });
  }

  /**
   * Recreate a just-deleted transaction.
   *
   * @param {Object} t The transaction to restore.
   */
  function restoreTransaction(t) {
    api("/api/transactions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        amount: t.amount,
        type: t.type,
        category: t.category,
        account: t.account,
        description: t.description,
        date: t.date,
      }),
    }).then(function () {
      toast("Transaction restored", "success");
      return loadAll();
    }).catch(function (e) { toast(e.message, "error"); });
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
    var categoryName = $("txCategory").value.trim();
    var account = $("txAccount").value;
    var date = $("txDate").value;

    if (!raw || isNaN(Number(raw)) || Number(raw) <= 0) {
      error.textContent = "Enter an amount greater than zero.";
      error.hidden = false;
      return;
    }
    if (!categoryName) {
      error.textContent = "Choose a category.";
      error.hidden = false;
      return;
    }
    if (!account) {
      error.textContent = "Choose an account.";
      error.hidden = false;
      return;
    }
    if (!date) {
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

    // A name the person typed but never saved as a category is created here, so
    // the dialog never dead-ends on "Unknown category".
    ensureCategory(categoryName).then(function (name) {
      return api(url, {
        method: method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          amount: raw,
          type: state.modalType,
          category: name,
          account: account,
          description: $("txDescription").value.trim() || null,
          date: date,
        }),
      });
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
    if ($("modalBackdrop").hidden) dialogReturnFocus = document.activeElement;
    $("modalBackdrop").hidden = false;
    setBackgroundInert(true);
    // A previous save leaves the button disabled until it is re-enabled here;
    // form.reset() does not restore it.
    $("txSubmit").disabled = false;

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
    $("txSubmit").disabled = false;
    setBackgroundInert(false);
    if (dialogReturnFocus && document.contains(dialogReturnFocus)) {
      dialogReturnFocus.focus();
    }
    dialogReturnFocus = null;
  }

  /**
   * Open the goal modal, prefilled when editing an existing goal.
   *
   * @param {Object} [goal] The goal to edit, or omit to create one.
   */
  function openGoal(goal) {
    if ($("goalsBackdrop").hidden) dialogReturnFocus = document.activeElement;
    $("goalsBackdrop").hidden = false;
    setBackgroundInert(true);
    // A previous save leaves the button disabled until it is re-enabled here;
    // form.reset() does not restore it.
    $("goalSubmit").disabled = false;

    if (goal) {
      state.editingGoalId = goal.id;
      $("goalName").value = goal.name;
      $("goalTarget").value = String(goal.target);
      $("goalDeadline").value = goal.deadline || "";
      renderGoalPickers(goal.account || "");
      $("goalsTitle").textContent = "Edit goal";
      $("goalSubmit").textContent = "Save changes";
    } else {
      state.editingGoalId = null;
      $("goalForm").reset();
      renderGoalPickers("");
      $("goalsTitle").textContent = "New goal";
      $("goalSubmit").textContent = "Save goal";
    }
    $("goalError").hidden = true;
    $("goalName").focus();
    $("goalName").select();
  }

  /** Close and reset the goal modal. */
  function closeGoal() {
    $("goalsBackdrop").hidden = true;
    $("goalForm").reset();
    $("goalError").hidden = true;
    state.editingGoalId = null;
    $("goalsTitle").textContent = "New goal";
    $("goalSubmit").textContent = "Save goal";
    $("goalSubmit").disabled = false;
    setBackgroundInert(false);
    if (dialogReturnFocus && document.contains(dialogReturnFocus)) {
      dialogReturnFocus.focus();
    }
    dialogReturnFocus = null;
  }

  /**
   * Create or update a goal from the modal form.
   *
   * @param {Event} event The submit event, prevented so the page does not post.
   */
  function submitGoal(event) {
    event.preventDefault();
    var error = $("goalError");
    error.hidden = true;

    var raw = $("goalTarget").value.replace(",", ".").trim();
    if (!raw || isNaN(Number(raw)) || Number(raw) <= 0) {
      error.textContent = "Enter a target greater than zero.";
      error.hidden = false;
      return;
    }

    var isEdit = state.editingGoalId !== null;
    var url = isEdit ? "/api/goals/" + state.editingGoalId : "/api/goals";
    var method = isEdit ? "PUT" : "POST";
    var btn = $("goalSubmit");
    btn.disabled = true;
    btn.textContent = isEdit ? "Updating…" : "Saving…";

    api(url, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("goalName").value.trim(),
        target: raw,
        account: $("goalAccount").value || null,
        deadline: $("goalDeadline").value || null,
      }),
    }).then(function () {
      var label = isEdit ? "Goal updated" : "Goal '" + $("goalName").value.trim() + "' created";
      closeGoal();
      toast(label, "success");
      return loadAll();
    }).catch(function (e) {
      error.textContent = e.message;
      error.hidden = false;
      btn.disabled = false;
      btn.textContent = isEdit ? "Save changes" : "Save goal";
    });
  }

  /**
   * Apply a partial update to a goal, such as archiving or restoring it.
   *
   * @param {number} id The goal's id.
   * @param {Object} patch The fields to change.
   */
  function patchGoal(id, patch) {
    var current = state.goals.filter(function (g) { return g.id === id; })[0];
    if (!current) return;
    api("/api/goals/" + id, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: current.name,
        target: String(current.target),
        account: current.account || null,
        deadline: current.deadline || null,
        is_archived: patch.is_archived === undefined ? current.is_archived : patch.is_archived,
        sort_order: current.sort_order,
      }),
    }).then(function () {
      toast(patch.is_archived ? "Goal archived" : "Goal restored", "success");
      return loadAll();
    }).catch(function (e) {
      toast(e.message, "error");
    });
  }

  /**
   * Delete a goal after confirming.
   *
   * @param {Object} goal The goal row to remove.
   */
  function deleteGoal(goal) {
    if (!window.confirm("Delete goal '" + goal.name + "'? This cannot be undone.")) return;
    api("/api/goals/" + goal.id, { method: "DELETE" })
      .then(function () {
        toast("Goal '" + goal.name + "' deleted", "success");
        return loadAll();
      })
      .catch(function (e) {
        toast(e.message, "error");
      });
  }

  /**
   * Close a dialog when its scrim is pressed, but not when a selection drag
   * inside the dialog happens to end over the scrim.
   *
   * A click event is dispatched on the nearest common ancestor of where the
   * press began and where it ended. Dragging a text selection out of an input
   * and releasing the mouse on the scrim therefore produced a click whose target
   * was the scrim itself, dismissing the dialog mid-edit. Tying the decision to
   * the element the press started on leaves those drags alone while a genuine
   * press on the scrim still closes the dialog.
   *
   * @param {HTMLElement} backdrop The scrim element.
   * @param {function(): void} close Dismisses the dialog.
   */
  function bindScrimClose(backdrop, close) {
    var pressedOnScrim = false;
    backdrop.addEventListener("pointerdown", function (e) {
      pressedOnScrim = e.target === backdrop;
    });
    backdrop.addEventListener("click", function (e) {
      if (pressedOnScrim && e.target === backdrop) close();
      pressedOnScrim = false;
    });
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
      ["filterFrom", "from"], ["filterTo", "to"],
      ["filterMin", "min"], ["filterMax", "max"],
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

  /**
   * Whether keyboard focus is somewhere that consumes typing.
   *
   * @param {EventTarget} target The event target.
   * @returns {boolean} True when a plain key press would be text input.
   */
  function isTypingTarget(target) {
    if (!target || !target.tagName) return false;
    var tag = target.tagName;
    var editable = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
      target.isContentEditable === true;
    return editable && !target.closest("[hidden]");
  }

  /** The container of the dialog that is currently open, if any. */
  function openDialog() {
    return document.querySelector(".modal-backdrop:not([hidden]) .modal");
  }

  /** Elements behind a dialog that should be inert while one is open. */
  function dialogBackground() {
    return [document.querySelector("header.topbar"), $("overview"), $("authGate")].filter(Boolean);
  }

  /**
   * Hide the page behind an open dialog from pointer and assistive technology.
   *
   * @param {boolean} on Whether a dialog is now open.
   */
  function setBackgroundInert(on) {
    dialogBackground().forEach(function (el) {
      if (on) {
        el.setAttribute("inert", "");
        el.setAttribute("aria-hidden", "true");
      } else {
        el.removeAttribute("inert");
        el.removeAttribute("aria-hidden");
      }
    });
  }

  /**
   * Keep Tab focus cycling inside the open dialog.
   *
   * @param {KeyboardEvent} event The Tab keydown event.
   */
  function trapFocus(event) {
    var dialog = openDialog();
    if (!dialog) return;
    var focusables = [
      "a[href]", "button:not([disabled])", "input:not([disabled]):not([type='hidden'])",
      "select:not([disabled])", "textarea:not([disabled])", "[tabindex]:not([tabindex='-1'])",
    ].join(", ");
    var items = Array.prototype.filter.call(
      dialog.querySelectorAll(focusables),
      function (el) { return el.offsetParent !== null || el === document.activeElement; }
    );
    if (!items.length) {
      event.preventDefault();
      return;
    }
    var first = items[0];
    var last = items[items.length - 1];
    var active = document.activeElement;
    if (event.shiftKey) {
      if (active === first || !dialog.contains(active)) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last || !dialog.contains(active)) {
      event.preventDefault();
      first.focus();
    }
  }

  /** Trap Tab inside an open dialog and cycle focus within it. */
  function bindDialogFocus() {
    document.addEventListener("keydown", function (e) {
      if (e.key === "Tab" && openDialog()) trapFocus(e);
    });
  }

  /** Bind the global keyboard shortcuts. */
  function bindShortcuts() {
    document.addEventListener("keydown", function (e) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      var dialogOpen = !$("modalBackdrop").hidden ||
        !$("settingsBackdrop").hidden || !$("goalsBackdrop").hidden;
      if (dialogOpen || isTypingTarget(e.target)) return;

      if (e.key === "/") {
        e.preventDefault();
        $("filterSearch").focus();
        $("filterSearch").select();
      } else if (e.key === "n" || e.key === "N") {
        e.preventDefault();
        openModal();
      }
    });
  }

  /** Bind the time range, series toggle, and section navigation controls. */
  function bindRanges() {
    var segs = document.querySelectorAll(".hero-controls .seg");
    Array.prototype.forEach.call(segs, function (seg) {
      seg.addEventListener("click", function () {
        Array.prototype.forEach.call(segs, function (s) {
          s.classList.remove("is-active");
          s.setAttribute("aria-pressed", "false");
        });
        seg.classList.add("is-active");
        seg.setAttribute("aria-pressed", "true");
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
        if (!hasCharts()) return;
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
    if (started) {
      loadAll();
      return;
    }

    // `started` is set after the bindings so it means "fully initialised". It
    // used to be set first, which meant a throw partway through left the flag on
    // and every later attempt skipped the work that had not run yet: the page
    // stayed permanently inert. The chart construction that used to throw is now
    // contained inside bindInterface, but the ordering keeps the flag honest.
    bindInterface();
    started = true;
    loadAll();
  }

  /**
   * Bind every event listener and construct the charts.
   *
   * Split out from {@link init} so that a chart that cannot be constructed costs
   * only the charts, rather than taking the whole interface down with it.
   */
  function bindInterface() {
    try {
      initCharts();
    } catch (err) {
      // The rest of the dashboard still works without the canvas charts, so
      // report and carry on rather than leaving a blank page behind.
      // initCharts may have populated some charts before failing, so discard
      // them: a half-built chart would break the renderers that follow.
      logError("Charts could not be drawn", err);
      charts = {};
      chartsReady = false;
    }

    auth.onThemeChanged(repaintCharts);
    auth.onCurrencyChanged(repaintCharts);
    bindFilters();
    bindRanges();
    bindShortcuts();
    bindDialogFocus();
    startClock();
    observeReveals();

    $("openModal").addEventListener("click", function () { openModal(); });
    $("closeModal").addEventListener("click", closeModal);
    $("cancelModal").addEventListener("click", closeModal);
    bindScrimClose($("modalBackdrop"), closeModal);
    $("txForm").addEventListener("submit", submitTransaction);
    $("newGoalBtn").addEventListener("click", function () { openGoal(); });
    $("closeGoals").addEventListener("click", closeGoal);
    $("cancelGoals").addEventListener("click", closeGoal);
    bindScrimClose($("goalsBackdrop"), closeGoal);
    $("goalForm").addEventListener("submit", submitGoal);
    $("budgetForm").addEventListener("submit", submitBudget);
    $("categoryForm").addEventListener("submit", createCategory);
    $("accountForm").addEventListener("submit", createAccount);
    $("exportBtn").addEventListener("click", exportBackup);
    $("importBtn").addEventListener("click", function () { $("importFile").click(); });
    $("importFile").addEventListener("change", importBackup);

    $("userChip").addEventListener("click", toggleUserPop);
    $("signOutBtn").addEventListener("click", function () {
      closeUserPop();
      auth.signOut().catch(function (e) { toast(e.message, "error"); });
    });
    $("openSettings").addEventListener("click", openSettings);
    $("closeSettings").addEventListener("click", closeSettings);
    bindScrimClose($("settingsBackdrop"), closeSettings);
    $("savePreferences").addEventListener("click", savePreferences);
    $("passwordForm").addEventListener("submit", function (e) {
      e.preventDefault();
      changePassword();
    });
    $("peopleForm").addEventListener("submit", function (e) {
      e.preventDefault();
      createPerson();
    });

    Array.prototype.forEach.call(document.querySelectorAll(".type-toggle .seg"), function (seg) {
      seg.addEventListener("click", function () { setModalType(seg.dataset.type); });
    });

    document.addEventListener("click", function (e) {
      if (!$("userPop").hidden &&
          !e.target.closest(".user-menu")) {
        closeUserPop();
      }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      if (!$("settingsBackdrop").hidden) closeSettings();
      else if (!$("modalBackdrop").hidden) closeModal();
      else if (!$("goalsBackdrop").hidden) closeGoal();
      else if (!$("userPop").hidden) closeUserPop();
    });
  }

  /**
   * Start the dashboard for a signed-in user.
   *
   * Called by the sign-in gate, so a user who signs out and back in gets the
   * same live dashboard rather than a second copy of its listeners.
   *
   * @param {Object} user The signed-in user record.
   */
  function start(user) {
    state.user = user;
    renderUser(user);
    init();
  }

  window.FinlifyDashboard = { start: start };

  auth.onSessionLost(function () {
    if (started) setStatus("is-error", "signed out");
  });

  auth.boot();
})();
