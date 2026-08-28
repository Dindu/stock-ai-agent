// Elite Trading Bot — Command Center
// Live data dashboard with charts, activity feed, market pulse

(function () {
  const REFRESH_MS = 30_000;
  let _curveDays = 14;
  let _feedFilter = 'all';
  let _activityCache = [];
  let _playsFilter = 'all';
  let _tradeDatePreset = 'today';
  let _ordersCache = [];
  let _playsCache = [];
  let _charts = { equity: null, daily: null, trades: null, winRate: null, sparkPnL: null };

  const VIEW_LABELS = {
    overview: 'Overview',
    trades: 'Trades',
    analytics: 'Analytics',
    alerts: 'Alerts',
    integrations: 'Systems'
  };

  function setActiveView(view) {
    const nextView = VIEW_LABELS[view] ? view : 'overview';
    document.body.dataset.activeView = nextView;
    document.querySelectorAll('[data-nav-view]').forEach((button) => {
      const active = button.dataset.navView === nextView;
      button.setAttribute('aria-current', active ? 'page' : 'false');
    });
    const heading = document.querySelector('.view-kicker h1');
    if (heading) heading.textContent = VIEW_LABELS[nextView];
    history.replaceState(null, '', nextView === 'overview' ? '/' : `#${nextView}`);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  document.querySelectorAll('[data-nav-view]').forEach((button) => {
    button.addEventListener('click', () => setActiveView(button.dataset.navView));
  });
  setActiveView(window.location.hash.slice(1) || 'overview');

  // ===== Theme =====
  function initTheme() {
    const saved = localStorage.getItem('etb_theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
    updateThemeIcon(saved);
  }
  function updateThemeIcon(theme) {
    const btn = document.getElementById('themeToggle');
    if (btn) btn.textContent = theme === 'dark' ? '🌙' : '☀️';
  }
  document.getElementById('themeToggle')?.addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('etb_theme', next);
    updateThemeIcon(next);
    renderAllCharts(window._lastEquityCurve || null);
  });

  // ===== Clock =====
  function tickClock() {
    const el = document.getElementById('clock');
    if (!el) return;
    const d = new Date();
    el.textContent = d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) + ' ET';
  }
  setInterval(tickClock, 1000); tickClock();

  // ===== Utilities =====
  const fmtUSD = (n, opts = {}) => {
    if (n == null || isNaN(n)) return '—';
    const sign = n > 0 ? '+' : (n < 0 ? '-' : '');
    return sign + '$' + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: opts.dp ?? 2, maximumFractionDigits: opts.dp ?? 2 });
  };
  const fmtAbsUSD = (n, opts = {}) => {
    if (n == null || isNaN(n)) return '—';
    return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: opts.dp ?? 2, maximumFractionDigits: opts.dp ?? 2 });
  };
  const fmtPct = (n) => (n == null || isNaN(n)) ? '—' : (n > 0 ? '+' : '') + Number(n).toFixed(2) + '%';
  const colorForPnL = (n) => n > 0 ? '#22e5a4' : n < 0 ? '#ff5d7e' : '#aab1d1';
  const pillClassForPnL = (n) => n > 0 ? 'pill pill-green' : n < 0 ? 'pill pill-red' : 'pill pill-gray';
  const timeAgo = (iso) => {
    if (!iso) return '—';
    const now = Date.now();
    const t = new Date(iso).getTime();
    if (isNaN(t)) return '—';
    const s = Math.max(1, Math.floor((now - t) / 1000));
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s/60) + 'm ago';
    if (s < 86400) return Math.floor(s/3600) + 'h ago';
    return Math.floor(s/86400) + 'd ago';
  };
  const setText = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
  const setColor = (id, color) => { const el = document.getElementById(id); if (el) el.style.color = color; };

  const safeFetch = async (url) => {
    try {
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) return { ok: false, status: r.status };
      return await r.json();
    } catch (e) { return { ok: false, error: e.message }; }
  };

  function chartTextColor() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? '#aab1d1' : '#5b6486';
  }
  function chartGridColor() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'rgba(255,255,255,0.06)' : 'rgba(28,35,64,0.06)';
  }

  async function loadAnalyticsSummary() {
    const r = await safeFetch('/api/dashboard');
    if (!Array.isArray(r.trades)) return;
    const controls = document.querySelector('.controls-panel');
    if (controls) controls.hidden = r.controlsEnabled !== true;
    const closed = r.trades.filter((trade) => {
      const status = String(trade.status || '').toUpperCase();
      return status === 'CLOSED' || (trade.exitPrice != null && trade.exitTime);
    });
    const pnls = closed.map((trade) => Number(trade.pnl)).filter(Number.isFinite);
    const wins = pnls.filter((pnl) => pnl > 0);
    const losses = pnls.filter((pnl) => pnl < 0);
    const grossWins = wins.reduce((sum, pnl) => sum + pnl, 0);
    const grossLosses = Math.abs(losses.reduce((sum, pnl) => sum + pnl, 0));
    const realized = pnls.reduce((sum, pnl) => sum + pnl, 0);
    const expectancy = pnls.length ? realized / pnls.length : null;
    setText('analyticsRealizedPnl', fmtUSD(realized));
    setColor('analyticsRealizedPnl', colorForPnL(realized));
    setText('analyticsWinRate', pnls.length ? `${((wins.length / pnls.length) * 100).toFixed(1)}%` : '—');
    setText('analyticsProfitFactor', grossLosses ? (grossWins / grossLosses).toFixed(2) : '—');
    setText('analyticsExpectancy', expectancy == null ? '—' : fmtUSD(expectancy));
    setColor('analyticsExpectancy', colorForPnL(expectancy));
  }

  // ===== KPI HERO + Positions =====
  async function loadKPIs() {
    try {
      const a = await safeFetch('/api/alpaca/snapshot');
      const banner = document.getElementById('dataStatusBanner');
      if (banner) {
        if (a.ok && a.liveData === false) {
          banner.textContent = `Live account data unavailable · ${a.liveDataError || 'Alpaca connection failed'} · Sheets history remains available`;
          banner.classList.add('show');
        } else {
          banner.textContent = '';
          banner.classList.remove('show');
        }
      }
      if (!a.ok) {
        setText('kpiPnL', '—'); setText('kpiPos', '—');
        return;
      }

      const today = a.today || {};
      const liveData = a.liveData !== false;
      const pnl = liveData && today.dayPnL != null ? Number(today.dayPnL) : null;
      const pnlPct = liveData && today.dayPnLPct != null ? Number(today.dayPnLPct) : null;
      const equity = liveData && a.account?.equity != null ? Number(a.account.equity) : null;
      const positions = Array.isArray(a.positions) ? a.positions : [];
      // today.fills is an OBJECT { buys, sells, total } from the snapshot endpoint
      const fillsSummary = (today.fills && typeof today.fills === 'object' && !Array.isArray(today.fills))
        ? today.fills
        : { buys: 0, sells: 0, total: 0 };
      const totalFills = Number(fillsSummary.total ?? 0);
      const tradesTaken = Number(today.tradesTaken ?? fillsSummary.buys ?? 0);

      // Top bar
      setText('topEquity', liveData ? fmtAbsUSD(equity) : 'Unavailable');
      setText('topPnL', fmtUSD(pnl));
      setColor('topPnL', colorForPnL(pnl));

      // KPI 1: Today P&L
      setText('kpiPnL', fmtUSD(pnl));
      setColor('kpiPnL', colorForPnL(pnl));
      setText('kpiPnLSub', liveData ? `${totalFills} fills today` : 'Alpaca unavailable');
      setText('kpiPnLTrend', fmtPct(pnlPct));
      setColor('kpiPnLTrend', colorForPnL(pnl));
      const pnlPill = document.getElementById('kpiPnLPill');
      if (pnlPill) {
        pnlPill.className = pillClassForPnL(pnl);
        pnlPill.textContent = pnl > 0 ? '↑ GAINS' : pnl < 0 ? '↓ LOSS' : 'FLAT';
      }

      // KPI 2: Plays taken — preview from snapshot (loadTodayOrders refines with actual W/L)
      const playsPreview = Number(fillsSummary.sells ?? 0);
      setText('kpiTrades', playsPreview || '—');
      const wlBarSnap = document.getElementById('kpiWLBar');
      if (wlBarSnap && !_ordersCache.length) wlBarSnap.innerHTML = '';
      const tp = document.getElementById('kpiTradesPill');
      if (tp) {
        tp.className = playsPreview > 0 ? 'pill pill-blue' : 'pill pill-gray';
        tp.textContent = playsPreview > 0 ? playsPreview + ' plays' : 'IDLE';
      }

      // KPI 3: Positions
      setText('kpiPos', liveData ? positions.length : '—');
      const posMV = positions.reduce((s, p) => s + Number(p.market_value || 0), 0);
      const posPL = positions.reduce((s, p) => s + Number(p.unrealized_pl || 0), 0);
      setText('kpiPosValue', liveData ? 'Market value ' + fmtAbsUSD(posMV) : 'Live data unavailable');
      setText('kpiPosPnL', liveData ? fmtUSD(posPL) : '—');
      setColor('kpiPosPnL', colorForPnL(posPL));
      const posPill = document.getElementById('kpiPosPill');
      if (posPill) {
        posPill.className = positions.length > 0 ? 'pill pill-blue' : 'pill pill-gray';
        posPill.textContent = !liveData ? 'STALE' : positions.length > 0 ? 'OPEN' : 'FLAT';
      }

      renderPositions(liveData ? positions : [], liveData ? null : 'Live positions unavailable');
    } catch (e) {
      console.error('loadKPIs error', e);
    }
  }

  function renderPositions(positions, emptyMessage = 'No open positions') {
    const list = document.getElementById('positionsList');
    const cnt = document.getElementById('posCount');
    if (cnt) cnt.textContent = positions.length;
    if (!list) return;
    if (!positions.length) {
      list.innerHTML = `<div class="text-center text-xs muted py-8">${emptyMessage}</div>`;
      return;
    }
    list.innerHTML = positions.map(p => {
      const pnl = Number(p.unrealized_pl || 0);
      // unrealized_plpc from Alpaca SDK is already a percentage value (e.g. 10.49)
      const pnlPct = Number(p.unrealized_plpc || 0);
      const sym = p.symbol || '—';
      const qty = p.qty || 0;
      const side = p.side === 'long' ? 'LONG' : 'SHORT';
      const sideClass = p.side === 'long' ? 'pill-green' : 'pill-red';
      const mv = Number(p.market_value || 0);
      const cb = Number(p.cost_basis || 0);
      const avgEntry = qty > 0 ? cb / qty : 0;
      return `
        <div class="tile-row">
          <div>
            <div class="font-semibold text-sm" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${sym}</div>
            <div class="text-[11px] muted">${qty} × ${fmtAbsUSD(avgEntry)}</div>
          </div>
          <span class="pill ${sideClass}">${side}</span>
          <div class="num-mono text-sm text-right">${fmtAbsUSD(mv)}</div>
          <div class="text-right">
            <div class="num-mono text-sm font-bold" style="color:${colorForPnL(pnl)}">${fmtUSD(pnl)}</div>
            <div class="text-[11px] num-mono" style="color:${colorForPnL(pnl)}">${fmtPct(pnlPct)}</div>
          </div>
        </div>`;
    }).join('');
  }

  // ===== Play pairing helper =====
  function computePlays(orders) {
    const bySymbol = {};
    for (const o of orders) {
      if (!bySymbol[o.symbol]) bySymbol[o.symbol] = { buys: [], sells: [] };
      if (o.side === 'buy') bySymbol[o.symbol].buys.push(o);
      else bySymbol[o.symbol].sells.push(o);
    }
    const plays = [];
    for (const [sym, { buys, sells }] of Object.entries(bySymbol)) {
      buys.sort((a, b) => new Date(a.filled_at) - new Date(b.filled_at));
      sells.sort((a, b) => new Date(a.filled_at) - new Date(b.filled_at));
      const buyQ = [...buys];
      for (const sell of sells) {
        const buy = buyQ.shift() || null;
        const sellPrice = Number(sell.filled_avg_price || 0);
        const buyPrice = buy ? Number(buy.filled_avg_price || 0) : 0;
        const qty = Number(sell.qty || 0);
        const pnl = buy ? (sellPrice - buyPrice) * qty : null;
        plays.push({ symbol: sym, sell, buy, pnl, qty, sellPrice, buyPrice });
      }
    }
    return plays;
  }

  function computeSheetPlays(trades) {
    return (Array.isArray(trades) ? trades : [])
      .filter((trade) => {
        const status = String(trade.status || '').toUpperCase();
        return status === 'CLOSED' || (trade.exitPrice != null && trade.exitTime);
      })
      .map((trade) => {
        const entryPrice = Number(trade.entryPrice || 0);
        const exitPrice = Number(trade.exitPrice || 0);
        const pnl = Number(trade.pnl);
        const qty = Number(trade.contracts || trade.quantity || 1);
        return {
          symbol: trade.symbol || '—',
          pnl: Number.isFinite(pnl) ? pnl : null,
          qty,
          buyPrice: entryPrice,
          sellPrice: exitPrice,
          buy: { filled_at: trade.entryTime },
          sell: { filled_at: trade.exitTime || trade.entryTime },
          source: 'google_sheets'
        };
      });
  }

  function updateDashboardSubtitle(orders, plays) {
    const wins = plays.filter(p => p.pnl != null && p.pnl > 0).length;
    const losses = plays.filter(p => p.pnl != null && p.pnl < 0).length;
    const cnt = document.getElementById('fillsCount');
    const playsEl = document.getElementById('fillsPlaysCount');
    const winsEl = document.getElementById('fillsWins');
    const lossesEl = document.getElementById('fillsLosses');
    if (cnt) cnt.textContent = orders.length;
    if (playsEl) playsEl.textContent = plays.length;
    if (winsEl) winsEl.textContent = `${wins}W`;
    if (lossesEl) lossesEl.textContent = `${losses}L`;
  }

  // ===== Today's actual filled orders =====
  async function loadTodayOrders() {
    try {
      const r = await safeFetch('/api/today-orders');
      const usingHistoryFallback = r.ok && r.liveData === false;
      let orders = (r.ok && !usingHistoryFallback && Array.isArray(r.orders)) ? r.orders : [];
      let plays = computePlays(orders);
      if (usingHistoryFallback) {
        const history = await safeFetch('/api/dashboard');
        const today = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
        const todayTrades = Array.isArray(history.trades)
          ? history.trades.filter((trade) => String(trade.entryTime || '').startsWith(today))
          : [];
        plays = computeSheetPlays(todayTrades);
      }
      if (r.ok && r.liveData === false) {
        setText('kpiTrades', '—');
        setText('kpiWins', '—W');
        setText('kpiLosses', '—L');
      }

      // Always update KPI tile with today's data
      const wins = plays.filter(p => p.pnl != null && p.pnl > 0).length;
      const losses = plays.filter(p => p.pnl != null && p.pnl < 0).length;
      const total = plays.length;
      const winRate = total > 0 ? Math.round((wins / total) * 100) : 0;
      setText('kpiTrades', total || '—');
      setText('kpiWins', `${wins}W`);
      setText('kpiLosses', `${losses}L`);
      setText('kpiWinRate', total > 0 ? `(${winRate}%)` : '');
      const tp = document.getElementById('kpiTradesPill');
      if (tp) {
        tp.className = total > 0 ? 'pill pill-blue' : 'pill pill-gray';
        tp.textContent = total > 0 ? `${total} plays` : 'IDLE';
      }
      const wlBar = document.getElementById('kpiWLBar');
      if (wlBar) {
        wlBar.innerHTML = total > 0
          ? plays.slice(-10).map(p => {
              const col = p.pnl == null ? '#888' : p.pnl > 0 ? '#22e5a4' : '#ff5d7e';
              return `<div style="width:8px;height:8px;border-radius:50%;background:${col}"></div>`;
            }).join('')
          : '';
      }

      // Only drive the trade table if user is on the "today" preset
      if (_tradeDatePreset === 'today') {
        _ordersCache = orders;
        _playsCache = plays;
        updateDashboardSubtitle(usingHistoryFallback ? plays : orders, plays);
        renderFills(usingHistoryFallback ? 'plays' : _playsFilter);
      }
    } catch (e) { console.error('loadTodayOrders error', e); }
  }

  // ===== Load orders for a date range =====
  async function loadOrdersRange(from, to) {
    const tbody = document.getElementById('fillsTableBody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="10"><div class="skel h-9 my-1"></div></td></tr>'.repeat(3);
    try {
      const params = new URLSearchParams();
      if (from) params.set('from', from);
      if (to)   params.set('to', to);
      const url = '/api/orders' + (params.toString() ? '?' + params.toString() : '');
      const r = await safeFetch(url);
      const orders = (r.ok && Array.isArray(r.orders)) ? r.orders : [];
      _ordersCache = orders;
      _playsCache = computePlays(orders);
      updateDashboardSubtitle(orders, _playsCache);
      renderFills(_playsFilter);
    } catch (e) { console.error('loadOrdersRange error', e); }
  }

  // ===== Render fills as a table =====
  function renderFills(filter) {
    _playsFilter = filter;
    const tbody = document.getElementById('fillsTableBody');
    if (!tbody) return;

    const emptyRow = (msg) =>
      `<tr><td colspan="10" class="text-center muted py-8" style="font-size:13px">${msg}</td></tr>`;

    if (filter === 'plays' || filter === 'winners' || filter === 'losers') {
      let plays = _playsCache;
      if (filter === 'winners') plays = plays.filter(p => p.pnl != null && p.pnl > 0);
      if (filter === 'losers')  plays = plays.filter(p => p.pnl != null && p.pnl < 0);
      if (!plays.length) { tbody.innerHTML = emptyRow(`No ${filter} found for this period`); return; }
      tbody.innerHTML = plays.slice(0, 500).map((p, i) => {
        const d = new Date(p.sell.filled_at || Date.now());
        const date = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' });
        const time = d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
        const pnlColor = p.pnl == null ? '' : p.pnl > 0 ? '#22e5a4' : '#ff5d7e';
        const arrow   = p.pnl == null ? '' : p.pnl > 0 ? '▲' : '▼';
        const pillCls = p.pnl == null ? 'pill-gray' : p.pnl > 0 ? 'pill-green' : 'pill-red';
        const notional = p.qty * p.sellPrice;
        return `<tr class="fade-in">
          <td class="num-mono muted" style="font-size:11px">${i + 1}</td>
          <td style="font-size:12px;color:#aab1d1">${date}</td>
          <td class="num-mono" style="font-size:11px;color:#aab1d1">${time}</td>
          <td><span class="font-semibold" style="font-size:13px">${p.symbol}</span></td>
          <td><span class="pill ${pillCls}">${arrow} PLAY</span></td>
          <td class="num-mono r" style="font-size:13px">${p.qty}</td>
          <td class="num-mono r" style="font-size:13px">${fmtAbsUSD(p.buyPrice)}</td>
          <td class="num-mono r" style="font-size:13px">${fmtAbsUSD(p.sellPrice)}</td>
          <td class="num-mono r" style="font-size:13px">${fmtAbsUSD(notional)}</td>
          <td class="num-mono r font-bold" style="font-size:13px;color:${pnlColor}">${p.pnl != null ? fmtUSD(p.pnl) : '—'}</td>
        </tr>`;
      }).join('');
    } else {
      let orders = _ordersCache;
      if (filter === 'buys') orders = orders.filter(o => o.side === 'buy');
      if (!orders.length) { tbody.innerHTML = emptyRow(`No ${filter === 'buys' ? 'buy orders' : 'orders'} for this period`); return; }
      tbody.innerHTML = orders.slice(0, 500).map((f, i) => {
        const sideClass = f.side === 'buy' ? 'pill-blue' : 'pill-amber';
        const d = new Date(f.filled_at || Date.now());
        const date = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' });
        const time = d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
        const notional = Number(f.notional || (Number(f.qty) || 0) * (Number(f.filled_avg_price) || 0));
        return `<tr class="fade-in">
          <td class="num-mono muted" style="font-size:11px">${i + 1}</td>
          <td style="font-size:12px;color:#aab1d1">${date}</td>
          <td class="num-mono" style="font-size:11px;color:#aab1d1">${time}</td>
          <td><span class="font-semibold" style="font-size:13px">${f.symbol || '—'}</span></td>
          <td><span class="pill ${sideClass}">${(f.side || '').toUpperCase()}</span></td>
          <td class="num-mono r" style="font-size:13px">${f.qty || 0}</td>
          <td class="num-mono r" style="font-size:13px">${fmtAbsUSD(Number(f.filled_avg_price || 0))}</td>
          <td class="num-mono r muted" style="font-size:13px">—</td>
          <td class="num-mono r" style="font-size:13px">${fmtAbsUSD(notional)}</td>
          <td class="num-mono r muted" style="font-size:13px">—</td>
        </tr>`;
      }).join('');
    }
  }

  // ===== Bot Health =====
  async function loadBotHealth() {
    try {
      const [bot, render] = await Promise.all([
        safeFetch('/api/bot/state'),
        safeFetch('/api/render/status')
      ]);
      const botEl = document.getElementById('kpiBot');
      const pillEl = document.getElementById('kpiBotPill');
      const deployEl = document.getElementById('kpiBotDeploy');
      const marketEl = document.getElementById('kpiBotMarket');
      const marketBadge = document.getElementById('marketBadge');
      const botBadge = document.getElementById('botBadge');

      if (bot.ok) {
        const marketOpen = bot.market?.is_open;
        const scans = bot.scans?.today || {};
        const status = marketOpen ? 'TRADING' : 'STANDBY';
        if (botEl) {
          botEl.textContent = status;
          botEl.style.color = marketOpen ? '#22e5a4' : '#ffb547';
        }
        if (marketBadge) {
          marketBadge.className = marketOpen ? 'pill pill-green' : 'pill pill-amber';
          marketBadge.textContent = `Market: ${marketOpen ? 'OPEN' : 'CLOSED'}`;
        }
        if (marketEl) marketEl.textContent = `${scans.total||0} scans · ${scans.accepted||0}✓`;
      }
      if (render.ok) {
        // Real schema: render.service.suspended (string "not_suspended" or "suspended")
        const suspended = render.service?.suspended;
        const isDown = suspended === 'suspended' || suspended === true;
        if (pillEl) {
          pillEl.className = isDown ? 'pill pill-red' : 'pill pill-green';
          pillEl.textContent = isDown ? '● DOWN' : '● LIVE';
        }
        if (botBadge) {
          botBadge.className = isDown ? 'pill pill-red' : 'pill pill-green';
          botBadge.textContent = `Bot: ${isDown ? 'DOWN' : 'LIVE'}`;
        }
        // Real schema: latestDeploy (not lastDeploy)
        if (deployEl) deployEl.textContent = render.latestDeploy?.finishedAt ? timeAgo(render.latestDeploy.finishedAt) : '—';
      } else {
        if (pillEl) { pillEl.className = 'pill pill-gray'; pillEl.textContent = '—'; }
      }
    } catch (e) { console.error('loadBotHealth error', e); }
  }

  // ===== Market Pulse =====
  async function loadMarketPulse() {
    try {
      const r = await safeFetch('/api/market-pulse?symbols=SPY,QQQ,IWM,DIA,GLD,TLT');
      const el = document.getElementById('marketPulse');
      if (!el) return;
      if (!r.ok || !Array.isArray(r.quotes)) {
        el.innerHTML = `<div class="col-span-2 text-xs muted text-center py-4">Market data unavailable</div>`;
        return;
      }
      el.innerHTML = r.quotes.map(q => {
        const up = q.changePct > 0;
        const color = colorForPnL(q.changePct);
        const arrow = up ? '↑' : q.changePct < 0 ? '↓' : '→';
        return `
          <div class="market-tile">
            <div class="flex items-center justify-between">
              <div class="font-bold text-sm">${q.symbol}</div>
              <div class="text-xs num-mono" style="color:${color}">${arrow} ${fmtPct(q.changePct)}</div>
            </div>
            <div class="num-mono text-lg font-bold mt-1">${fmtAbsUSD(q.price)}</div>
            <div class="text-[11px] muted num-mono mt-0.5">H ${Number(q.high||0).toFixed(2)} · L ${Number(q.low||0).toFixed(2)}</div>
          </div>`;
      }).join('');
    } catch (e) { console.error('loadMarketPulse error', e); }
  }

  // ===== Activity Feed =====
  async function loadActivity() {
    try {
      const r = await safeFetch('/api/activity-feed?limit=40');
      if (!r.ok) return;
      _activityCache = r.events || [];
      renderActivity();
    } catch (e) { console.error('loadActivity error', e); }
  }

  function renderActivity() {
    const el = document.getElementById('activityFeed');
    if (!el) return;
    let events = _activityCache;
    if (_feedFilter !== 'all') events = events.filter(e => e.source === _feedFilter);
    if (!events.length) {
      el.innerHTML = `<div class="text-center text-xs muted py-6">No recent activity</div>`;
      return;
    }
    el.innerHTML = events.slice(0, 30).map(e => {
      const icon = e.source === 'discord' ? '💬' :
                   e.kind === 'TRADE' ? '💸' :
                   e.kind === 'POSITION' ? '📊' :
                   e.kind === 'BRIEFING' ? '📈' :
                   e.kind === 'ALERT' ? '🚨' : '⚙️';
      const desc = (e.description || e.title || '').slice(0, 140);
      return `
        <div class="flex gap-3 p-2.5 rounded-lg hover:bg-white/5 transition">
          <div class="text-lg flex-shrink-0">${icon}</div>
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-0.5">
              <span class="badge" style="background:rgba(79,140,255,0.15);color:#4f8cff">${e.kind || e.source}</span>
              <span class="text-[11px] muted">${timeAgo(e.timestamp)}</span>
            </div>
            <div class="text-xs leading-snug" style="word-break:break-word">${escapeHtml(desc)}</div>
          </div>
        </div>`;
    }).join('');
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  }

  // ===== Charts =====
  async function loadEquityCurve() {
    try {
      const r = await safeFetch(`/api/equity-curve?days=${_curveDays}`);
      if (!r.ok || !Array.isArray(r.curve)) return;
      window._lastEquityCurve = r.curve;
      renderAllCharts(r.curve);
    } catch (e) { console.error('loadEquityCurve error', e); }
  }

  function renderAllCharts(curve) {
    if (!curve || typeof Chart === 'undefined') return;
    try { renderEquityChart(curve); } catch (e) { console.error('equity chart', e); }
    try { renderDailyPnLChart(curve); } catch (e) { console.error('daily chart', e); }
    try { renderTradesChart(curve); } catch (e) { console.error('trades chart', e); }
    try { renderWinRateChart(curve); } catch (e) { console.error('wr chart', e); }
    try { renderSparkPnL(curve); } catch (e) { console.error('spark chart', e); }
  }

  function renderEquityChart(curve) {
    const ctx = document.getElementById('equityChart');
    if (!ctx) return;
    if (_charts.equity) _charts.equity.destroy();
    const labels = curve.map(p => p.date.slice(5));
    const data = curve.map(p => p.equity);
    const gradient = ctx.getContext('2d').createLinearGradient(0, 0, 0, 240);
    gradient.addColorStop(0, 'rgba(79,140,255,0.35)');
    gradient.addColorStop(1, 'rgba(79,140,255,0.02)');
    _charts.equity = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'Equity',
          data,
          borderColor: '#4f8cff',
          backgroundColor: gradient,
          borderWidth: 2.5,
          tension: 0.35,
          fill: true,
          pointRadius: 0,
          pointHoverRadius: 5,
          pointHoverBackgroundColor: '#4f8cff'
        }]
      },
      options: chartOpts({ y: { ticks: { callback: v => '$' + (v/1000).toFixed(1) + 'k' } } })
    });
  }

  function renderDailyPnLChart(curve) {
    const ctx = document.getElementById('dailyPnlChart');
    if (!ctx) return;
    if (_charts.daily) _charts.daily.destroy();
    const labels = curve.map(p => p.date.slice(5));
    const data = curve.map(p => p.dailyPnL);
    const colors = data.map(v => v >= 0 ? 'rgba(34,229,164,0.85)' : 'rgba(255,93,126,0.85)');
    _charts.daily = new Chart(ctx, {
      type: 'bar',
      data: { labels, datasets: [{ data, backgroundColor: colors, borderRadius: 6 }] },
      options: chartOpts({ legend: false, y: { ticks: { callback: v => '$' + v } } })
    });
  }

  function renderTradesChart(curve) {
    const ctx = document.getElementById('tradesChart');
    if (!ctx) return;
    if (_charts.trades) _charts.trades.destroy();
    const labels = curve.map(p => p.date.slice(5));
    const data = curve.map(p => p.trades);
    _charts.trades = new Chart(ctx, {
      type: 'bar',
      data: { labels, datasets: [{ data, backgroundColor: 'rgba(181,109,255,0.75)', borderRadius: 6 }] },
      options: chartOpts({ legend: false })
    });
  }

  function renderWinRateChart(curve) {
    const ctx = document.getElementById('winRateChart');
    if (!ctx) return;
    if (_charts.winRate) _charts.winRate.destroy();
    const labels = curve.map(p => p.date.slice(5));
    const data = curve.map(p => p.winRate);
    _charts.winRate = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data,
          borderColor: '#ffb547',
          backgroundColor: 'rgba(255,181,71,0.15)',
          borderWidth: 2,
          tension: 0.4,
          fill: true,
          pointRadius: 3,
          pointBackgroundColor: '#ffb547'
        }]
      },
      options: chartOpts({ legend: false, y: { min: 0, max: 100, ticks: { callback: v => v + '%' } } })
    });
  }

  function renderSparkPnL(curve) {
    const ctx = document.getElementById('sparkPnL');
    if (!ctx) return;
    if (_charts.sparkPnL) _charts.sparkPnL.destroy();
    const data = curve.map(p => p.dailyPnL);
    _charts.sparkPnL = new Chart(ctx, {
      type: 'line',
      data: {
        labels: data.map((_,i)=>i),
        datasets: [{
          data,
          borderColor: '#22e5a4',
          backgroundColor: 'rgba(34,229,164,0.18)',
          borderWidth: 1.5,
          tension: 0.4,
          fill: true,
          pointRadius: 0
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false } }
      }
    });
  }

  function chartOpts(extra = {}) {
    const txt = chartTextColor();
    const grid = chartGridColor();
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: extra.legend === undefined ? false : extra.legend, labels: { color: txt } },
        tooltip: {
          backgroundColor: 'rgba(7,9,18,0.95)',
          borderColor: 'rgba(255,255,255,0.1)',
          borderWidth: 1,
          padding: 10,
          titleColor: '#fff',
          bodyColor: '#aab1d1'
        }
      },
      scales: {
        x: { ticks: { color: txt, font: { family: 'JetBrains Mono', size: 10 } }, grid: { display: false } },
        y: {
          ticks: { color: txt, font: { family: 'JetBrains Mono', size: 10 }, ...(extra.y?.ticks||{}) },
          grid: { color: grid },
          ...(extra.y ? { min: extra.y.min, max: extra.y.max } : {})
        }
      }
    };
  }

  // ===== Service Health =====
  async function loadServiceHealth() {
    try {
      const r = await safeFetch('/api/services/health');
      const el = document.getElementById('servicesGrid');
      const overallEl = document.getElementById('healthOverall');
      if (!el) return;
      if (!r.ok || !Array.isArray(r.services)) {
        el.innerHTML = `<div class="col-span-full text-xs muted text-center py-2">Health check unavailable</div>`;
        if (overallEl) { overallEl.className = 'pill pill-red'; overallEl.textContent = 'ERROR'; }
        return;
      }
      if (overallEl) {
        const allOk = r.overall === 'healthy';
        overallEl.className = allOk ? 'pill pill-green' : 'pill pill-amber';
        overallEl.textContent = `${r.okCount}/${r.totalCount} ${allOk ? 'HEALTHY' : 'DEGRADED'}`;
      }
      el.innerHTML = r.services.map(s => {
        const dotClass = s.ok ? 'pill-green' : 'pill-red';
        const dotEmoji = s.ok ? '●' : '○';
        const latencyTxt = s.ok ? `${s.latencyMs}ms` : 'fail';
        const latencyColor = s.latencyMs < 1000 ? '#22e5a4' : s.latencyMs < 2500 ? '#ffb547' : '#ff5d7e';
        const detail = s.ok ? (s.detail || '') : (s.error || '').slice(0, 24);
        return `
          <div class="market-tile" style="padding:8px 10px" title="${escapeHtml(s.name + (s.error ? ': ' + s.error : ''))}">
            <div class="flex items-center justify-between gap-1">
              <span class="text-[11px] font-semibold truncate">${s.name}</span>
              <span style="color:${s.ok ? '#22e5a4' : '#ff5d7e'};font-size:10px">${dotEmoji}</span>
            </div>
            <div class="flex items-center justify-between mt-1">
              <span class="text-[10px] muted truncate" style="max-width:70%">${escapeHtml(detail)}</span>
              <span class="text-[10px] num-mono" style="color:${s.ok ? latencyColor : '#ff5d7e'}">${latencyTxt}</span>
            </div>
          </div>`;
      }).join('');
    } catch (e) { console.error('loadServiceHealth', e); }
  }

  // ===== Scan Decisions Stream =====
  let _scanFilter = 'all';
  let _scanCache = [];
  async function loadScanDecisions() {
    try {
      const r = await safeFetch('/api/scan-decisions?limit=80');
      if (!r.ok) return;
      _scanCache = Array.isArray(r.decisions) ? r.decisions : [];
      const stats = document.getElementById('scanStats');
      if (stats) {
        stats.textContent = `${r.total||0} today · ${r.accepted||0}✓ · ${r.rejected||0}✗`;
        stats.className = r.accepted > 0 ? 'pill pill-green' : 'pill pill-amber';
      }
      renderScans();
    } catch (e) { console.error('loadScanDecisions', e); }
  }

  function renderScans() {
    const el = document.getElementById('scanList');
    if (!el) return;
    let arr = _scanCache;
    if (_scanFilter !== 'all') arr = arr.filter(d => d.decision === _scanFilter);
    if (!arr.length) {
      el.innerHTML = `<div class="text-center text-xs muted py-6">No scan decisions ${_scanFilter !== 'all' ? '('+_scanFilter+')' : 'yet today'}</div>`;
      return;
    }
    el.innerHTML = arr.slice(0, 50).map(d => {
      const t = new Date(d.timestamp);
      const time = t.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
      const accepted = d.decision === 'ACCEPTED';
      const decPill = accepted ? 'pill-green' : 'pill-red';
      const decIcon = accepted ? '✓' : '✗';
      const engineColor = {
        'PULLBACK': '#4f8cff',
        'BREAKDOWN': '#ff5d7e',
        'TREND_CONT': '#22e5a4',
        'EARLY_CONT': '#b56dff'
      }[d.engine] || '#aab1d1';
      const biasColor = d.bias === 'bullish' ? '#22e5a4' : d.bias === 'bearish' ? '#ff5d7e' : '#aab1d1';
      const reason = d.gateReason ? `<span class="text-[11px] muted">— ${escapeHtml(d.gateReason)}</span>` : '';
      return `
        <div class="flex items-center gap-3 p-2 rounded-lg hover:bg-white/5 transition" style="border-left:2px solid ${engineColor}">
          <span class="num-mono text-[11px] muted" style="min-width:70px">${time}</span>
          <span class="font-bold text-sm" style="min-width:60px">${d.symbol||'?'}</span>
          <span class="text-[10px] font-bold px-1.5 py-0.5 rounded" style="background:${engineColor}22;color:${engineColor};min-width:90px;text-align:center">${d.engine||'?'}</span>
          <span class="pill ${decPill}" style="min-width:34px;justify-content:center">${decIcon}</span>
          <span class="text-[11px]" style="color:${biasColor}">${(d.bias||'').toUpperCase()||'—'}</span>
          ${reason}
        </div>`;
    }).join('');
  }

  document.getElementById('scanFilter')?.addEventListener('change', (event) => {
    _scanFilter = event.target.value;
    renderScans();
  });

  // ===== Strategy Stats =====
  async function loadStrategyStats() {
    try {
      const r = await safeFetch('/api/strategy-stats?days=7');
      if (!r.ok) return;
      const enginesEl = document.getElementById('engineStats');
      const reasonsEl = document.getElementById('rejectReasons');
      const engines = Array.isArray(r.engines) ? r.engines : [];
      if (enginesEl) {
        if (!engines.length) {
          enginesEl.innerHTML = `<div class="text-xs muted text-center py-3">No data yet</div>`;
        } else {
          const maxTotal = Math.max(...engines.map(e => e.total));
          enginesEl.innerHTML = engines.map(e => {
            const widthPct = maxTotal > 0 ? (e.total / maxTotal) * 100 : 0;
            const acceptColor = e.acceptRate >= 30 ? '#22e5a4' : e.acceptRate >= 10 ? '#ffb547' : '#ff5d7e';
            const engineColor = {
              'PULLBACK': '#4f8cff',
              'BREAKDOWN': '#ff5d7e',
              'TREND_CONT': '#22e5a4',
              'EARLY_CONT': '#b56dff'
            }[e.engine] || '#aab1d1';
            return `
              <div class="p-2 rounded-lg" style="background:rgba(255,255,255,0.03)">
                <div class="flex items-center justify-between mb-1">
                  <span class="text-xs font-bold" style="color:${engineColor}">${e.engine}</span>
                  <span class="text-[11px] num-mono" style="color:${acceptColor}">${e.acceptRate}% accept</span>
                </div>
                <div class="relative h-2 rounded-full overflow-hidden" style="background:rgba(255,255,255,0.05)">
                  <div style="background:${engineColor};opacity:.7;height:100%;width:${widthPct}%"></div>
                </div>
                <div class="flex items-center justify-between mt-1 text-[10px] num-mono muted">
                  <span>${e.accepted}✓ · ${e.rejected}✗</span>
                  <span>${e.total} total</span>
                </div>
              </div>`;
          }).join('');
        }
      }
      const reasons = Array.isArray(r.topRejectReasons) ? r.topRejectReasons : [];
      if (reasonsEl) {
        if (!reasons.length) {
          reasonsEl.innerHTML = `<div class="text-xs muted text-center py-2">—</div>`;
        } else {
          const maxCount = Math.max(...reasons.map(r => r.count));
          reasonsEl.innerHTML = reasons.slice(0, 6).map(r => {
            const w = (r.count / maxCount) * 100;
            return `
              <div class="flex items-center gap-2 text-[11px]">
                <span class="truncate flex-1" title="${escapeHtml(r.reason)}">${escapeHtml(r.reason)}</span>
                <div class="relative h-1.5 rounded-full overflow-hidden" style="width:60px;background:rgba(255,255,255,0.05)">
                  <div style="background:#ff5d7e;opacity:.6;height:100%;width:${w}%"></div>
                </div>
                <span class="num-mono muted" style="min-width:20px;text-align:right">${r.count}</span>
              </div>`;
          }).join('');
        }
      }
    } catch (e) { console.error('loadStrategyStats', e); }
  }

  // ===== Bot Controls =====
  async function postJSON(url) {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    return r.json().catch(() => ({ ok: false }));
  }
  function flashStatus(text, color = '#22e5a4') {
    const el = document.getElementById('ctrlStatus');
    if (!el) return;
    el.textContent = text;
    el.style.background = color + '22';
    el.style.color = color;
    el.style.borderColor = color + '4d';
    setTimeout(() => {
      el.className = 'pill pill-gray';
      el.style = '';
      el.textContent = 'Ready';
    }, 5000);
  }
  document.getElementById('btnCloseAll')?.addEventListener('click', async () => {
    if (!confirm('⚠ Close ALL open positions at market?\n\nThis will market-sell every contract.')) return;
    flashStatus('Closing positions...', '#ffb547');
    const r = await postJSON('/api/bot/close-all');
    if (r.ok) {
      flashStatus(`✓ ${r.closed}/${r.total} closed`, r.failed > 0 ? '#ffb547' : '#22e5a4');
      setTimeout(refreshAll, 1500);
    } else {
      flashStatus('✗ Failed', '#ff5d7e');
    }
  });
  document.getElementById('btnCancelOrders')?.addEventListener('click', async () => {
    if (!confirm('⚠ Cancel ALL open orders?')) return;
    flashStatus('Cancelling orders...', '#ffb547');
    const r = await postJSON('/api/bot/cancel-orders');
    if (r.ok) {
      flashStatus(`✓ ${r.cancelled}/${r.total} cancelled`, r.failed > 0 ? '#ffb547' : '#22e5a4');
      setTimeout(refreshAll, 1500);
    } else {
      flashStatus('✗ Failed', '#ff5d7e');
    }
  });

  // ===== Range / filter buttons =====
  document.querySelectorAll('.curve-range').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.curve-range').forEach(x => { x.classList.remove('pill-blue'); x.classList.add('pill-gray'); });
      b.classList.remove('pill-gray'); b.classList.add('pill-blue');
      _curveDays = Number(b.dataset.days);
      loadEquityCurve();
    });
  });
  document.querySelectorAll('.feed-filter').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.feed-filter').forEach(x => { x.classList.remove('pill-blue'); x.classList.add('pill-gray'); });
      b.classList.remove('pill-gray'); b.classList.add('pill-blue');
      _feedFilter = b.dataset.filter;
      renderActivity();
    });
  });
  // ===== Trade period filter =====
  function applyDatePreset(preset) {
    _tradeDatePreset = preset;
    const todayStr = new Date().toISOString().slice(0, 10);
    const daysAgo = (n) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);
    let from = null, to = null;
    if (preset === 'today')     { from = todayStr; to = todayStr; }
    else if (preset === 'yesterday') { from = daysAgo(1); to = daysAgo(1); }
    else if (preset === '7d')   { from = daysAgo(7);  to = todayStr; }
    else if (preset === '30d')  { from = daysAgo(30); to = todayStr; }
    // 'all' → from=null, to=null
    if (preset === 'today') {
      // reuse cached today's data (no extra fetch needed)
      renderFills(_playsFilter);
    } else {
      loadOrdersRange(from, to);
    }
  }
  document.getElementById('tradePeriodFilter')?.addEventListener('change', (event) => {
    applyDatePreset(event.target.value);
  });

  // ===== Refresh all (each loader is independently try/caught) =====
  async function refreshAll() {
    setText('lastUpdate', new Date().toLocaleTimeString('en-US', { hour12: false }));
    // Use allSettled so one failure doesn't kill the others
    await Promise.allSettled([
      loadKPIs(),
      loadTodayOrders(),
      loadAnalyticsSummary(),
      loadBotHealth(),
      loadMarketPulse(),
      loadActivity(),
      loadEquityCurve(),
      loadServiceHealth(),
      loadScanDecisions(),
      loadStrategyStats()
    ]);
  }

  document.getElementById('refreshBtn')?.addEventListener('click', () => {
    const btn = document.getElementById('refreshBtn');
    btn.style.transform = 'rotate(360deg)';
    btn.style.transition = 'transform .6s ease';
    setTimeout(() => { btn.style.transform = ''; btn.style.transition = ''; }, 700);
    refreshAll();
  });

  // ===== Boot =====
  initTheme();
  refreshAll();
  setInterval(refreshAll, REFRESH_MS);

})();
