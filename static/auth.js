/**
 * @file Sign-in gate and per-user preferences for Finlify.
 *
 * Everything on this machine belongs to one person at a time, so the dashboard
 * is hidden behind a sign-in gate. This file owns that gate, the shared request
 * helper that funnels 401 responses back to the gate, and the user's own theme
 * and currency, which are mirrored into local storage so the page paints in the
 * right colours before the first request returns.
 */

(function () {
  "use strict";

  var FX = window.FX;

  var THEME_KEY = "finlify.theme";
  var CURRENCY_KEY = "finlify.currency";
  var NAME_KEY = "finlify.name";

  var THEMES = ["dark", "light", "auto"];

  var VIEWS = {
    setup: { form: "authSetup", error: "setupError", submit: "setupSubmit", label: "Claim this ledger" },
    signin: { form: "authSignIn", error: "signinError", submit: "signinSubmit", label: "Sign in" },
    register: { form: "authRegister", error: "registerError", submit: "registerSubmit", label: "Create account" },
  };

  var state = {
    user: null,
    currencies: [],
    needsSetup: false,
    view: null,
  };

  var sessionLost = null;
  var $ = function (id) { return document.getElementById(id); };

  /**
   * Read a cached preference, tolerating a browser that blocks storage.
   *
   * @param {string} key The local storage key.
   * @returns {string|null} The stored value, or null when unavailable.
   */
  function read(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  /**
   * Cache a preference for the next page load.
   *
   * @param {string} key The local storage key.
   * @param {string} value The value to store.
   */
  function write(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (e) {
      return;
    }
  }

  /**
   * Apply an appearance theme to the document.
   *
   * @param {string} theme One of "dark", "light", or "auto".
   */
  function applyTheme(theme) {
    if (THEMES.indexOf(theme) === -1) theme = "dark";
    document.documentElement.dataset.theme = theme;
    write(THEME_KEY, theme);
    if (state.user) state.user.theme = theme;
  }

  /**
   * Apply a currency to every formatter, including the chart engine.
   *
   * @param {string} code An ISO 4217 currency code.
   */
  function applyCurrency(code) {
    if (!FX.setCurrency(code)) return;
    write(CURRENCY_KEY, FX.currencyCode());
    if (state.user) state.user.currency = FX.currencyCode();
  }

  /**
   * Apply a whole preference payload from the server.
   *
   * @param {Object} payload An auth or settings response body.
   */
  function applyPreferences(payload) {
    if (payload && payload.user) state.user = payload.user;
    if (payload && payload.currencies) state.currencies = payload.currencies;
    if (state.user) {
      applyTheme(state.user.theme);
      applyCurrency(state.user.currency);
      write(NAME_KEY, state.user.display_name || state.user.username);
    }
  }

  /**
   * Perform a JSON request, turning any failure into an Error carrying status.
   *
   * A 401 anywhere in the app means the session ended, so the gate is shown
   * again rather than leaving a half-loaded dashboard on screen.
   *
   * @param {string} path Request path, including any query string.
   * @param {RequestInit} [options] Fetch options such as method and body.
   * @returns {Promise<Object>} The parsed response body.
   */
  function request(path, options) {
    return fetch(path, options).then(function (res) {
      var isJson = (res.headers.get("content-type") || "").indexOf("application/json") !== -1;
      return (isJson ? res.json() : res.text()).then(function (body) {
        if (res.status === 401) {
          state.user = null;
          showGate("signin", { message: "Your session ended. Sign in again to continue." });
        }
        if (!res.ok) {
          var msg = "Request failed (" + res.status + ")";
          if (body && typeof body === "object" && body.detail) {
            msg = typeof body.detail === "string"
              ? body.detail
              : body.detail.map(function (d) { return d.msg; }).join(" ");
          }
          var error = new Error(msg);
          error.status = res.status;
          throw error;
        }
        return body;
      });
    });
  }

  /**
   * Show a message inside one of the gate forms.
   *
   * @param {string} view The view name.
   * @param {string} [message] The message to display.
   */
  function setError(view, message) {
    var el = $(VIEWS[view].error);
    if (!el) return;
    el.textContent = message || "";
    el.hidden = !message;
  }

  /**
   * Disable or enable the submit button of a view while a request is running.
   *
   * @param {string} view The view name.
   * @param {boolean} busy True to disable, false to restore.
   */
  function setBusy(view, busy) {
    var btn = $(VIEWS[view].submit);
    if (!btn) return;
    btn.disabled = busy;
    if (!busy) btn.textContent = VIEWS[view].label;
  }

  /**
   * Switch the gate to one of its views.
   *
   * @param {string} view One of "setup", "signin", or "register".
   * @param {Object} [options] Optional message and a form to focus.
   */
  function showView(view, options) {
    var opts = options || {};
    Object.keys(VIEWS).forEach(function (name) {
      $(VIEWS[name].form).hidden = name !== view;
      setError(name, null);
    });
    state.view = view;
    $("authGate").hidden = false;
    $("overview").hidden = true;
    if (opts.message) setError(view, opts.message);
    if (view === "signin") $("signinUsername").focus();
    else if (view === "setup") $("setupUsername").focus();
    else $("registerUsername").focus();
  }

  /**
   * Show the gate, stopping the dashboard behind it.
   *
   * @param {string} view The view to show.
   * @param {Object} [options] Optional message for the form.
   */
  function showGate(view, options) {
    showView(view, options);
    if (typeof sessionLost === "function") sessionLost();
  }

  /**
   * Fill the currency picker with the codes the server offers.
   *
   * @param {string} selectId The element to populate.
   * @param {string} selected The code to mark as chosen.
   */
  function fillCurrencies(selectId, selected) {
    var select = $(selectId);
    if (!select) return;
    var current = selected || read(CURRENCY_KEY) || (state.user && state.user.currency) || "EUR";
    select.innerHTML = "";
    state.currencies.forEach(function (item) {
      var option = document.createElement("option");
      option.value = item.code;
      option.textContent = item.code + " · " + item.label + " (" + item.symbol + ")";
      if (item.code === current) option.selected = true;
      select.appendChild(option);
    });
  }

  /**
   * Mark the active appearance choice in every appearance switcher.
   *
   * @param {string} theme The chosen theme.
   */
  function markTheme(theme) {
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-theme-choice]"),
      function (seg) {
        seg.classList.toggle("is-active", seg.dataset.themeChoice === theme);
      }
    );
  }

  /**
   * Hide the gate and hand control to the dashboard.
   *
   * @param {Object} payload The auth or settings response body.
   */
  function enterApp(payload) {
    applyPreferences(payload);
    $("authGate").hidden = true;
    $("overview").hidden = false;
    fillCurrencies("setupCurrency", state.user.currency);
    markTheme(state.user.theme);
    window.FinlifyDashboard.start(state.user);
  }

  /**
   * Read the signed-in session, or the reason there is not one.
   *
   * @returns {Promise<void>} Resolves once the gate or dashboard is shown.
   */
  function boot() {
    return request("/api/auth/state")
      .then(function (payload) {
        state.needsSetup = !!payload.needs_setup;
        state.currencies = payload.currencies || [];
        applyTheme(read(THEME_KEY) || (payload.user && payload.user.theme) || "dark");
        applyCurrency(read(CURRENCY_KEY) || (payload.user && payload.user.currency) || "EUR");
        fillCurrencies("setupCurrency", read(CURRENCY_KEY) || "EUR");
        markTheme(read(THEME_KEY) || "dark");
        if (payload.authenticated) enterApp(payload);
        else if (state.needsSetup) showView("setup");
        else showView("signin");
      })
      .catch(function () {
        showView("signin", { message: "Cannot reach the Finlify server on this machine." });
      });
  }

  /**
   * Sign the current user out and return to the gate.
   *
   * @returns {Promise<void>} Resolves once the gate is shown again.
   */
  function signOut() {
    return request("/api/auth/logout", { method: "POST" }).then(function () {
      state.user = null;
      showView("signin");
    });
  }

  /**
   * Save preferences and report the saved result.
   *
   * @param {Object} changes Field names to send.
   * @returns {Promise<Object>} The settings response body.
   */
  function saveSettings(changes) {
    return request("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    }).then(function (payload) {
      applyPreferences(payload);
      return payload;
    });
  }

  /**
   * Submit a gate form, showing any failure inside the form itself.
   *
   * @param {string} view The view name.
   * @param {string} path The endpoint to post to.
   * @param {Object} body The request body.
   */
  function submit(view, path, body) {
    setError(view, null);
    setBusy(view, true);
    request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (payload) {
        setBusy(view, false);
        enterApp(payload);
      })
      .catch(function (err) {
        setBusy(view, false);
        setError(view, err.message);
      });
  }

  function bind() {
    $("authSetup").addEventListener("submit", function (e) {
      e.preventDefault();
      var theme = read(THEME_KEY) || "dark";
      Array.prototype.forEach.call(
        $("authSetup").querySelectorAll("[data-theme-choice]"),
        function (seg) {
          if (seg.classList.contains("is-active")) theme = seg.dataset.themeChoice;
        }
      );
      var body = {
        username: $("setupUsername").value.trim(),
        password: $("setupPassword").value,
        theme: theme,
      };
      var currency = $("setupCurrency").value;
      if (currency) body.currency = currency;
      submit("setup", "/api/auth/setup", body);
    });

    $("authSignIn").addEventListener("submit", function (e) {
      e.preventDefault();
      submit("signin", "/api/auth/login", {
        username: $("signinUsername").value.trim(),
        password: $("signinPassword").value,
      });
    });

    $("authRegister").addEventListener("submit", function (e) {
      e.preventDefault();
      submit("register", "/api/auth/register", {
        username: $("registerUsername").value.trim(),
        password: $("registerPassword").value,
        display_name: $("registerName").value.trim() || null,
      });
    });

    $("toRegister").addEventListener("click", function () { showView("register"); });
    $("toSignIn").addEventListener("click", function () { showView("signin"); });

    Array.prototype.forEach.call(
      document.querySelectorAll("[data-theme-choice]"),
      function (seg) {
        seg.addEventListener("click", function () {
          applyTheme(seg.dataset.themeChoice);
          markTheme(seg.dataset.themeChoice);
        });
      }
    );

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && $("authGate").hidden === false && state.user) showView("signin");
    });
  }

  window.FinlifyAuth = {
    boot: boot,
    request: request,
    signOut: signOut,
    saveSettings: saveSettings,
    applyPreferences: applyPreferences,
    applyTheme: applyTheme,
    applyCurrency: applyCurrency,
    showGate: showGate,
    fillCurrencies: fillCurrencies,
    markTheme: markTheme,
    cachedName: function () { return read(NAME_KEY); },
    onSessionLost: function (fn) { sessionLost = fn; },
    user: function () { return state.user; },
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
