(function () {
  const API_STORAGE_KEY = "equity_api_base_url";
  const SNAPSHOT_PATH = "./workflow.json";
  const chartColors = {
    sameDay: "#22c55e",
    swing: "#3b82f6",
  };

  const elements = {
    apiBaseLabel: document.getElementById("apiBaseLabel"),
    lastRefreshLabel: document.getElementById("lastRefreshLabel"),
    modeLabel: document.getElementById("modeLabel"),
    statusBanner: document.getElementById("statusBanner"),
    brokerEquity: document.getElementById("brokerEquity"),
    brokerEquityNote: document.getElementById("brokerEquityNote"),
    filledOrders: document.getElementById("filledOrders"),
    filledOrdersNote: document.getElementById("filledOrdersNote"),
    sameDayAvg: document.getElementById("sameDayAvg"),
    sameDayNote: document.getElementById("sameDayNote"),
    swingAvg: document.getElementById("swingAvg"),
    swingNote: document.getElementById("swingNote"),
    sameDayChart: document.getElementById("sameDayChart"),
    swingChart: document.getElementById("swingChart"),
    benchmarkStats: document.getElementById("benchmarkStats"),
    benchmarkMeta: document.getElementById("benchmarkMeta"),
    reportSummary: document.getElementById("reportSummary"),
    reportExamples: document.getElementById("reportExamples"),
    liveBadges: document.getElementById("liveBadges"),
    liveMeta: document.getElementById("liveMeta"),
    openOrders: document.getElementById("openOrders"),
    holdings: document.getElementById("holdings"),
    closedResults: document.getElementById("closedResults"),
    scoreSameDay: document.getElementById("scoreSameDay"),
    scoreSameDayNote: document.getElementById("scoreSameDayNote"),
    scoreSwing: document.getElementById("scoreSwing"),
    scoreSwingNote: document.getElementById("scoreSwingNote"),
    backtestMeta: document.getElementById("backtestMeta"),
    signals: document.getElementById("signals"),
    settingsBtn: document.getElementById("settingsBtn"),
    refreshBtn: document.getElementById("refreshBtn"),
    settingsDialog: document.getElementById("settingsDialog"),
    apiBaseInput: document.getElementById("apiBaseInput"),
    saveApiBtn: document.getElementById("saveApiBtn"),
    clearApiBtn: document.getElementById("clearApiBtn"),
  };

  function formatMoney(value) {
    if (value == null || Number.isNaN(Number(value))) return "n/a";
    return Number(value).toLocaleString(undefined, {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 0,
    });
  }

  function formatPct(value, digits = 2) {
    if (value == null || Number.isNaN(Number(value))) return "n/a";
    return Number(value).toFixed(digits) + "%";
  }

  function formatPositionPct(value, digits = 2) {
    if (value == null || Number.isNaN(Number(value))) return "n/a";
    const numeric = Number(value);
    const normalized = Math.abs(numeric) <= 1 ? numeric * 100 : numeric;
    return formatPct(normalized, digits);
  }

  function toneClass(value) {
    if (value == null || Number.isNaN(Number(value))) return "neutral";
    if (Number(value) > 0) return "positive";
    if (Number(value) < 0) return "negative";
    return "neutral";
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function getConfiguredApiBase() {
    const params = new URLSearchParams(window.location.search);
    const queryValue = params.get("api");
    if (queryValue) return queryValue.replace(/\/+$/, "");
    if (window.EQUITY_API_BASE_URL) return String(window.EQUITY_API_BASE_URL).replace(/\/+$/, "");
    const saved = window.localStorage.getItem(API_STORAGE_KEY);
    if (saved) return saved.replace(/\/+$/, "");
    return "";
  }

  function setBanner(kind, message) {
    elements.statusBanner.className = "status-banner " + kind;
    elements.statusBanner.innerHTML = message;
  }

  function setList(target, html, emptyText) {
    if (!html) {
      target.className = "list-wrap empty-state";
      target.textContent = emptyText;
      return;
    }
    target.className = "list-wrap";
    target.innerHTML = html;
  }

  function setMetaRow(target, items) {
    target.innerHTML = items
      .filter(Boolean)
      .map((item) => `<span>${esc(item)}</span>`)
      .join("");
  }

  function renderBadges(target, items) {
    target.innerHTML = items
      .map((item) => `<span class="badge ${item.kind}">${esc(item.label)}</span>`)
      .join("");
  }

  function formatDateTimeET(value) {
    if (!value) return "n/a";
    const normalized = /z$/i.test(value) || /[+-]\d\d:\d\d$/.test(value)
      ? value
      : value + "Z";
    const date = new Date(normalized);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    }).format(date) + " ET";
  }

  function describeSignalSide(side) {
    if (side === "buy") {
      return {
        title: "Enter Long",
        badge: "Long entry example",
        summaryTitle: "Long Entry Outcomes",
        cardTitle: "LONG ENTRY",
        detail: "Shows how the stock moved after the buy signal through the 3:55 PM ET close."
      };
    }
    if (side === "sell") {
      return {
        title: "Exit Long",
        badge: "Long exit example",
        summaryTitle: "Long Exit Outcomes",
        cardTitle: "LONG EXIT",
        detail: "Not a short. Shows whether the stock fell or rose after the exit signal by the 3:55 PM ET close."
      };
    }
    return {
      title: "Signal",
      badge: "Signal example",
      summaryTitle: "Signal Outcomes",
      cardTitle: String(side || "SIGNAL").toUpperCase(),
      detail: ""
    };
  }

  function formatSignedPct(value, digits = 2) {
    if (value == null || Number.isNaN(Number(value))) return "n/a";
    const numeric = Number(value);
    const prefix = numeric > 0 ? "+" : "";
    return prefix + numeric.toFixed(digits) + "%";
  }

  function describeExampleOutcome(item) {
    const side = String(item.signal_side || "").toLowerCase();
    const entry = Number(item.entry_price);
    const exit = Number(item.exit_price);
    if (!Number.isFinite(entry) || !Number.isFinite(exit) || !entry) {
      return item.labels.detail;
    }
    const movePct = ((exit / entry) - 1) * 100;
    if (side === "sell") {
      if (movePct < 0) {
        return `After the exit signal, price fell ${formatPct(Math.abs(movePct))} into the close, so exiting early helped.`;
      }
      if (movePct > 0) {
        return `After the exit signal, price rose ${formatPct(movePct)} into the close, so exiting early hurt.`;
      }
      return "After the exit signal, price finished flat into the close.";
    }
    if (movePct < 0) {
      return `After the buy signal, price fell ${formatPct(Math.abs(movePct))} into the close, so the entry lost money.`;
    }
    if (movePct > 0) {
      return `After the buy signal, price rose ${formatPct(movePct)} into the close, so the entry made money.`;
    }
    return "After the buy signal, price finished flat into the close.";
  }

  function renderReport(targetSummary, targetExamples, report) {
    const buy = (report.sides || {}).buy || {};
    const sell = (report.sides || {}).sell || {};
    const benchmark = report.benchmark || {};
    const buyLabels = describeSignalSide("buy");
    const sellLabels = describeSignalSide("sell");
    const summaryCards = [
      {
        title: buyLabels.summaryTitle,
        stat: formatPct(buy.avg_net_return_pct),
        tone: toneClass(buy.avg_net_return_pct),
        copy: `${buy.count || 0} rows · ${formatPct(buy.win_rate_pct, 1)} win rate · median ${formatPct(buy.median_net_return_pct)} · ${buyLabels.detail}`
      },
      {
        title: sellLabels.summaryTitle,
        stat: formatPct(sell.avg_net_return_pct),
        tone: toneClass(sell.avg_net_return_pct),
        copy: `${sell.count || 0} rows · ${formatPct(sell.win_rate_pct, 1)} win rate · median ${formatPct(sell.median_net_return_pct)} · ${sellLabels.detail}`
      },
      {
        title: "Benchmark Read",
        stat: benchmark.available ? formatPct(benchmark.alpha_per_trade_pct) : "n/a",
        tone: toneClass(benchmark.alpha_per_trade_pct),
        copy: benchmark.available
          ? `SPY ${formatPct(benchmark.return_pct)} over ${benchmark.start_date} to ${benchmark.end_date}`
          : (benchmark.message || "Benchmark unavailable.")
      }
    ];

    targetSummary.className = "report-grid";
    targetSummary.innerHTML = summaryCards.map((item) => `
      <div class="report-card">
        <div class="report-card-head">
          <div class="report-card-title">${esc(item.title)}</div>
          <div class="report-card-stat ${item.tone}">${esc(item.stat)}</div>
        </div>
        <div class="report-card-copy">${esc(item.copy)}</div>
      </div>
    `).join("");

    const examples = [
      ...(buy.example_rows || []).slice(0, 3).map((item) => ({ ...item, labels: buyLabels })),
      ...(sell.example_rows || []).slice(0, 3).map((item) => ({ ...item, labels: sellLabels }))
    ];

    if (!examples.length) {
      targetExamples.className = "report-examples empty-state";
      targetExamples.textContent = "No example trades yet.";
      return;
    }

    targetExamples.className = "report-examples";
    targetExamples.innerHTML = examples.map((item) => `
      <article class="trade-card">
        <div class="trade-card-head">
          <div class="trade-card-title">${esc(item.ticker || "?")} ${esc(item.labels.cardTitle)}</div>
          <div class="trade-card-return ${toneClass(item.net_return_pct)}">${esc(formatSignedPct(item.net_return_pct))}</div>
        </div>
        <div class="trade-card-meta">
          <div><span class="trade-card-badge">${esc(item.labels.badge)}</span></div>
          <div>Session: ${esc(item.session_date || "n/a")}</div>
          <div>Entry: ${esc(formatDateTimeET(item.entry_timestamp))} at ${esc(formatMoney(item.entry_price))}</div>
          <div>Exit: ${esc(formatDateTimeET(item.exit_timestamp))} at ${esc(formatMoney(item.exit_price))}</div>
          <div>Outcome: ${esc(item.win_loss || "n/a")}${item.flag ? ` · ${esc(item.flag)}` : ""}</div>
          <div>${esc(describeExampleOutcome(item))}</div>
        </div>
      </article>
    `).join("");
  }

  function renderChart(target, points, color) {
    if (!points || !points.length) {
      target.className = "chart-wrap empty-state";
      target.textContent = "No curve points yet.";
      return;
    }
    const values = points.map((point) => Number(point.equity || 0));
    const min = Math.min.apply(null, values);
    const max = Math.max.apply(null, values);
    const width = 720;
    const height = 164;
    const span = Math.max(max - min, 1);
    const step = points.length === 1 ? 0 : width / (points.length - 1);
    const polyline = points.map((point, index) => {
      const x = step * index;
      const y = height - (((Number(point.equity || 0) - min) / span) * (height - 24)) - 12;
      return `${x},${y}`;
    }).join(" ");
    const startLabel = points[0].label;
    const endLabel = points[points.length - 1].label;
    const first = values[0];
    const last = values[values.length - 1];
    const delta = first ? ((last / first) - 1) * 100 : null;

    target.className = "chart-wrap";
    target.innerHTML = [
      `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">`,
      `<polyline points="${polyline}" fill="none" stroke="${color}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"></polyline>`,
      `</svg>`,
      `<div class="meta-row"><span>${esc(startLabel)} to ${esc(endLabel)}</span><span class="${toneClass(delta)}">${formatPct(delta)}</span></div>`
    ].join("");
  }

  function renderListItem(leftTitle, leftMeta, leftNote, rightTitle, rightMeta, rightNote, rightClass) {
    return [
      `<div class="list-item">`,
      `<div class="list-item-left">`,
      `<div class="list-title">${esc(leftTitle)}</div>`,
      leftMeta ? `<div class="list-meta">${esc(leftMeta)}</div>` : "",
      leftNote ? `<div class="list-meta">${esc(leftNote)}</div>` : "",
      `</div>`,
      `<div class="list-item-right">`,
      `<div class="list-title ${rightClass || ""}">${esc(rightTitle)}</div>`,
      rightMeta ? `<div class="list-meta">${esc(rightMeta)}</div>` : "",
      rightNote ? `<div class="list-meta">${esc(rightNote)}</div>` : "",
      `</div>`,
      `</div>`
    ].join("");
  }

  function renderWorkflow(data, sourceLabel, sourceKind) {
    const perf = data.performance || {};
    const live = perf.live || {};
    const broker = perf.broker || {};
    const curves = perf.curves || {};
    const backtests = data.backtests || {};
    const sameDay = backtests.same_day || {};
    const sameDayReport = sameDay.report || {};
    const sameDaySides = sameDayReport.sides || {};
    const sameDayBuy = sameDaySides.buy || {};
    const benchmark = sameDayReport.benchmark || {};
    const swing5 = (backtests.swing || {})["5"] || {};
    const mode = data.mode || {};
    const signals = (data.signal_generation || {}).recent_signals || [];
    const holdings = broker.positions || [];
    const openOrders = broker.open_orders || [];
    const closed = live.closed_results_preview || [];

    elements.apiBaseLabel.textContent = sourceLabel || "Bundled snapshot";
    elements.lastRefreshLabel.textContent = data.generated_at
      ? formatDateTimeET(data.generated_at)
      : formatDateTimeET(new Date().toISOString());
    elements.modeLabel.textContent = mode.holding_style_label
      ? mode.holding_style_label
      : "Unknown";

    elements.brokerEquity.textContent = formatMoney(live.account_equity);
    elements.brokerEquityNote.textContent = broker.available
      ? `Cash ${formatMoney(live.cash)} · buying power ${formatMoney(live.buying_power)}`
      : (broker.message || "Broker feed unavailable.");

    elements.filledOrders.textContent = String(live.filled_order_count || 0);
    elements.filledOrdersNote.textContent = `${mode.broker_provider || "broker"} · ${mode.execution_enabled ? "execution enabled" : "execution disabled"}`;

    const sameDayHeadline = benchmark.signal_avg_buy_net_return_pct != null
      ? benchmark.signal_avg_buy_net_return_pct
      : sameDay.avg_gross_return_pct;
    const sameDayHeadlineNote = benchmark.signal_avg_buy_net_return_pct != null
      ? `${formatPct(benchmark.alpha_per_trade_pct)} alpha vs SPY · ${sameDayBuy.count || 0} buy rows`
      : `${formatPct(sameDay.win_rate_pct, 1)} win rate · ${sameDay.ok_count || 0} resolved rows`;

    elements.sameDayAvg.textContent = formatPct(sameDayHeadline);
    elements.sameDayAvg.className = "stat-value " + toneClass(sameDayHeadline);
    elements.sameDayNote.textContent = sameDayHeadlineNote;

    elements.swingAvg.textContent = formatPct(swing5.avg_return_pct);
    elements.swingAvg.className = "stat-value " + toneClass(swing5.avg_return_pct);
    elements.swingNote.textContent = `${formatPct(swing5.win_rate_pct, 1)} win rate · ${swing5.count || 0} scored rows`;

    if (sourceKind === "snapshot") {
      setBanner(
        "warn",
        `Showing bundled workflow snapshot${data.generated_at ? ` from <code>${esc(data.generated_at)}</code>` : ""}. Use API Settings to connect a live backend anytime.`
      );
    } else if (broker.available) {
      setBanner("good", `Connected successfully. This Pages frontend is reading live workflow data from <code>${esc(sourceLabel)}</code>.`);
    } else {
      setBanner("warn", `Workflow loaded, but broker access is unavailable. Backend message: <code>${esc(broker.message || "unavailable")}</code>.`);
    }

    renderChart(elements.sameDayChart, curves.same_day || [], chartColors.sameDay);
    renderChart(elements.swingChart, curves.swing_5d || [], chartColors.swing);

    renderBadges(elements.liveBadges, [
      { kind: broker.available ? "good" : "error", label: `broker ${broker.available ? "connected" : "unavailable"}` },
      { kind: mode.execution_enabled ? "good" : "error", label: `execution ${mode.execution_enabled ? "enabled" : "disabled"}` },
      { kind: mode.require_approval ? "warn" : "good", label: `approval ${mode.require_approval ? "required" : "auto"}` },
      { kind: holdings.length ? "good" : "warn", label: `${holdings.length} open positions` },
      { kind: openOrders.length ? "warn" : "good", label: `${openOrders.length} open broker orders` }
    ]);

    setMetaRow(elements.liveMeta, [
      `positions mv ${formatMoney(live.position_market_value)}`,
      `unrealized ${formatMoney(live.unrealized_pl)}`,
      `same-day linked avg ${formatPct(live.estimated_same_day_avg_return_pct)}`,
      `5d linked avg ${formatPct(live.estimated_swing_5d_avg_return_pct)}`
    ]);

    setList(
      elements.openOrders,
      openOrders.map((item) => renderListItem(
        `${item.symbol || "?"} ${(item.side || "").toUpperCase()}`,
        `${item.order_type || "order"} · qty ${item.qty || "n/a"}`,
        "",
        item.status || "open",
        item.submitted_at || "",
        "",
        "neutral"
      )).join(""),
      broker.available ? "No open broker orders." : "Open broker orders are unavailable from the current broker session."
    );

    setList(
      elements.holdings,
      holdings.map((item) => renderListItem(
        item.symbol || "?",
        `${item.qty || 0} sh at ${formatMoney(item.avg_entry_price)}`,
        `Market value ${formatMoney(item.market_value)} · current ${formatMoney(item.current_price)}`,
        formatMoney(item.unrealized_pl),
        formatPositionPct(item.unrealized_plpc),
        "",
        toneClass(item.unrealized_pl)
      )).join(""),
      broker.available ? "No open holdings are currently reported by the broker." : "Holdings are unavailable until the API can reach the broker."
    );

    setList(
      elements.closedResults,
      closed.map((item) => renderListItem(
        `${item.ticker || "?"} ${(item.side || "").toUpperCase()}`,
        `${formatMoney(item.filled_avg_price)} fill · ${item.qty != null ? `${item.qty} sh` : formatMoney(item.notional)}`,
        item.filled_at || "",
        formatPct(item.estimated_same_day_return_pct),
        "same-day estimate",
        `${formatPct(item.estimated_swing_5d_return_pct)} on 5d lens`,
        toneClass(item.estimated_same_day_return_pct)
      )).join(""),
      "No filled orders are linked into the dashboard yet."
    );

    elements.scoreSameDay.textContent = formatPct(sameDayHeadline);
    elements.scoreSameDay.className = "score-value " + toneClass(sameDayHeadline);
    elements.scoreSameDayNote.textContent = benchmark.signal_avg_buy_net_return_pct != null
      ? `buy net avg · alpha ${formatPct(benchmark.alpha_per_trade_pct)} · latest session ${sameDay.latest_session_date || "n/a"}`
      : `${formatPct(sameDay.win_rate_pct, 1)} win rate · latest session ${sameDay.latest_session_date || "n/a"}`;

    elements.scoreSwing.textContent = formatPct(swing5.avg_return_pct);
    elements.scoreSwing.className = "score-value " + toneClass(swing5.avg_return_pct);
    elements.scoreSwingNote.textContent = `${formatPct(swing5.win_rate_pct, 1)} win rate · latest ${swing5.latest_computed_at || "n/a"}`;

    setMetaRow(elements.backtestMeta, [
      `configured lens ${mode.primary_backtest === "same_day" ? "same-day" : "swing"}`,
      `holding style ${mode.holding_style_label || "n/a"}`,
      `${(data.signal_generation || {}).directional_signal_count || 0} directional signals`,
      `gross avg ${formatPct(sameDay.avg_gross_return_pct)}`
    ]);

    if (benchmark.available) {
      elements.benchmarkStats.className = "benchmark-grid";
      elements.benchmarkStats.innerHTML = [
        {
          label: "SPY Buy/Hold",
          numericValue: benchmark.return_pct,
          value: formatPct(benchmark.return_pct),
          note: `${formatMoney(benchmark.start_price)} to ${formatMoney(benchmark.end_price)}`
        },
        {
          label: "Signal Avg (Buy)",
          numericValue: benchmark.signal_avg_buy_net_return_pct,
          value: formatPct(benchmark.signal_avg_buy_net_return_pct),
          note: `${sameDayBuy.count || 0} buy rows · median ${formatPct(sameDayBuy.median_net_return_pct)}`
        },
        {
          label: "Alpha / Trade",
          numericValue: benchmark.alpha_per_trade_pct,
          value: formatPct(benchmark.alpha_per_trade_pct),
          note: `${formatPct(sameDayBuy.win_rate_pct, 1)} win rate`
        }
      ].map((item) => `
        <div class="benchmark-card">
          <div class="benchmark-label">${esc(item.label)}</div>
          <div class="benchmark-value ${toneClass(item.numericValue)}">${esc(item.value)}</div>
          <div class="benchmark-note">${esc(item.note)}</div>
        </div>
      `).join("");
      setMetaRow(elements.benchmarkMeta, [
        `period ${benchmark.start_date || "n/a"} to ${benchmark.end_date || "n/a"}`,
        `${benchmark.trading_days || 0} trading days`,
        `${sameDayReport.total_count || 0} total rows`,
        `${sameDayReport.ok_count || 0} ok rows`
      ]);
    } else {
      elements.benchmarkStats.className = "benchmark-grid empty-state";
      elements.benchmarkStats.textContent = benchmark.message || "No benchmark data yet.";
      setMetaRow(elements.benchmarkMeta, [
        `${sameDayReport.total_count || 0} total rows`,
        `${sameDayReport.ok_count || 0} ok rows`
      ]);
    }

    renderReport(elements.reportSummary, elements.reportExamples, {
      ...sameDayReport
    });

    setList(
      elements.signals,
      signals.map((item) => renderListItem(
        `${item.ticker || "?"} ${(item.signal_side || "").toUpperCase()}`,
        `${item.event_type || "signal"} · strength ${item.signal_strength != null ? Number(item.signal_strength).toFixed(2) : "n/a"}`,
        item.title || "No title recorded.",
        item.generated_at ? formatDateTimeET(item.generated_at) : (item.status || "unknown"),
        "",
        "",
        "neutral"
      )).join(""),
      "No recent signals are available."
    );
  }

  async function fetchWorkflowFromApi(apiBase) {
    const response = await fetch(apiBase + "/api/trading/workflow", {
      method: "GET",
      headers: { "Accept": "application/json" }
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return response.json();
  }

  async function fetchBundledSnapshot() {
    const response = await fetch(SNAPSHOT_PATH, {
      method: "GET",
      headers: { "Accept": "application/json" }
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return response.json();
  }

  async function loadWorkflow() {
    const apiBase = getConfiguredApiBase();
    elements.refreshBtn.disabled = true;
    setBanner("info", apiBase ? "Loading live workflow data..." : "Loading bundled workflow snapshot...");
    try {
      if (apiBase) {
        const data = await fetchWorkflowFromApi(apiBase);
        renderWorkflow(data, apiBase, "api");
      } else {
        const data = await fetchBundledSnapshot();
        renderWorkflow(data, "Bundled snapshot", "snapshot");
      }
    } catch (error) {
      if (apiBase) {
        try {
          const data = await fetchBundledSnapshot();
          renderWorkflow(data, "Bundled snapshot", "snapshot");
          setBanner(
            "warn",
            `Could not load <code>${esc(apiBase + "/api/trading/workflow")}</code>. Showing bundled snapshot instead. ${esc(error.message)}`
          );
        } catch (snapshotError) {
          setBanner(
            "error",
            `Could not load the live API or bundled snapshot. Live error: ${esc(error.message)}. Snapshot error: ${esc(snapshotError.message)}`
          );
        }
      } else {
        setBanner("error", `Could not load bundled snapshot <code>${esc(SNAPSHOT_PATH)}</code>. ${esc(error.message)}`);
      }
    } finally {
      elements.refreshBtn.disabled = false;
    }
  }

  function openSettings() {
    elements.apiBaseInput.value = getConfiguredApiBase();
    elements.settingsDialog.showModal();
  }

  function saveSettings() {
    const value = elements.apiBaseInput.value.trim().replace(/\/+$/, "");
    if (value) {
      window.localStorage.setItem(API_STORAGE_KEY, value);
    } else {
      window.localStorage.removeItem(API_STORAGE_KEY);
    }
    elements.settingsDialog.close();
    loadWorkflow();
  }

  function clearSettings() {
    window.localStorage.removeItem(API_STORAGE_KEY);
    elements.apiBaseInput.value = "";
    loadWorkflow();
  }

  elements.settingsBtn.addEventListener("click", openSettings);
  elements.refreshBtn.addEventListener("click", loadWorkflow);
  elements.saveApiBtn.addEventListener("click", saveSettings);
  elements.clearApiBtn.addEventListener("click", clearSettings);

  loadWorkflow();
})();
