/* ── Dashboard WebSocket client + Chart.js ────────────────────────────────── */

const WS_URL = `ws://${location.host}/ws`;
const RECONNECT_DELAY_MS = 3000;

// Equity curve data (in-memory, capped at 200 points)
const MAX_EQUITY_POINTS = 200;
const equityLabels = [];
const equityData   = [];

let ws   = null;
let chart = null;

// ── Chart setup ──────────────────────────────────────────────────────────────

function initChart() {
  const ctx = document.getElementById('equity-chart').getContext('2d');
  chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: equityLabels,
      datasets: [{
        label: 'Portfolio Value ($)',
        data: equityData,
        borderColor: '#58a6ff',
        backgroundColor: 'rgba(88,166,255,0.08)',
        borderWidth: 1.5,
        pointRadius: 0,
        fill: true,
        tension: 0.3,
      }],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: true,
      plugins: { legend: { display: false } },
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

function pushEquityPoint(totalValue, timestamp) {
  const label = new Date(timestamp).toLocaleTimeString();
  if (equityLabels.length >= MAX_EQUITY_POINTS) {
    equityLabels.shift();
    equityData.shift();
  }
  equityLabels.push(label);
  equityData.push(totalValue);
  chart.update('none');
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

  setText('badge-mode',     data.mode || 'paper');
  setText('badge-strategy', data.strategy || '—');
  setText('last-update',    'Updated ' + new Date(data.timestamp).toLocaleTimeString());

  pushEquityPoint(data.total_value_usd, data.timestamp);
}

function prependTrade(fill) {
  const tbody = document.getElementById('trades-body');
  if (!tbody) return;
  const tr = document.createElement('tr');
  const time = new Date(fill.timestamp || fill.received_at).toLocaleTimeString();
  const sideClass = fill.side === 'BUY' ? 'side-buy' : 'side-sell';
  tr.innerHTML = `
    <td>${time}</td>
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

// ── Metrics polling (every 60s) ───────────────────────────────────────────────

async function refreshMetrics() {
  try {
    const res = await fetch('/api/metrics');
    if (!res.ok) return;
    const data = await res.json();
    setText('total-trades', data.total_trades ?? '—');
    setText('win-rate', data.win_rate != null ? (data.win_rate * 100).toFixed(1) + '%' : '—');
  } catch (_) { /* silently ignore network errors */ }
}

// ── WebSocket ─────────────────────────────────────────────────────────────────

function connect() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log('[ws] connected');
    // Start metrics polling
    refreshMetrics();
    setInterval(refreshMetrics, 60_000);
  };

  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }

    switch (msg.type) {
      case 'snapshot':
        applySnapshot(msg);
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
        break; // keepalive ack
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
