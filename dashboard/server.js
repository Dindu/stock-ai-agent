// Minimal Express server to serve dashboard and Google Sheets data
const express = require('express');
const path = require('path');
require('dotenv').config({
  path: path.join(__dirname, '..', '.env'),
  quiet: true
});

// Align dashboard names with the Python bot without duplicating credentials.
process.env.GOOGLE_SHEET_ID ||= process.env.GOOGLE_SPREADSHEET_ID;
process.env.ALPACA_API_KEY_ID ||= process.env.ALPACA_API_KEY;

const DASHBOARD_ENABLE_CONTROLS = process.env.DASHBOARD_ENABLE_CONTROLS === '1';

const app = express();
const PORT = process.env.PORT || 3001;

function isOccOptionSymbol(symbol) {
  return /^[A-Z]{1,6}\d{6}[CP]\d{8}$/.test(String(symbol || '').toUpperCase());
}

function parseNum(value) {
  const n = parseFloat(value);
  return Number.isFinite(n) ? n : null;
}

function getTradeDollarPnl(trade) {
  if (!trade) return 0;

  const storedPnl = parseNum(trade.pnl);
  if (storedPnl !== null) return storedPnl;

  const entry = parseNum(trade.entryPrice);
  const exit = parseNum(trade.exitPrice);
  if (entry === null || exit === null) return 0;

  const qty = parseNum(trade.contracts ?? trade.quantity ?? 1) ?? 1;
  const optionLike = isOccOptionSymbol(trade.symbol) || ['call', 'put'].includes(String(trade.direction || '').toLowerCase());
  const multiplier = optionLike ? 100 : 1;

  return (exit - entry) * qty * multiplier;
}

async function fetchTradesFromGoogleSheets() {
  try {
    const googleSheetsService = require('./google_sheets_service');
    const tradesObj = await googleSheetsService.getAllTrades();
    const trades = Object.values(tradesObj || {}).filter(Boolean);
    return { trades, source: 'google_sheets' };
  } catch (error) {
    console.error('❌ Google Sheets fetch failed:', error.message);
    return { trades: [], source: 'google_sheets_error' };
  }
}

function buildClosedTradesFromAlpacaOrders(orders) {
  const bySymbol = new Map();

  const sorted = (Array.isArray(orders) ? orders : [])
    .filter(o => parseNum(o.filled_qty) !== null && parseNum(o.filled_avg_price) !== null)
    .sort((a, b) => new Date(a.created_at || 0) - new Date(b.created_at || 0));

  sorted.forEach(order => {
    const symbol = String(order.symbol || '').toUpperCase();
    if (!symbol) return;
    if (!bySymbol.has(symbol)) bySymbol.set(symbol, []);
    bySymbol.get(symbol).push(order);
  });

  const trades = [];
  let seq = 1;

  bySymbol.forEach((symbolOrders, symbol) => {
    const openBuys = [];

    symbolOrders.forEach(order => {
      const side = String(order.side || '').toLowerCase();
      const qty = parseNum(order.filled_qty) ?? 0;
      const price = parseNum(order.filled_avg_price);
      if (qty <= 0 || price === null) return;

      if (side === 'buy') {
        openBuys.push({
          id: order.id,
          qtyRemaining: qty,
          price,
          time: order.created_at
        });
        return;
      }

      if (side === 'sell') {
        let qtyToClose = qty;
        while (qtyToClose > 0 && openBuys.length > 0) {
          const entry = openBuys[0];
          const matchedQty = Math.min(entry.qtyRemaining, qtyToClose);

          const optionLike = isOccOptionSymbol(symbol);
          const multiplier = optionLike ? 100 : 1;
          const pnl = (price - entry.price) * matchedQty * multiplier;

          trades.push({
            tradeId: `ALPACA-${seq++}`,
            symbol,
            entryPrice: entry.price,
            exitPrice: price,
            entryTime: entry.time,
            exitTime: order.created_at,
            status: 'closed',
            direction: optionLike ? null : 'long',
            contracts: matchedQty,
            pnl
          });

          entry.qtyRemaining -= matchedQty;
          qtyToClose -= matchedQty;
          if (entry.qtyRemaining <= 0) openBuys.shift();
        }
      }
    });
  });

  return trades;
}

async function fetchTradesFromAlpaca() {
  try {
    const AlpacaService = require('./alpaca_service');
    const alpaca = new AlpacaService();
    const orders = await alpaca.getOrders('all');
    const trades = buildClosedTradesFromAlpacaOrders(orders);
    return { trades, source: 'alpaca' };
  } catch (error) {
    console.error('❌ Alpaca fallback fetch failed:', error.message);
    return { trades: [], source: 'alpaca_error' };
  }
}

// Add request logging middleware
app.use((req, res, next) => {
  console.log(`📡 ${new Date().toISOString()} - ${req.method} ${req.url}`);
  next();
});

// Serve static files (dashboard)
app.use(express.static(path.join(__dirname, 'public')));

// Friendly aliases — all point to the single consolidated dashboard
app.get(['/new', '/dashboard', '/command-center'], (_req, res) => {
  res.redirect('/');
});

// Health check endpoint
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', time: new Date() });
});

// API endpoint for dashboard data
app.get('/api/dashboard', async (req, res) => {
  try {
    // Set no-cache headers to ensure fresh data
    res.set({
      'Cache-Control': 'no-cache, no-store, must-revalidate',
      'Pragma': 'no-cache',
      'Expires': '0'
    });
    
    // Data source priority: Google Sheets -> Alpaca fallback.
    const googleResult = await fetchTradesFromGoogleSheets();
    let trades = googleResult.trades;
    let dataSource = googleResult.source;

    if (!Array.isArray(trades) || trades.length === 0) {
      const alpacaResult = await fetchTradesFromAlpaca();
      trades = alpacaResult.trades;
      dataSource = `${googleResult.source}->${alpacaResult.source}`;
    }

    // Map active trades
    const activeTrades = {};
    trades.forEach(trade => {
      if (!trade.exitPrice) {
        activeTrades[trade.tradeId] = trade;
      }
    });

    // Map daily history
    const dayMap = {};
    trades.forEach(trade => {
      const entryDate = new Date(trade.entryTime || trade.time);
      const dayKey = entryDate.toISOString().slice(0, 10);
      if (!dayMap[dayKey]) {
        dayMap[dayKey] = {
          date: dayKey,
          totalTrades: 0,
          totalPnL: 0,
          winningTrades: 0
        };
      }
      dayMap[dayKey].totalTrades++;
      if (trade.exitPrice) {
        const pnl = getTradeDollarPnl(trade);
        dayMap[dayKey].totalPnL += pnl;
        if (pnl > 0) dayMap[dayKey].winningTrades++;
      }
    });
    const dailyHistory = Object.values(dayMap).sort((a, b) => new Date(a.date) - new Date(b.date));

    // Calculate weekly win rate (last 7 days)
    const now = new Date();
    const sevenDaysAgo = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
    
    const weeklyTrades = trades.filter(trade => {
      if (!trade.exitTime && !trade.exitDate) return false;
      const exitDate = new Date(trade.exitTime || trade.exitDate);
      return exitDate >= sevenDaysAgo && exitDate <= now;
    });
    
    const weeklyWinningTrades = weeklyTrades.filter(trade => {
      const pnl = getTradeDollarPnl(trade);
      return pnl > 0;
    }).length;
    
    const weeklyWinRate = weeklyTrades.length > 0 ? (weeklyWinningTrades / weeklyTrades.length) * 100 : 0;

    // Metrics with Paper Trading Balance Tracking
    const totalTrades = trades.length;
    const totalPnL = trades.reduce((sum, t) => sum + getTradeDollarPnl(t), 0);
    const winningTrades = trades.filter(t => getTradeDollarPnl(t) > 0).length;
    const winRate = totalTrades > 0 ? (winningTrades / totalTrades) * 100 : 0;
    
    // Paper Trading Account Balance Calculations
    const STARTING_BALANCE = 100000; // $100K from config
    
    // Estimate invested capital from entries where sizing exists.
    let totalInvested = 0;
    
    trades.forEach(trade => {
      if (trade.entryPrice && trade.exitPrice) {
        const contracts = parseFloat(trade.contracts ?? trade.quantity) || 1;
        const optionLike = isOccOptionSymbol(trade.symbol) || ['call', 'put'].includes(String(trade.direction || '').toLowerCase());
        const multiplier = optionLike ? 100 : 1;
        const entryValue = contracts * trade.entryPrice * multiplier;
        totalInvested += entryValue;
      }
    });

    const finalDollarPnL = totalPnL;
    const finalCurrentBalance = STARTING_BALANCE + finalDollarPnL;
    const finalAccountReturn = (finalDollarPnL / STARTING_BALANCE) * 100;
    
    const metrics = {
      totalTrades,
      totalPnL,
      winRate,
      weeklyWinRate,
      weeklyTrades: weeklyTrades.length,
      // Paper Trading Metrics
      startingBalance: STARTING_BALANCE,
      currentBalance: finalCurrentBalance,
      dollarPnL: finalDollarPnL,
      accountReturn: finalAccountReturn,
      totalInvested: totalInvested
    };

    console.log('📊 Dashboard data prepared:', {
      dataSource,
      tradesCount: trades.length,
      activeTrades: Object.keys(activeTrades).length,
      dailyHistoryDays: dailyHistory.length,
      totalPnL,
      winRate
    });

    // Debug: Log first few trades to see actual data structure
    if (trades.length > 0) {
      console.log('📊 Sample trade data:', trades.slice(0, 3).map(t => ({
        tradeId: t.tradeId,
        symbol: t.symbol,
        status: t.status,
        exitPrice: t.exitPrice,
        exitTime: t.exitTime
      })));
    }

    // Recent exits - showing ALL exited/closed trades (not just last 24 hours)
    const recentExits = trades.filter(trade => {
      return trade.status === 'exited' || trade.status === 'closed';
    });

    console.log('📊 Recent exits debug:', {
      totalTrades: trades.length,
      exitedTrades: trades.filter(t => t.status === 'exited').length,
      tradesWithExitTime: trades.filter(t => t.exitTime).length,
      recentExitsCount: recentExits.length,
      statusValues: [...new Set(trades.map(t => t.status))].filter(Boolean)
    });

    res.json({
      trades: trades || [],
      dailyHistory: dailyHistory || [],
      activeTrades: activeTrades || {},
      recentExits: recentExits || [],
      metrics: metrics || {},
      controlsEnabled: DASHBOARD_ENABLE_CONTROLS,
      dataSource,
      aiDecisions: { mode: 'ADAPTIVE', marketCondition: 'NEUTRAL' },
      advancedMetrics: {},
      aiMetrics: {}
    });
  } catch (error) {
    console.error('Error fetching trades:', error);
    res.status(500).json({ error: error.message, stack: error.stack });
  }
});

// Debug endpoint — shows exactly what's failing on Render
app.get('/api/debug', async (req, res) => {
  const report = {
    env: {
      GOOGLE_SHEET_ID:              !!process.env.GOOGLE_SHEET_ID,
      GOOGLE_SERVICE_ACCOUNT_EMAIL: !!process.env.GOOGLE_SERVICE_ACCOUNT_EMAIL,
      GOOGLE_PRIVATE_KEY:           !!process.env.GOOGLE_PRIVATE_KEY,
      GOOGLE_PRIVATE_KEY_starts:    (process.env.GOOGLE_PRIVATE_KEY || '').slice(0, 40),
      GOOGLE_PRIVATE_KEY_has_newlines: (process.env.GOOGLE_PRIVATE_KEY || '').includes('\n'),
      ALPACA_API_KEY_ID:            !!process.env.ALPACA_API_KEY_ID,
      ALPACA_KEY_ID_fallback:       !!process.env.ALPACA_KEY_ID,
      ALPACA_SECRET_KEY:            !!process.env.ALPACA_SECRET_KEY,
      RENDER_API_KEY:               !!process.env.RENDER_API_KEY,
      NODE_ENV:                     process.env.NODE_ENV || 'not set',
    },
    googleSheets: { status: 'not tested', error: null, tradeCount: 0 },
    alpaca:       { status: 'not tested', error: null, tradeCount: 0 },
  };

  // Test Google Sheets
  try {
    const result = await fetchTradesFromGoogleSheets();
    report.googleSheets.status = result.source;
    report.googleSheets.tradeCount = result.trades.length;
    if (result.trades.length > 0) {
      report.googleSheets.sampleTrade = result.trades[0];
    }
  } catch (e) {
    report.googleSheets.status = 'exception';
    report.googleSheets.error = e.message;
  }

  // Test Alpaca
  try {
    const result = await fetchTradesFromAlpaca();
    report.alpaca.status = result.source;
    report.alpaca.tradeCount = result.trades.length;
  } catch (e) {
    report.alpaca.status = 'exception';
    report.alpaca.error = e.message;
  }

  res.json(report);
});

// ─────────────────────────────────────────────────────────────────────────────
// CONTROL CENTER ENDPOINTS — consolidate Alpaca / Render / Discord / Bot State
// ─────────────────────────────────────────────────────────────────────────────

// Lazy-loaded singletons so the dashboard still starts even if a service fails
let _alpaca = null;
function getAlpaca() {
  if (!_alpaca) {
    const AlpacaService = require('./alpaca_service');
    _alpaca = new AlpacaService();
  }
  return _alpaca;
}

// --- ALPACA: account + positions + open orders + today's activity ---
app.get('/api/alpaca/snapshot', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    const alpaca = getAlpaca();
    const [accountResult, positionsResult, openOrdersResult, allOrdersResult] = await Promise.all([
      alpaca.getAccount().then(value => ({ value })).catch(error => ({ error: error.message })),
      alpaca.getPositions().then(value => ({ value })).catch(error => ({ error: error.message })),
      alpaca.getOrders('open').then(value => ({ value })).catch(error => ({ error: error.message })),
      alpaca.getOrders('all').then(value => ({ value })).catch(error => ({ error: error.message }))
    ]);
    const failures = [accountResult, positionsResult, openOrdersResult, allOrdersResult]
      .filter(result => result.error)
      .map(result => result.error);
    const account = accountResult.value || null;
    const positions = positionsResult.value || [];
    const openOrders = openOrdersResult.value || [];
    const allOrders = allOrdersResult.value || [];

    // ── Today's P&L (from account equity vs last_equity) ──
    const dayPnL = (account && !account.error && typeof account.last_equity === 'number')
      ? (account.equity - account.last_equity)
      : null;
    const dayPnLPct = (dayPnL !== null && account.last_equity > 0)
      ? (dayPnL / account.last_equity) * 100
      : null;

    // ── Today's trade activity (filled orders created today, ET) ──
    // Convert "now" to ET, then build start-of-day in ET as UTC ISO
    const nowET = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
    const startOfDayET = new Date(nowET);
    startOfDayET.setHours(0, 0, 0, 0);
    // Compute the offset between machine local-clock interpretation of ET and actual UTC.
    // Easier: just check the date string match.
    const todayStrET = nowET.toISOString().slice(0, 10);

    const filledToday = (Array.isArray(allOrders) ? allOrders : []).filter(o => {
      if (!o.created_at) return false;
      const status = String(o.status || '').toLowerCase();
      if (!['filled', 'partially_filled'].includes(status)) return false;
      // Convert order timestamp to ET date string and compare
      const dET = new Date(new Date(o.created_at).toLocaleString('en-US', { timeZone: 'America/New_York' }));
      const dStr = dET.toISOString().slice(0, 10);
      return dStr === todayStrET;
    });

    const buysToday = filledToday.filter(o => String(o.side).toLowerCase() === 'buy').length;
    const sellsToday = filledToday.filter(o => String(o.side).toLowerCase() === 'sell').length;
    // "Trades taken today" = entries opened = filled BUY orders today
    const tradesTakenToday = buysToday;

    res.json({
      ok: true,
      time: new Date().toISOString(),
      paper: true,
      liveData: failures.length === 0,
      liveDataError: failures[0] || null,
      account,
      positions,
      openOrders,
      today: {
        dayPnL,
        dayPnLPct,
        tradesTaken: tradesTakenToday,
        fills: { buys: buysToday, sells: sellsToday, total: filledToday.length }
      }
    });
  } catch (error) {
    console.error('❌ /api/alpaca/snapshot error:', error.message);
    res.status(500).json({ ok: false, error: error.message });
  }
});

// --- Alpaca: today's filled orders (full list) ---
app.get('/api/today-orders', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    const alpaca = getAlpaca();
    try {
      await alpaca.getAccount();
    } catch (error) {
      return res.json({
        ok: true,
        liveData: false,
        liveDataError: error.message,
        time: new Date().toISOString(),
        count: null,
        orders: []
      });
    }
    let allOrders;
    try {
      allOrders = await alpaca.getOrders('all');
    } catch (error) {
      return res.json({
        ok: true,
        liveData: false,
        liveDataError: error.message,
        time: new Date().toISOString(),
        count: null,
        orders: []
      });
    }
    const nowET = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
    const todayStrET = nowET.toISOString().slice(0, 10);
    const fills = (Array.isArray(allOrders) ? allOrders : []).filter(o => {
      if (!o.created_at) return false;
      const status = String(o.status || '').toLowerCase();
      if (!['filled', 'partially_filled'].includes(status)) return false;
      const dET = new Date(new Date(o.filled_at || o.created_at).toLocaleString('en-US', { timeZone: 'America/New_York' }));
      return dET.toISOString().slice(0, 10) === todayStrET;
    });
    fills.sort((a, b) => new Date(b.filled_at || b.created_at) - new Date(a.filled_at || a.created_at));
    res.json({
      ok: true,
      liveData: true,
      time: new Date().toISOString(),
      count: fills.length,
      orders: fills.map(o => ({
        id: o.id,
        symbol: o.symbol,
        side: o.side,
        qty: Number(o.qty || o.filled_qty || 0),
        filled_avg_price: Number(o.filled_avg_price || 0),
        filled_at: o.filled_at || o.created_at,
        type: o.type,
        status: o.status,
        notional: Number(o.qty || o.filled_qty || 0) * Number(o.filled_avg_price || 0)
      }))
    });
  } catch (e) {
    console.error('❌ /api/today-orders error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// --- Alpaca: orders with optional date range (?from=YYYY-MM-DD&to=YYYY-MM-DD) ---
app.get('/api/orders', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    const alpaca = getAlpaca();
    const { from, to } = req.query;

    // Validate date params to prevent injection
    const dateRe = /^\d{4}-\d{2}-\d{2}$/;
    const safeFrom = from && dateRe.test(from) ? from : null;
    const safeTo   = to   && dateRe.test(to)   ? to   : null;

    const params = { status: 'all', limit: 500, direction: 'desc' };
    if (safeFrom) params.after  = new Date(safeFrom + 'T00:00:00Z').toISOString();
    if (safeTo)   params.until  = new Date(safeTo   + 'T23:59:59Z').toISOString();

    const allOrders = await alpaca.alpaca.getOrders(params).catch(() => []);

    const fills = (Array.isArray(allOrders) ? allOrders : []).filter(o => {
      const s = String(o.status || '').toLowerCase();
      if (!['filled', 'partially_filled'].includes(s)) return false;
      if (!safeFrom && !safeTo) return true;
      const d = new Date(o.filled_at || o.created_at);
      if (safeFrom && d < new Date(safeFrom + 'T00:00:00Z')) return false;
      if (safeTo   && d > new Date(safeTo   + 'T23:59:59Z')) return false;
      return true;
    });

    fills.sort((a, b) => new Date(b.filled_at || b.created_at) - new Date(a.filled_at || a.created_at));

    res.json({
      ok: true,
      time: new Date().toISOString(),
      count: fills.length,
      from: safeFrom,
      to: safeTo,
      orders: fills.map(o => ({
        id: o.id,
        symbol: o.symbol,
        side: o.side,
        qty: Number(o.qty || o.filled_qty || 0),
        filled_avg_price: Number(o.filled_avg_price || 0),
        filled_at: o.filled_at || o.created_at,
        type: o.type,
        status: o.status,
        notional: Number(o.qty || o.filled_qty || 0) * Number(o.filled_avg_price || 0)
      }))
    });
  } catch (e) {
    console.error('❌ /api/orders error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// --- RENDER: service status + recent log lines ---
// Render API docs: https://api-docs.render.com/reference/introduction
async function renderApi(pathPart) {
  const key = process.env.RENDER_API_KEY;
  if (!key) throw new Error('RENDER_API_KEY not set');
  const fetchFn = (typeof fetch === 'function') ? fetch : (await import('node-fetch')).default;
  const url = `https://api.render.com/v1${pathPart}`;
  const r = await fetchFn(url, {
    headers: { Authorization: `Bearer ${key}`, Accept: 'application/json' }
  });
  if (!r.ok) {
    const body = await r.text().catch(() => '');
    throw new Error(`Render API ${r.status} ${r.statusText} — ${body.slice(0, 300)}`);
  }
  return r.json();
}

app.get('/api/render/status', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    const serviceId = process.env.RENDER_SERVICE_ID;
    if (!serviceId) throw new Error('RENDER_SERVICE_ID not set');
    const [service, deploys] = await Promise.all([
      renderApi(`/services/${serviceId}`).catch(e => ({ error: e.message })),
      renderApi(`/services/${serviceId}/deploys?limit=5`).catch(() => [])
    ]);
    const latestDeploy = Array.isArray(deploys) && deploys.length > 0
      ? (deploys[0].deploy || deploys[0])
      : null;
    res.json({
      ok: true,
      time: new Date().toISOString(),
      service: service && !service.error ? {
        id: service.id,
        name: service.name,
        type: service.type,
        suspended: service.suspended,
        url: service.serviceDetails?.url || null
      } : { error: service?.error || 'unknown' },
      latestDeploy: latestDeploy ? {
        id: latestDeploy.id,
        status: latestDeploy.status,
        createdAt: latestDeploy.createdAt,
        finishedAt: latestDeploy.finishedAt,
        commit: latestDeploy.commit?.message || null
      } : null
    });
  } catch (error) {
    console.error('❌ /api/render/status error:', error.message);
    res.status(500).json({ ok: false, error: error.message });
  }
});

// Cache the ownerId across calls — it never changes for a service
let _renderOwnerId = null;
async function getRenderOwnerId() {
  if (_renderOwnerId) return _renderOwnerId;
  const serviceId = process.env.RENDER_SERVICE_ID;
  if (!serviceId) throw new Error('RENDER_SERVICE_ID not set');
  const service = await renderApi(`/services/${serviceId}`);
  _renderOwnerId = service?.ownerId;
  if (!_renderOwnerId) throw new Error('Could not resolve Render ownerId from service');
  return _renderOwnerId;
}

app.get('/api/render/logs', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    // ?service=bot -> use RENDER_BOT_SERVICE_ID (cron job running the trading bot)
    // ?service=dashboard or default -> RENDER_SERVICE_ID (the web dashboard service)
    const wantBot = String(req.query.service || '').toLowerCase() === 'bot';
    const serviceId = wantBot
      ? (process.env.RENDER_BOT_SERVICE_ID || process.env.RENDER_SERVICE_ID)
      : process.env.RENDER_SERVICE_ID;
    if (!serviceId) throw new Error('RENDER_SERVICE_ID not set');
    const limit = Math.min(parseInt(req.query.limit) || 100, 100);
    const windowMinutes = Math.min(parseInt(req.query.windowMinutes) || 60, 1440);
    const ownerId = await getRenderOwnerId();
    const endTime = new Date().toISOString();
    const startTime = new Date(Date.now() - windowMinutes * 60 * 1000).toISOString();
    const qs = new URLSearchParams({
      ownerId,
      resource: serviceId,
      startTime,
      endTime,
      limit: String(limit),
      direction: 'backward'
    }).toString();
    const data = await renderApi(`/logs?${qs}`);
    const logs = Array.isArray(data?.logs) ? data.logs : [];
    res.json({
      ok: true,
      time: new Date().toISOString(),
      serviceId,
      serviceKind: wantBot ? 'bot' : 'dashboard',
      windowMinutes,
      hasMore: !!data?.hasMore,
      count: logs.length,
      logs: logs.map(l => ({
        timestamp: l.timestamp || null,
        message: l.message || '',
        level: (l.labels || []).find(x => x.name === 'level')?.value || null,
        type: (l.labels || []).find(x => x.name === 'type')?.value || null
      }))
    });
  } catch (error) {
    console.error('❌ /api/render/logs error:', error.message);
    res.status(500).json({ ok: false, error: error.message });
  }
});

// --- DISCORD: recent messages from configured channels via bot token ---
// Reuses DISCORD_BOT_TOKEN and DISCORD_LIVE_TRADES_CHANNEL_ID from .env
app.get('/api/discord/recent', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    const token = process.env.DISCORD_BOT_TOKEN;
    const channelId = req.query.channel || process.env.DISCORD_LIVE_TRADES_CHANNEL_ID;
    if (!token) throw new Error('DISCORD_BOT_TOKEN not set');
    if (!channelId) throw new Error('DISCORD_LIVE_TRADES_CHANNEL_ID not set and no ?channel= provided');
    const limit = Math.min(parseInt(req.query.limit) || 20, 100);
    const fetchFn = (typeof fetch === 'function') ? fetch : (await import('node-fetch')).default;
    const r = await fetchFn(`https://discord.com/api/v10/channels/${channelId}/messages?limit=${limit}`, {
      headers: { Authorization: `Bot ${token}`, Accept: 'application/json' }
    });
    if (!r.ok) {
      const body = await r.text().catch(() => '');
      throw new Error(`Discord API ${r.status} ${r.statusText} — ${body.slice(0, 200)}`);
    }
    const msgs = await r.json();
    res.json({
      ok: true,
      time: new Date().toISOString(),
      channelId,
      count: Array.isArray(msgs) ? msgs.length : 0,
      messages: (Array.isArray(msgs) ? msgs : []).map(m => ({
        id: m.id,
        timestamp: m.timestamp,
        author: m.author?.username || 'unknown',
        content: m.content || '',
        embeds: (m.embeds || []).map(e => ({
          title: e.title || null,
          description: e.description || null,
          color: e.color || null
        }))
      }))
    });
  } catch (error) {
    console.error('❌ /api/discord/recent error:', error.message);
    res.status(500).json({ ok: false, error: error.message });
  }
});

// --- BOT STATE: reads today's scan_decisions JSONL + scrapes Render logs ---
// Local scan_decisions logs only exist when bot runs locally. When bot runs on
// Render (your setup), we additionally derive recent decision events by scanning
// Render's log stream for known patterns the bot emits to stdout.
app.get('/api/bot/state', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    const fs = require('fs');
    const today = new Date().toISOString().slice(0, 10);
    const scanFile = path.join(__dirname, 'logs', 'scan_decisions', `${today}.jsonl`);
    let scanStats = { total: 0, accepted: 0, rejected: 0, lastTime: null, tail: [] };
    let localFileExists = false;
    let localAgeSec = null;
    if (fs.existsSync(scanFile)) {
      localFileExists = true;
      const stat = fs.statSync(scanFile);
      localAgeSec = Math.round((Date.now() - stat.mtimeMs) / 1000);
      const raw = fs.readFileSync(scanFile, 'utf8');
      const lines = raw.split('\n').filter(Boolean);
      scanStats.total = lines.length;
      const recent = lines.slice(-100).map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
      recent.forEach(d => {
        const decision = String(d.decision || d.result || '').toUpperCase();
        if (decision.includes('ACCEPT')) scanStats.accepted++;
        else if (decision.includes('REJECT') || decision.includes('SKIP')) scanStats.rejected++;
        if (d.timestamp || d.time) scanStats.lastTime = d.timestamp || d.time;
      });
      scanStats.tail = recent.slice(-10);
    }

    // ── Mine Render BOT service logs for live activity (last 60 min) ──
    // The bot emits patterns like:
    //   🕹️ [Position Manager] 4:00:42 PM ET - 1 position(s): SPY 741C @ 2.86 → 3.13 (+9.4% / +$135)
    //   [INDEX STREAM] 📡 SPY $741.31 | VWAP $739.48 (ABOVE) ...
    //   [StockStream] ⏭ AMD SKIPPED — stale setup (...)
    //   ✅ Alpaca: SPY call @ $741 (1d) = $3.13 (Bid: ..., Ask: ...)
    let renderEvents = [];
    let renderError = null;
    let botServiceUsed = null;
    try {
      // Prefer the dedicated bot cron-job service ID over the web dashboard service ID
      const botServiceId = process.env.RENDER_BOT_SERVICE_ID || process.env.RENDER_SERVICE_ID;
      botServiceUsed = botServiceId;
      if (process.env.RENDER_API_KEY && botServiceId) {
        const ownerId = await getRenderOwnerId();
        const endTime = new Date().toISOString();
        const startTime = new Date(Date.now() - 60 * 60 * 1000).toISOString();
        const qs = new URLSearchParams({
          ownerId,
          resource: botServiceId,
          startTime, endTime,
          limit: '100',
          direction: 'backward'
        }).toString();
        const data = await renderApi(`/logs?${qs}`);
        const logs = Array.isArray(data?.logs) ? data.logs : [];
        // Match lines that look like trade/scan events. Order matters (most specific first).
        const PATTERNS = [
          { re: /\[Position Manager\][^:]*:\s*\d+\s+position.*?(\w+)\s+(\d+)([CP])\s*@\s*([\d.]+)\s*→\s*([\d.]+)\s*\(([+\-][\d.]+)%\s*\/\s*([+\-]?\$?[\d.]+)\)/i, kind: 'POSITION' },
          { re: /\[StockStream\]\s*⏭\s*(\w+)\s+SKIPPED\s*—\s*(.+)/i, kind: 'STOCK_SKIP' },
          { re: /\[StockStream\]\s*(?:✅|🟢)\s*(\w+)\s+(ACCEPT|FIRED|SIGNAL)/i, kind: 'STOCK_ACCEPT' },
          { re: /\[INDEX STREAM\]\s*📡\s*(\w+)\s+\$([\d.]+).*VWAP\s+\$([\d.]+)\s+\((ABOVE|BELOW)\).*?Vol\s+([\d.]+)×/i, kind: 'INDEX_TICK' },
          { re: /(Entered|Exited|Closed|Opened)\s+(\w+)\s+(?:CALL|PUT|\d+[CP])/i, kind: 'TRADE' },
          { re: /✅\s*Alpaca:\s*(\w+)\s+(call|put)\s+@\s+\$(\d+).*?=\s*\$([\d.]+)/i, kind: 'PRICE_FETCH' },
          { re: /(?:🚨|⚠️)\s*(.*?(?:STOP|TARGET|EXIT).*)/i, kind: 'ALERT' },
          { re: /📊\s*(ACCEPTED|REJECTED)\s*[:\-]?\s*(\w+)/i, kind: 'DECISION' },
          { re: /(?:🚀|🟢|✅).*?(?:Entry|Opened|Bought)\s+(\w+)/i, kind: 'ENTRY' }
        ];
        renderEvents = logs.map(l => {
          const msg = l.message || '';
          for (const { re, kind } of PATTERNS) {
            const m = msg.match(re);
            if (m) return { timestamp: l.timestamp, kind, message: msg.slice(0, 240) };
          }
          return null;
        }).filter(Boolean).slice(0, 20);
      }
    } catch (e) {
      renderError = e.message;
    }

    // Market clock (ET)
    const nowET = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
    const day = nowET.getDay();
    const hours = nowET.getHours();
    const mins = nowET.getMinutes();
    const minsSinceOpen = (hours - 9) * 60 + (mins - 30);
    const minsToClose = (16 - hours) * 60 - mins;
    const isWeekday = day >= 1 && day <= 5;
    const marketOpen = isWeekday && minsSinceOpen >= 0 && minsToClose > 0;

    res.json({
      ok: true,
      time: new Date().toISOString(),
      market: {
        open: marketOpen,
        nowET: nowET.toISOString(),
        minsSinceOpen,
        minsToClose
      },
      localScans: {
        ...scanStats,
        fileExists: localFileExists,
        fileAgeSec: localAgeSec,
        // "stale" = file hasn't been written to in >5 min
        stale: !localFileExists || (localAgeSec !== null && localAgeSec > 300)
      },
      renderEvents: {
        count: renderEvents.length,
        events: renderEvents,
        error: renderError,
        serviceUsed: botServiceUsed
      },
      config: {
        indexOnly: process.env.INDEX_ONLY === 'true',
        screenerEnabled: process.env.SCREENER_ENABLED === 'true',
        staleSetupBars: Number(process.env.STALE_SETUP_BARS) || 60,
        staleSetupMovePct: Number(process.env.STALE_SETUP_MOVE_PCT) || 2.5,
        paperTrading: process.env.ALPACA_PAPER_TRADING === 'true'
      }
    });
  } catch (error) {
    console.error('❌ /api/bot/state error:', error.message);
    res.status(500).json({ ok: false, error: error.message });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// EXTRA ENDPOINTS for the new modern dashboard (`/new.html`)
// ─────────────────────────────────────────────────────────────────────────────

// --- EQUITY CURVE: REAL daily account equity history straight from Alpaca ---
// Source: Alpaca's /v2/account/portfolio/history endpoint (the truth — same numbers Alpaca shows).
// Per-day trade count + win rate are reconciled with real filled orders (sells with realized P&L > 0 = wins).
app.get('/api/equity-curve', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    const days = Math.min(parseInt(req.query.days) || 30, 365);
    const alpaca = getAlpaca();

    // 1) REAL equity history from Alpaca
    const history = await alpaca.getPortfolioHistory({
      period: `${days}D`,
      timeframe: '1D'
    });

    const timestamps = Array.isArray(history?.timestamp) ? history.timestamp : [];
    const equities = Array.isArray(history?.equity) ? history.equity : [];
    const pls = Array.isArray(history?.profit_loss) ? history.profit_loss : [];
    const baseValue = Number(history?.base_value) || (equities.length ? equities[0] : 0);

    // 2) REAL filled orders → per-day trade counts + wins (sells with positive realized P&L)
    // We pull recent filled orders and group by their fill date.
    const allOrders = await alpaca.getOrders('all').catch(() => []);
    // Build a quick lookup of average buy price per option symbol for win-rate inference
    // (sell with avg fill > most recent buy avg = winning close)
    const lastBuyPriceBySymbol = {};
    const ordersByDay = {}; // dayKey -> { trades, wins }
    // Sort oldest-first so we know each symbol's most-recent buy price before its sell
    const filledOrders = allOrders
      .filter(o => parseFloat(o.filled_qty) > 0 && o.filled_avg_price > 0)
      .sort((a, b) => new Date(a.created_at) - new Date(b.created_at));

    filledOrders.forEach(o => {
      const fillTime = o.filled_at || o.created_at;
      if (!fillTime) return;
      const dayKey = new Date(fillTime).toISOString().slice(0, 10);
      if (!ordersByDay[dayKey]) ordersByDay[dayKey] = { trades: 0, wins: 0 };
      ordersByDay[dayKey].trades++;

      const side = String(o.side || '').toLowerCase();
      const price = parseFloat(o.filled_avg_price) || 0;

      if (side === 'buy') {
        // Track latest buy avg for this contract symbol
        lastBuyPriceBySymbol[o.symbol] = price;
      } else if (side === 'sell') {
        // A profitable close = sell price > last known buy price for that symbol
        const lastBuy = lastBuyPriceBySymbol[o.symbol];
        if (lastBuy && price > lastBuy) {
          ordersByDay[dayKey].wins++;
        }
        // Clear so we don't double-count if we have layered partial closes
        delete lastBuyPriceBySymbol[o.symbol];
      }
    });

    // 3) Build the curve: one entry per Alpaca history bucket
    const curve = timestamps.map((ts, i) => {
      const date = new Date(ts * 1000).toISOString().slice(0, 10);
      const equity = Math.round((Number(equities[i]) || 0) * 100) / 100;
      const dailyPnL = Math.round((Number(pls[i]) || 0) * 100) / 100;
      const dayStats = ordersByDay[date] || { trades: 0, wins: 0 };
      return {
        date,
        equity,
        dailyPnL,
        trades: dayStats.trades,
        wins: dayStats.wins,
        winRate: dayStats.trades > 0 ? Math.round((dayStats.wins / dayStats.trades) * 1000) / 10 : 0
      };
    });

    const lastEquity = curve.length ? curve[curve.length - 1].equity : baseValue;
    res.json({
      ok: true,
      time: new Date().toISOString(),
      source: 'alpaca_portfolio_history',
      startingBalance: Math.round(baseValue * 100) / 100,
      days: curve.length,
      curve,
      summary: {
        peakEquity: curve.length ? Math.max(...curve.map(c => c.equity)) : baseValue,
        currentEquity: lastEquity,
        totalPnL: Math.round((lastEquity - baseValue) * 100) / 100,
        bestDay: curve.length ? Math.max(...curve.map(c => c.dailyPnL)) : 0,
        worstDay: curve.length ? Math.min(...curve.map(c => c.dailyPnL)) : 0,
        totalTrades: curve.reduce((sum, c) => sum + c.trades, 0),
        totalWins: curve.reduce((sum, c) => sum + c.wins, 0)
      }
    });
  } catch (error) {
    console.error('❌ /api/equity-curve error:', error.message);
    const sheetResult = await fetchTradesFromGoogleSheets();
    const closed = sheetResult.trades
      .filter(trade => String(trade.status || '').toUpperCase() === 'CLOSED' && trade.exitTime)
      .sort((a, b) => new Date(a.exitTime) - new Date(b.exitTime));
    const byDay = {};
    closed.forEach(trade => {
      const day = String(trade.exitTime).slice(0, 10);
      if (!byDay[day]) byDay[day] = { dailyPnL: 0, trades: 0, wins: 0 };
      const pnl = getTradeDollarPnl(trade);
      byDay[day].dailyPnL += pnl;
      byDay[day].trades += 1;
      if (pnl > 0) byDay[day].wins += 1;
    });
    const dates = Object.keys(byDay).sort().slice(-Math.min(parseInt(req.query.days) || 30, 365));
    let equity = 100000;
    const curve = dates.map(date => {
      const day = byDay[date];
      equity += day.dailyPnL;
      return { date, equity: Math.round(equity * 100) / 100, dailyPnL: Math.round(day.dailyPnL * 100) / 100, trades: day.trades, wins: day.wins, winRate: day.trades ? Math.round(day.wins / day.trades * 1000) / 10 : 0 };
    });
    res.json({ ok: true, time: new Date().toISOString(), source: 'google_sheets_trade_history', startingBalance: 100000, days: curve.length, curve });
  }
});

// --- MARKET PULSE: live quotes for index symbols (Alpaca Data API direct) ---
app.get('/api/market-pulse', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    const symbols = (req.query.symbols || 'SPY,QQQ,IWM,DIA,GLD').split(',').map(s => s.trim().toUpperCase());
    const keyId = process.env.ALPACA_API_KEY_ID || process.env.ALPACA_KEY_ID;
    const secret = process.env.ALPACA_SECRET_KEY;
    if (!keyId || !secret) throw new Error('Alpaca keys not set — set ALPACA_API_KEY_ID and ALPACA_SECRET_KEY');
    const fetchFn = (typeof fetch === 'function') ? fetch : (await import('node-fetch')).default;
    const headers = { 'APCA-API-KEY-ID': keyId, 'APCA-API-SECRET-KEY': secret, Accept: 'application/json' };
    // Batch snapshot endpoint: GET /v2/stocks/snapshots?symbols=SPY,QQQ,...
    const url = `https://data.alpaca.markets/v2/stocks/snapshots?symbols=${symbols.join(',')}`;
    const r = await fetchFn(url, { headers });
    if (!r.ok) {
      const body = await r.text().catch(() => '');
      throw new Error(`Alpaca data ${r.status} ${r.statusText} — ${body.slice(0, 200)}`);
    }
    const data = await r.json();
    const quotes = symbols.map(sym => {
      const snap = data?.[sym];
      if (!snap) return { symbol: sym, error: 'no_data' };
      const trade = snap.latestTrade || {};
      const daily = snap.dailyBar || {};
      const prevDaily = snap.prevDailyBar || {};
      const price = parseFloat(trade.p ?? daily.c ?? 0);
      const prevClose = parseFloat(prevDaily.c ?? 0);
      const change = prevClose > 0 ? price - prevClose : 0;
      const changePct = prevClose > 0 ? (change / prevClose) * 100 : 0;
      return {
        symbol: sym,
        price: Math.round(price * 100) / 100,
        prevClose: Math.round(prevClose * 100) / 100,
        change: Math.round(change * 100) / 100,
        changePct: Math.round(changePct * 100) / 100,
        high: parseFloat(daily.h ?? 0),
        low: parseFloat(daily.l ?? 0),
        volume: parseInt(daily.v ?? 0)
      };
    });
    res.json({ ok: true, time: new Date().toISOString(), quotes });
  } catch (error) {
    console.error('❌ /api/market-pulse error:', error.message);
    res.status(500).json({ ok: false, error: error.message });
  }
});

// --- ACTIVITY FEED: unified stream (Discord alerts + Render bot events) ---
app.get('/api/activity-feed', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    const limit = Math.min(parseInt(req.query.limit) || 30, 100);
    const events = [];

    // 1. Discord alerts
    try {
      const token = process.env.DISCORD_BOT_TOKEN;
      const channelId = process.env.DISCORD_LIVE_TRADES_CHANNEL_ID;
      if (token && channelId) {
        const fetchFn = (typeof fetch === 'function') ? fetch : (await import('node-fetch')).default;
        const r = await fetchFn(`https://discord.com/api/v10/channels/${channelId}/messages?limit=${Math.min(limit, 30)}`, {
          headers: { Authorization: `Bot ${token}`, Accept: 'application/json' }
        });
        if (r.ok) {
          const msgs = await r.json();
          (Array.isArray(msgs) ? msgs : []).forEach(m => {
            const embed = (m.embeds || [])[0] || {};
            events.push({
              source: 'discord',
              kind: (embed.title || 'ALERT').replace(/[^\w\s]/g, '').trim().split(' ')[0].toUpperCase(),
              timestamp: m.timestamp,
              title: embed.title || '',
              description: embed.description || m.content || '',
              author: m.author?.username || 'bot'
            });
          });
        }
      }
    } catch (e) { /* fall through silently */ }

    // 2. Render bot service log events
    try {
      const botServiceId = process.env.RENDER_BOT_SERVICE_ID;
      if (process.env.RENDER_API_KEY && botServiceId) {
        const ownerId = await getRenderOwnerId();
        const endTime = new Date().toISOString();
        const startTime = new Date(Date.now() - 60 * 60 * 1000).toISOString();
        const qs = new URLSearchParams({
          ownerId, resource: botServiceId, startTime, endTime,
          limit: '50', direction: 'backward'
        }).toString();
        const data = await renderApi(`/logs?${qs}`);
        const logs = Array.isArray(data?.logs) ? data.logs : [];
        // Only include "interesting" lines, not heartbeat health checks
        const INTERESTING = [
          { re: /\[Position Manager\]/, kind: 'POSITION' },
          { re: /Entered|Exited|Closed|Opened/i, kind: 'TRADE' },
          { re: /SKIPPED.*stale setup/i, kind: 'SKIP' },
          { re: /ACCEPTED|REJECTED/i, kind: 'DECISION' },
          { re: /🚨|⚠️.*(STOP|TARGET|EXIT)/i, kind: 'ALERT' },
          { re: /EOD|Daily Briefing|Morning/i, kind: 'BRIEFING' }
        ];
        logs.forEach(l => {
          const msg = l.message || '';
          for (const { re, kind } of INTERESTING) {
            if (re.test(msg)) {
              events.push({
                source: 'bot',
                kind,
                timestamp: l.timestamp,
                title: null,
                description: msg.slice(0, 240),
                author: 'bot'
              });
              break;
            }
          }
        });
      }
    } catch (e) { /* fall through silently */ }

    // Sort newest-first and trim
    events.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
    if (!events.length) {
      const sheetResult = await fetchTradesFromGoogleSheets();
      sheetResult.trades.filter(trade => trade.exitTime).slice(0, limit).forEach(trade => {
        events.push({ source: 'sheets', kind: 'TRADE', timestamp: trade.exitTime, title: trade.symbol, description: `${trade.symbol} ${trade.status || 'closed'} · ${trade.exitReason || 'trade update'}`, author: 'trade log' });
      });
    }
    res.json({ ok: true, time: new Date().toISOString(), count: events.length, events: events.slice(0, limit) });
  } catch (error) {
    console.error('❌ /api/activity-feed error:', error.message);
    res.status(500).json({ ok: false, error: error.message });
  }
});

// ─────────────────────────────────────────────────────────────────────────
// SCAN DECISIONS — live stream of bot scan decisions from JSONL log
// ─────────────────────────────────────────────────────────────────────────
app.get('/api/scan-decisions', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    const limit = Math.min(parseInt(req.query.limit) || 50, 500);
    const fs = require('fs');
    const path = require('path');
    const logsDir = path.join(__dirname, 'logs', 'scan_decisions');

    // Today in ET
    const nowET = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
    const todayStr = nowET.toISOString().slice(0, 10);
    const todayFile = path.join(logsDir, `${todayStr}.jsonl`);

    let lines = [];
    if (fs.existsSync(todayFile)) {
      lines = fs.readFileSync(todayFile, 'utf8').split('\n').filter(Boolean);
    } else {
      // Fall back to most recent file if today's not present yet
      try {
        const files = fs.readdirSync(logsDir).filter(f => f.endsWith('.jsonl')).sort().reverse();
        if (files[0]) lines = fs.readFileSync(path.join(logsDir, files[0]), 'utf8').split('\n').filter(Boolean);
      } catch (_) {}
    }

    const decisions = lines.slice(-limit).reverse().map(l => {
      try { return JSON.parse(l); } catch { return null; }
    }).filter(Boolean);

    // Quick stats from full file
    const allParsed = lines.map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
    const accepted = allParsed.filter(d => d.decision === 'ACCEPTED').length;
    const rejected = allParsed.filter(d => d.decision === 'REJECTED').length;

    if (!decisions.length) {
      const sheetResult = await fetchTradesFromGoogleSheets();
      const tradeDecisions = sheetResult.trades.filter(trade => trade.entryTime).slice(0, limit).map(trade => ({
        timestamp: trade.entryTime,
        symbol: trade.symbol,
        engine: trade.setupType || 'TRADE LOG',
        decision: String(trade.status || '').toUpperCase() === 'CLOSED' ? 'ACCEPTED' : 'OPEN',
        bias: String(trade.direction || '').toLowerCase().includes('put') ? 'bearish' : 'bullish',
        gateReason: trade.exitReason || 'Recorded trade'
      }));
      return res.json({ ok: true, time: new Date().toISOString(), total: tradeDecisions.length, accepted: tradeDecisions.filter(d => d.decision === 'ACCEPTED').length, rejected: 0, count: tradeDecisions.length, decisions: tradeDecisions, source: 'google_sheets_trade_history' });
    }

    res.json({
      ok: true,
      time: new Date().toISOString(),
      total: allParsed.length,
      accepted,
      rejected,
      count: decisions.length,
      decisions
    });
  } catch (e) {
    console.error('❌ /api/scan-decisions error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// ─────────────────────────────────────────────────────────────────────────
// STRATEGY STATS — aggregate scan decisions by engine + reject reasons
// ─────────────────────────────────────────────────────────────────────────
app.get('/api/strategy-stats', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    const days = Math.min(parseInt(req.query.days) || 7, 30);
    const fs = require('fs');
    const path = require('path');
    const logsDir = path.join(__dirname, 'logs', 'scan_decisions');

    let allDecisions = [];
    if (fs.existsSync(logsDir)) {
      const files = fs.readdirSync(logsDir)
        .filter(f => f.endsWith('.jsonl'))
        .sort()
        .reverse()
        .slice(0, days);
      for (const f of files) {
        try {
          const lines = fs.readFileSync(path.join(logsDir, f), 'utf8').split('\n').filter(Boolean);
          for (const l of lines) {
            try { allDecisions.push(JSON.parse(l)); } catch { /* skip */ }
          }
        } catch (_) {}
      }
    }

    // Group by engine
    const byEngine = {};
    const rejectReasons = {};
    for (const d of allDecisions) {
      const eng = d.engine || 'UNKNOWN';
      if (!byEngine[eng]) byEngine[eng] = { engine: eng, total: 0, accepted: 0, rejected: 0, acceptRate: 0 };
      byEngine[eng].total++;
      if (d.decision === 'ACCEPTED') byEngine[eng].accepted++;
      else if (d.decision === 'REJECTED') {
        byEngine[eng].rejected++;
        const r = d.gateReason || 'unknown';
        rejectReasons[r] = (rejectReasons[r] || 0) + 1;
      }
    }
    Object.values(byEngine).forEach(e => {
      e.acceptRate = e.total > 0 ? Math.round((e.accepted / e.total) * 100) : 0;
    });

    // Top reject reasons
    const topReasons = Object.entries(rejectReasons)
      .map(([reason, count]) => ({ reason, count }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 10);

    if (!allDecisions.length) {
      const sheetResult = await fetchTradesFromGoogleSheets();
      const grouped = {};
      sheetResult.trades.forEach(trade => {
        const engine = trade.setupType || 'UNKNOWN';
        if (!grouped[engine]) grouped[engine] = { engine, total: 0, accepted: 0, rejected: 0, acceptRate: 0 };
        grouped[engine].total += 1;
        if (String(trade.status || '').toUpperCase() === 'CLOSED') grouped[engine].accepted += 1;
      });
      Object.values(grouped).forEach(engine => { engine.acceptRate = engine.total ? Math.round(engine.accepted / engine.total * 100) : 0; });
      return res.json({ ok: true, time: new Date().toISOString(), daysScanned: days, totalDecisions: sheetResult.trades.length, engines: Object.values(grouped), topRejectReasons: [], source: 'google_sheets_trade_history' });
    }

    res.json({
      ok: true,
      time: new Date().toISOString(),
      daysScanned: days,
      totalDecisions: allDecisions.length,
      engines: Object.values(byEngine).sort((a, b) => b.total - a.total),
      topRejectReasons: topReasons
    });
  } catch (e) {
    console.error('❌ /api/strategy-stats error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// ─────────────────────────────────────────────────────────────────────────
// SERVICES HEALTH — parallel ping of all external services with timeout
// ─────────────────────────────────────────────────────────────────────────
async function pingWithTimeout(name, fn, timeoutMs = 4000) {
  const t0 = Date.now();
  try {
    const result = await Promise.race([
      fn(),
      new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), timeoutMs))
    ]);
    return { name, ok: true, latencyMs: Date.now() - t0, detail: result || null };
  } catch (e) {
    return { name, ok: false, latencyMs: Date.now() - t0, error: e.message };
  }
}

app.get('/api/services/health', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  try {
    const fetchFn = (typeof fetch === 'function') ? fetch : (await import('node-fetch')).default;

    const checks = await Promise.all([
      // Alpaca
      pingWithTimeout('Alpaca', async () => {
        const acc = await getAlpaca().getAccount();
        return acc?.status || 'ok';
      }),
      // Render dashboard
      pingWithTimeout('Render (Dashboard)', async () => {
        if (!process.env.RENDER_API_KEY || !process.env.RENDER_SERVICE_ID) throw new Error('no key');
        const s = await renderApi(`/services/${process.env.RENDER_SERVICE_ID}`);
        return s.suspended === 'not_suspended' ? 'live' : (s.suspended || 'unknown');
      }),
      // Render bot
      pingWithTimeout('Render (Bot)', async () => {
        if (!process.env.RENDER_API_KEY || !process.env.RENDER_BOT_SERVICE_ID) throw new Error('no key');
        const s = await renderApi(`/services/${process.env.RENDER_BOT_SERVICE_ID}`);
        return s.suspended === 'not_suspended' ? 'live' : (s.suspended || 'unknown');
      }),
      // Discord (verify bot token)
      pingWithTimeout('Discord', async () => {
        if (!process.env.DISCORD_BOT_TOKEN) throw new Error('no token');
        const r = await fetchFn('https://discord.com/api/v10/users/@me', {
          headers: { Authorization: `Bot ${process.env.DISCORD_BOT_TOKEN}` }
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const me = await r.json();
        return me.username || 'ok';
      }),
      // Polygon
      pingWithTimeout('Polygon', async () => {
        if (!process.env.POLYGON_API_KEY) throw new Error('no key');
        const r = await fetchFn(`https://api.polygon.io/v1/marketstatus/now?apiKey=${process.env.POLYGON_API_KEY}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        return d.market || 'ok';
      }),
      // Finnhub
      pingWithTimeout('Finnhub', async () => {
        if (!process.env.FINNHUB_API_KEY) throw new Error('no key');
        const r = await fetchFn(`https://finnhub.io/api/v1/quote?symbol=SPY&token=${process.env.FINNHUB_API_KEY}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        return d.c ? `SPY ${d.c}` : 'ok';
      }),
      // Twelve Data
      pingWithTimeout('TwelveData', async () => {
        if (!process.env.TWELVE_DATA_API_KEY) throw new Error('no key');
        const r = await fetchFn(`https://api.twelvedata.com/quote?symbol=SPY&apikey=${process.env.TWELVE_DATA_API_KEY}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        if (d.code && d.code !== 200) throw new Error(d.message || 'api error');
        return d.symbol || 'ok';
      }),
      // Google Sheets
      pingWithTimeout('Google Sheets', async () => {
        if (!process.env.GOOGLE_SHEET_ID) throw new Error('no sheet');
        try {
          const svc = require('./google_sheets_service');
          if (typeof svc.initialize === 'function') {
            const ok = await svc.initialize();
            if (!ok) throw new Error('init failed');
            return 'connected';
          }
          return 'configured';
        } catch (e) { throw new Error(e.message); }
      })
    ]);

    const allOk = checks.every(c => c.ok);
    res.json({
      ok: true,
      time: new Date().toISOString(),
      overall: allOk ? 'healthy' : 'degraded',
      okCount: checks.filter(c => c.ok).length,
      totalCount: checks.length,
      services: checks
    });
  } catch (e) {
    console.error('❌ /api/services/health error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// ─────────────────────────────────────────────────────────────────────────
// MANUAL BOT CONTROLS — close all positions / cancel all orders
// ─────────────────────────────────────────────────────────────────────────
app.post('/api/bot/close-all', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  if (!DASHBOARD_ENABLE_CONTROLS) {
    return res.status(403).json({ ok: false, error: 'Dashboard controls are disabled' });
  }
  try {
    const alpaca = getAlpaca();
    const positions = await alpaca.getPositions().catch(() => []);
    const results = [];
    for (const p of positions) {
      try {
        // For options use closeOptionsPosition if available, else market sell
        if (typeof alpaca.closeOptionsPosition === 'function' && /^[A-Z]+\d+/.test(p.symbol)) {
          const r = await alpaca.closeOptionsPosition({
            symbol: p.symbol,
            qty: p.qty,
            side: p.side === 'long' ? 'sell' : 'buy'
          });
          results.push({ symbol: p.symbol, ok: true, orderId: r?.id || null, kind: 'options' });
        } else {
          // Fallback: place market sell via raw SDK
          const order = await alpaca.placeOrder({
            symbol: p.symbol,
            qty: Math.abs(Number(p.qty)),
            side: p.side === 'long' ? 'sell' : 'buy',
            type: 'market',
            time_in_force: 'day'
          });
          results.push({ symbol: p.symbol, ok: true, orderId: order?.id || null, kind: 'market' });
        }
      } catch (e) {
        results.push({ symbol: p.symbol, ok: false, error: e.message });
      }
    }
    res.json({
      ok: true,
      time: new Date().toISOString(),
      closed: results.filter(r => r.ok).length,
      failed: results.filter(r => !r.ok).length,
      total: results.length,
      results
    });
  } catch (e) {
    console.error('❌ /api/bot/close-all error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/api/bot/cancel-orders', async (req, res) => {
  res.set({ 'Cache-Control': 'no-cache, no-store, must-revalidate' });
  if (!DASHBOARD_ENABLE_CONTROLS) {
    return res.status(403).json({ ok: false, error: 'Dashboard controls are disabled' });
  }
  try {
    const alpaca = getAlpaca();
    const openOrders = await alpaca.getOrders('open').catch(() => []);
    const results = [];
    for (const o of openOrders) {
      try {
        await alpaca.cancelOrder(o.id);
        results.push({ id: o.id, symbol: o.symbol, ok: true });
      } catch (e) {
        results.push({ id: o.id, symbol: o.symbol, ok: false, error: e.message });
      }
    }
    res.json({
      ok: true,
      time: new Date().toISOString(),
      cancelled: results.filter(r => r.ok).length,
      failed: results.filter(r => !r.ok).length,
      total: results.length,
      results
    });
  } catch (e) {
    console.error('❌ /api/bot/cancel-orders error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// Stub endpoint for /api/ai/progress to prevent 404 errors
app.get('/api/ai/progress', (req, res) => {
  res.json({ status: 'ok', aiMode: 'ADAPTIVE', marketCondition: 'NEUTRAL', systemHealth: 'healthy', lastUpdate: new Date(), performance: {}, learning: {}, parameters: {} });
});

// Stub endpoint for /api/ai/timeline to prevent 404 errors
app.get('/api/ai/timeline', (req, res) => {
  res.json([
    { date: '2025-08-20', winRate: 65 },
    { date: '2025-08-21', winRate: 70 },
    { date: '2025-08-22', winRate: 68 },
    { date: '2025-08-23', winRate: 72 }
  ]);
});

app.listen(PORT, () => {
  console.log(`Dashboard server running on http://localhost:${PORT}`);
});
