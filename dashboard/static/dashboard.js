/* ── Dashboard WebSocket client + Chart.js ────────────────────────────────── */

const WS_URL = `ws://${location.host}/ws`;
const RECONNECT_DELAY_MS = 3000;

// ── Strategy colour palette ───────────────────────────────────────────────────

const STRATEGY_COLORS = {
  value_betting:       '#58a6ff',   // blue
  weather_betting:     '#3fb950',   // green
  sum_to_one_arb:      '#d29922',   // yellow
  calibration_betting: '#bc8cff',   // purple
  market_maker:        '#f0883e',   // orange
};

function strategyColor(name) {
  return STRATEGY_COLORS[name] || '#8b949e';
}

// ── Equity curve (multi-strategy) ────────────────────────────────────────────

const MAX_EQUITY_POINTS = 200;
// Shared x-axis labels (time ticks — updated on each snapshot)
const equityLabels = [];
// Per-strategy data arrays: { [strategy]: number[] }
const equityDatasets = {};

let ws    = null;
let chart = null;

function initChart() {
  const ctx = document.getElementById('equity-chart').getContext('2d');
  chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: equityLabels,
      datasets: [],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: true,
      plugins: {
        legend: { display: true, labels: { color: '#c9d1d9', font: { size: 11 } } },
      },
      scales: {
        x: {
          ticks: { color: '#8b949e', maxTicksLimit: 8, font: { size: 10 } },
          grid:  { color: 'rgba(255,255,255,0.04)' },
        },
        y: {
          ticks: {
            color: '#8b949e',
            font: { size: 10 },
            callback: v => '$' + v.toLocaleString(),
          },
          grid: { color: 'rgba(255,255,255,0.04)' },
        },
      },
    },
  });
}

function _getOrCreateDataset(strategy) {
  const existing = chart.data.datasets.find(d => d.label === strategy);
  if (existing) return existing;

  const color = strategyColor(strategy);
  const dataset = {
    label: strategy,
    data: [],
    borderColor: color,
    backgroundColor: color.replace(')', ', 0.07)').replace('rgb', 'rgba'),
    borderWidth: 1.5,
    pointRadius: 0,
    fill: false,
    tension: 0.3,
  };
  chart.data.datasets.push(dataset);
  equityDatasets[strategy] = dataset.data;
  return dataset;
}

function updateEquityChart(strategy, totalValue, timestamp) {
  if (!chart) return;
  const label = new Date(timestamp).toLocaleTimeString();

  // Add label if it's new (shared x-axis)
  if (equityLabels.length === 0 || equityLabels[equityLabels.length - 1] !== label) {
    if (equityLabels.length >= MAX_EQUITY_POINTS) equityLabels.shift();
    equityLabels.push(label);

    // Extend all existing datasets with null to keep lengths aligned
    for (const ds of chart.data.datasets) {
      if (ds.label !== strategy) {
        if (ds.data.length >= MAX_EQUITY_POINTS) ds.data.shift();
        ds.data.push(null);
      }
    }
  }

  const ds = _getOrCreateDataset(strategy);
  if (ds.data.length >= MAX_EQUITY_POINTS) ds.data.shift();
  ds.data.push(totalValue);

  chart.update('none');
}

// ── Strategy cards ────────────────────────────────────────────────────────────

function renderStrategyCards(strategies) {
  const grid = document.getElementById('strategies-grid');
  if (!grid) return;

  strategies.forEach(s => {
    const id = 'strategy-card-' + s.strategy.replace(/[^a-z0-9]/g, '_');
    let card = document.getElementById(id);

    const color = strategyColor(s.strategy);
    const pnl = s.realized_pnl ?? 0;
    const pnlSign = pnl >= 0 ? '+$' : '-$';
    const pnlClass = pnl >= 0 ? 'positive' : 'negative';
    const winRateTxt = s.win_rate != null ? (s.win_rate * 100).toFixed(1) + '%' : '—';

    if (!card) {
      card = document.createElement('div');
      card.className = 'strategy-card';
      card.id = id;
      grid.appendChild(card);
    }

    const ticks        = s.ticks != null ? Number(s.ticks).toLocaleString('en-US') : '—';
    const ticksPerMin  = s.ticks_per_min != null ? `~${s.ticks_per_min}/min` : '—';
    const lastUpdate   = s.last_update ? new Date(s.last_update).toLocaleTimeString() : '—';

    // Staleness indicator: flag if last_update is > 3 min ago
    let staleClass = '';
    if (s.last_update) {
      const ageMin = (Date.now() - new Date(s.last_update).getTime()) / 60000;
      if (ageMin > 3) staleClass = 'stale';
    }

    card.innerHTML = `
      <div class="strategy-card-header">
        <span class="strategy-name" style="color:${color}">${s.strategy}</span>
      </div>
      <div class="strategy-metrics">
        <div class="strategy-metric">
          <div class="sm-label">Value</div>
          <div class="sm-value">$${fmt(s.total_value_usd)}</div>
        </div>
        <div class="strategy-metric">
          <div class="sm-label">Cash</div>
          <div class="sm-value">$${fmt(s.cash_usd)}</div>
        </div>
        <div class="strategy-metric">
          <div class="sm-label">PnL</div>
          <div class="sm-value ${pnlClass}">${pnlSign}${fmt(Math.abs(pnl))}</div>
        </div>
        <div class="strategy-metric">
          <div class="sm-label">Win Rate</div>
          <div class="sm-value">${winRateTxt}</div>
        </div>
        <div class="strategy-metric">
          <div class="sm-label">Trades</div>
          <div class="sm-value">${s.total_trades ?? '—'}</div>
        </div>
        <div class="strategy-metric">
          <div class="sm-label">Ticks</div>
          <div class="sm-value">${ticks}</div>
        </div>
        <div class="strategy-metric">
          <div class="sm-label">Tick Rate</div>
          <div class="sm-value">${ticksPerMin}</div>
        </div>
        <div class="strategy-metric sm-span2 ${staleClass}">
          <div class="sm-label">Last Update</div>
          <div class="sm-value">${lastUpdate}</div>
        </div>
      </div>
      <div class="strategy-card-accent" style="background:${color}"></div>
    `;
  });
}

// ── DOM helpers ──────────────────────────────────────────────────────────────

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function setColorClass(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('positive', 'negative');
  if (value > 0) el.classList.add('positive');
  else if (value < 0) el.classList.add('negative');
}

function applySnapshot(data) {
  setText('total-value',    '$' + fmt(data.total_value_usd));
  setText('cash-value',     '$' + fmt(data.cash_usd));
  setText('realized-pnl',   (data.realized_pnl >= 0 ? '+$' : '-$') + fmt(Math.abs(data.realized_pnl)));
  setText('open-positions', data.open_positions ?? '—');
  setColorClass('realized-pnl', data.realized_pnl);

  const circuitEl = document.getElementById('badge-circuit');
  if (circuitEl) {
    circuitEl.textContent = (data.circuit_state || 'closed').toUpperCase();
    circuitEl.className = 'badge circuit ' + (data.circuit_state || 'closed');
  }

  setText('badge-mode',  data.mode || 'paper');
  setText('last-update', 'Updated ' + new Date(data.timestamp).toLocaleTimeString());
}

function prependTrade(fill) {
  const tbody = document.getElementById('trades-body');
  if (!tbody) return;
  const tr = document.createElement('tr');
  const time = new Date(fill.timestamp || fill.received_at).toLocaleTimeString();
  const sideClass = fill.side === 'BUY' ? 'side-buy' : 'side-sell';
  const strat = fill.strategy || '—';
  const stratColor = strategyColor(strat);
  tr.innerHTML = `
    <td>${time}</td>
    <td><span class="badge-strategy-mini" style="border-color:${stratColor};color:${stratColor}">${strat}</span></td>
    <td>${(fill.token_id || '').slice(0, 10)}…</td>
    <td class="${sideClass}">${fill.side}</td>
    <td>$${fmt(fill.filled_size_usd)}</td>
    <td>${(fill.fill_price ?? 0).toFixed(4)}</td>
    <td>${(fill.slippage_bps ?? 0).toFixed(0)} bps</td>
  `;
  tbody.prepend(tr);
  // Trim to 20 rows
  while (tbody.rows.length > 20) tbody.deleteRow(-1);
}

function addAlert(message) {
  const section = document.getElementById('alerts-section');
  const list    = document.getElementById('alerts-list');
  if (!section || !list) return;
  section.classList.remove('hidden');
  const li = document.createElement('li');
  li.textContent = message;
  list.prepend(li);
}

// ── Metrics / strategies polling ──────────────────────────────────────────────

async function refreshMetrics() {
  try {
    const res = await fetch('/api/metrics');
    if (!res.ok) return;
    const data = await res.json();
    setText('total-trades', data.total_trades ?? '—');
    setText('win-rate', data.win_rate != null ? (data.win_rate * 100).toFixed(1) + '%' : '—');
  } catch (_) { /* silently ignore */ }
}

async function refreshStrategies() {
  try {
    const res = await fetch('/api/strategies');
    if (!res.ok) return;
    const data = await res.json();
    if (data.strategies) {
      renderStrategyCards(data.strategies);
      data.strategies.forEach(s => {
        if (s.last_snapshot_at) {
          updateEquityChart(s.strategy, s.total_value_usd, s.last_snapshot_at);
        }
      });
    }
  } catch (_) { /* silently ignore */ }
}

// ── WebSocket ─────────────────────────────────────────────────────────────────

function connect() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log('[ws] connected');
    refreshMetrics();
    refreshStrategies();
    setInterval(refreshMetrics,    60_000);
    setInterval(refreshStrategies, 60_000);
  };

  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }

    switch (msg.type) {
      case 'snapshot':
        applySnapshot(msg);
        break;
      case 'strategies':
        if (msg.strategies) {
          renderStrategyCards(msg.strategies);
          msg.strategies.forEach(s => {
            if (s.last_snapshot_at) {
              updateEquityChart(s.strategy, s.total_value_usd, s.last_snapshot_at);
            }
          });
        }
        break;
      case 'fill':
        if (msg.fill)      prependTrade(msg.fill);
        if (msg.portfolio) applySnapshot(msg.portfolio);
        break;
      case 'alert':
        addAlert(msg.message);
        break;
      case 'circuit':
        applySnapshot({ ...(window._lastSnapshot || {}), circuit_state: msg.state });
        break;
      case 'pong':
        break;
    }

    if (msg.type === 'snapshot' || msg.type === 'fill') {
      window._lastSnapshot = msg.portfolio || msg;
    }
  };

  ws.onclose = () => {
    console.log(`[ws] disconnected — reconnecting in ${RECONNECT_DELAY_MS}ms`);
    setTimeout(connect, RECONNECT_DELAY_MS);
  };

  ws.onerror = (err) => {
    console.error('[ws] error', err);
    ws.close();
  };
}

// Client-side keepalive ping every 20s
setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send('ping');
}, 20_000);

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmt(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// ── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  initChart();
  connect();
});
