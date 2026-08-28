require('dotenv').config({ quiet: true });
const Alpaca = require('@alpacahq/alpaca-trade-api');

const ALPACA_DEBUG = String(process.env.ALPACA_DEBUG || process.env.DEBUG_ALPACA || '').trim().toLowerCase() === 'true';

class AlpacaService {
  constructor() {
    this.alpaca = new Alpaca({
      keyId: process.env.ALPACA_API_KEY_ID || process.env.ALPACA_KEY_ID,
      secretKey: process.env.ALPACA_SECRET_KEY,
      paper: true, // Always use paper trading for safety
      usePolygon: false
    });

    // Options trading uses the same Orders API as equities.
    // You place an order with the OCC option symbol via /v2/orders.
    console.log('🔗 Alpaca Paper Trading initialized (equities + options via /v2/orders)');
  }

  _isOccOptionSymbol(symbol) {
    // Root(1-6) + YYMMDD + C/P + strike (8 digits, 3 decimal implied)
    return /^[A-Z]{1,6}\d{6}[CP]\d{8}$/.test(String(symbol || '').toUpperCase());
  }

  async getAccount() {
    try {
      const account = await this.alpaca.getAccount();
      return {
        cash: parseFloat(account.cash),
        buying_power: parseFloat(account.buying_power),
        equity: parseFloat(account.equity),
        last_equity: parseFloat(account.last_equity),
        portfolio_value: parseFloat(account.portfolio_value),
        daytrade_count: parseInt(account.daytrade_count) || 0,
        status: account.status
      };
    } catch (error) {
      console.error('❌ Error fetching account:', error.message);
      throw error;
    }
  }

  async getPositions() {
    try {
      const positions = await this.alpaca.getPositions();
      return positions.map(pos => ({
        symbol: pos.symbol,
        qty: parseFloat(pos.qty),
        market_value: parseFloat(pos.market_value),
        cost_basis: parseFloat(pos.cost_basis),
        unrealized_pl: parseFloat(pos.unrealized_pl),
        unrealized_plpc: parseFloat(pos.unrealized_plpc) * 100,
        side: pos.side
      }));
    } catch (error) {
      console.error('❌ Error fetching positions:', error.message);
      return [];
    }
  }

  async getOrders(status = 'all') {
    try {
      const orders = await this.alpaca.getOrders({ status });
      return orders.map(order => ({
        id: order.id,
        symbol: order.symbol,
        qty: parseFloat(order.qty),
        side: order.side,
        type: order.type,
        status: order.status,
        filled_qty: parseFloat(order.filled_qty || 0),
        filled_avg_price: parseFloat(order.filled_avg_price || 0), // Actual fill price
        created_at: order.created_at
      }));
    } catch (error) {
      console.error('❌ Error fetching orders:', error.message);
      return [];
    }
  }

  // Options orders live in the same Orders API list; filter by OCC option symbol shape.
  async getOptionsOrders(status = 'all', limit = 50) {
    try {
      const params = { status };
      if (limit) params.limit = limit;
      const orders = await this.alpaca.getOrders(params);
      return orders
        .filter(o => this._isOccOptionSymbol(o.symbol) || String(o.asset_class || '').toLowerCase() === 'us_option')
        .map(order => ({
          id: order.id,
          symbol: order.symbol,
          qty: parseFloat(order.qty),
          side: order.side,
          type: order.type,
          status: order.status,
          filled_qty: parseFloat(order.filled_qty || 0),
          filled_avg_price: parseFloat(order.filled_avg_price || 0),
          created_at: order.created_at
        }));
    } catch (error) {
      console.error('❌ Error fetching options orders:', error.message);
      return [];
    }
  }

  async placeOrder(orderData) {
    try {
      // Debug: Log the received orderData
      console.log(`[DEBUG] placeOrder received:`, JSON.stringify(orderData, null, 2));
      
      // Handle both object and individual parameters for backward compatibility
      let symbol, quantity, side, orderType, timeInForce;
      
      if (typeof orderData === 'object' && orderData.symbol) {
        // New object format
        symbol = orderData.symbol;
        quantity = orderData.qty;
        side = orderData.side;
        orderType = orderData.type || 'market';
        timeInForce = orderData.time_in_force || 'gtc';
      } else {
        // Legacy individual parameters
        symbol = orderData;
        quantity = arguments[1];
        side = arguments[2] || 'buy';
        orderType = arguments[3] || 'market';
        timeInForce = 'gtc';
      }

      // Debug: Log the parsed values
      console.log(`[DEBUG] Parsed values: symbol=${symbol}, qty=${quantity}, side=${side}, type=${orderType}`);
      
      // Validation
      if (!symbol || typeof symbol !== 'string') {
        throw new Error(`Invalid symbol: ${symbol} (type: ${typeof symbol})`);
      }
      if (!quantity || isNaN(quantity)) {
        throw new Error(`Invalid quantity: ${quantity}`);
      }

      const order = await this.alpaca.createOrder({
        symbol: symbol.toUpperCase(),
        qty: Math.abs(quantity),
        side: side,
        type: orderType,
        time_in_force: timeInForce
      });

      console.log(`📈 ${side.toUpperCase()} order placed: ${quantity} shares of ${symbol}`);
      console.log(`   Order ID: ${order.id}, Status: ${order.status}`);
      
      return {
        id: order.id,
        symbol: order.symbol,
        qty: parseFloat(order.qty),
        side: order.side,
        type: order.type,
        status: order.status,
        filled_qty: parseFloat(order.filled_qty || 0),
        created_at: order.created_at
      };
    } catch (error) {
      console.error(`❌ Error placing order:`, error.message);
      throw error;
    }
  }

  // Options trading uses the standard Orders API (/v2/orders) with an OCC option symbol.
  async placeOptionsOrder(optionsOrderData) {
    try {
      if (ALPACA_DEBUG) {
        console.log(`[DEBUG] placeOptionsOrder received:`, JSON.stringify(optionsOrderData, null, 2));
      }
      
      const {
        symbol,           // Underlying symbol (e.g., 'AAPL')
        strike,           // Strike price (e.g., 230)
        expiration,       // Expiration date (e.g., '2025-09-12')
        direction,        // 'call' or 'put'
        side,            // 'buy' or 'sell'
        quantity,        // Number of contracts
        orderType = 'market',
        timeInForce = 'day'
      } = optionsOrderData;

      // Construct options symbol (Alpaca OCC format)
      // Format: SYMBOL + YYMMDD + C/P + STRIKE (8 digits with padding)
      const expDate = new Date(expiration + 'T00:00:00.000Z'); // Force UTC to avoid timezone issues
      const year = expDate.getUTCFullYear().toString().slice(-2);
      const month = String(expDate.getUTCMonth() + 1).padStart(2, '0');
      const day = String(expDate.getUTCDate()).padStart(2, '0');
      const callPut = direction.toLowerCase() === 'call' ? 'C' : 'P';
      
      // Strike formatting: pad to 8 digits (5 before decimal, 3 after)
      const strikeFormatted = String(Math.round(strike * 1000)).padStart(8, '0');
      
      const optionsSymbol = `${symbol.toUpperCase()}${year}${month}${day}${callPut}${strikeFormatted}`;

      console.log(`📈 [ALPACA] OPTIONS ${side.toUpperCase()} ${symbol.toUpperCase()} ${strike}${callPut} exp ${expiration} x${quantity} (OCC ${optionsSymbol})`);

      if (ALPACA_DEBUG) {
        console.log(`📊 [OPTIONS] Date components: ${year}-${month}-${day} from input ${expiration}`);
      }

      // Limit-order support: if caller passes limitPrice, prefer limit over market.
      // This reduces slippage on options entries — we pay mid+buffer instead of full ask.
      const resolvedType = (orderType === 'limit' || (typeof optionsOrderData.limitPrice === 'number' && optionsOrderData.limitPrice > 0))
        ? 'limit'
        : (orderType || 'market');

      const orderPayload = {
        symbol: optionsSymbol,
        qty: Math.max(1, Math.round(Math.abs(quantity))),
        side,
        type: resolvedType,
        time_in_force: timeInForce
      };

      if (resolvedType === 'limit' && typeof optionsOrderData.limitPrice === 'number' && optionsOrderData.limitPrice > 0) {
        orderPayload.limit_price = Math.round(optionsOrderData.limitPrice * 100) / 100;
      }

      if (ALPACA_DEBUG) {
        console.log(`📊 [OPTIONS] Order payload:`, JSON.stringify(orderPayload, null, 2));
      }

      let order;
      try {
        order = await this.alpaca.createOrder(orderPayload);
      } catch (firstErr) {
        // 403 wash trade: a stale GTC sell order (e.g. an old broker stop) exists for this
        // contract series and its limit price is below our new buy limit.
        // Alpaca embeds the blocking order ID in the JSON error body — cancel it and retry once.
        const errBody = firstErr?.error || firstErr?.response?.body || {};
        const errMsg  = typeof errBody === 'string' ? errBody : JSON.stringify(errBody);
        const blockingId = errBody?.existing_order_id
          || (errMsg.match(/"existing_order_id":"([^"]+)"/) || [])[1]
          || null;

        if ((firstErr?.statusCode === 403 || String(firstErr?.message || '').includes('403')) &&
            String(firstErr?.message || errMsg).includes('wash trade') && blockingId) {
          console.warn(`   ⚠️ Wash trade block — cancelling stale order ${blockingId} and retrying...`);
          try {
            await this.alpaca.cancelOrder(blockingId);
            await new Promise(r => setTimeout(r, 500)); // brief settle
          } catch (cancelErr) {
            console.warn(`   ⚠️ Could not cancel blocking order ${blockingId}: ${cancelErr.message}`);
          }
          order = await this.alpaca.createOrder(orderPayload); // retry once
          console.log(`   ✅ Retry succeeded after clearing stale stop order`);
        } else {
          throw firstErr; // not a wash trade — rethrow
        }
      }

      console.log(`✅ [ALPACA] OPTIONS order placed: ${order.id} (${order.status})`);
      
      return {
        id: order.id,
        symbol: order.symbol,
        underlying: symbol,
        strike: strike,
        expiration: expiration,
        direction: direction,
        qty: parseFloat(order.qty),
        side: order.side,
        type: order.type,
        status: order.status,
        filled_qty: parseFloat(order.filled_qty || 0),
        created_at: order.created_at
      };
    } catch (error) {
      const safe = {
        statusCode: error?.statusCode ?? null,
        code: error?.error?.code ?? error?.response?.body?.code ?? null,
        message: error?.error?.message ?? error?.response?.body?.message ?? error?.message ?? 'Unknown Alpaca error',
        requestId: error?.response?.headers?.['x-request-id'] ?? null,
        symbol: optionsOrderData?.symbol ?? null,
        expiration: optionsOrderData?.expiration ?? null
      };
      console.error(`❌ [OPTIONS] Error placing options order: ${safe.statusCode || ''} - ${safe.message}${safe.requestId ? ` (requestId=${safe.requestId})` : ''}`.trim());
      throw error;
    }
  }

  // Close options position by submitting the opposite-side order via /v2/orders.
  async closeOptionsPosition(orderData) {
    try {
      const {
        originalOrderId,
        symbol,
        strike,
        expiration,
        direction,
        quantity: requestedQuantity
      } = orderData;

      const reqQty = (requestedQuantity === undefined || requestedQuantity === null) ? 'n/a' : requestedQuantity;
      console.log(`📤 [OPTIONS] Closing options position: ${symbol} ${strike}${direction.toUpperCase()} requested x${reqQty}`);

      // Get all positions to find the options position
      const positions = await this.alpaca.getPositions();
      
      // Look for the options position
      const expDate = new Date(expiration + 'T00:00:00.000Z'); // Force UTC to avoid timezone issues
      const year = expDate.getUTCFullYear().toString().slice(-2);
      const month = String(expDate.getUTCMonth() + 1).padStart(2, '0');
      const day = String(expDate.getUTCDate()).padStart(2, '0');
      const callPut = direction.toLowerCase() === 'call' ? 'C' : 'P';
      const strikeFormatted = String(Math.round(strike * 1000)).padStart(8, '0');
      const optionsSymbol = `${symbol.toUpperCase()}${year}${month}${day}${callPut}${strikeFormatted}`;

      const optionsPosition = positions.find(pos => pos.symbol === optionsSymbol);
      
      if (!optionsPosition) {
        // Position already gone at broker — treat as already closed
        console.log(`ℹ️ [OPTIONS] No broker position found for ${optionsSymbol} — already closed or expired`);
        return { id: 'already_closed', symbol: optionsSymbol, status: 'already_closed' };
      }

      // Cancel any open stop/stop_limit orders for this symbol before submitting market close
      try {
        const openOrders = await this.alpaca.getOrders({ status: 'open', limit: 100 });
        const blockingOrders = openOrders.filter(o => o.symbol === optionsSymbol);
        if (blockingOrders.length > 0) {
          console.log(`🧹 [OPTIONS] Cancelling ${blockingOrders.length} open order(s) for ${optionsSymbol} before close...`);
          await Promise.all(blockingOrders.map(o => this.alpaca.cancelOrder(o.id).catch(() => {})));
          await new Promise(r => setTimeout(r, 500)); // brief settle
        }
      } catch (cancelErr) {
        console.warn(`   ⚠️ Could not cancel open orders for ${optionsSymbol}: ${cancelErr.message}`);
      }

      // Close the options position using correct endpoint
        const closeSide = parseFloat(optionsPosition.qty) > 0 ? 'sell' : 'buy';
        const closeQty = Math.abs(parseFloat(optionsPosition.qty));

        console.log(`📥 [OPTIONS] Broker position: ${optionsSymbol} qty=${optionsPosition.qty} -> sending ${closeSide.toUpperCase()} x${Math.max(1, Math.round(closeQty))}`);

        const orderPayload = {
          symbol: optionsSymbol,
          qty: Math.max(1, Math.round(closeQty)),
          side: closeSide,
          type: 'market',
          time_in_force: 'day',
          position_intent: closeSide === 'sell' ? 'sell_to_close' : 'buy_to_close'
        };

        const closeOrder = await this.alpaca.createOrder(orderPayload);

        console.log(`✅ [OPTIONS] Options position closed: ${closeOrder.id} - ${optionsSymbol}`);
        return closeOrder;
    } catch (error) {
      console.error(`❌ [OPTIONS] Error closing position:`, error.message);
      throw error;
    }
  }

  // Get current options position qty (absolute contracts) by OCC symbol.
  // Useful when Sheets rows are missing Contracts/Remaining (e.g., older trades).
  async getOptionsPositionQty(orderData) {
    try {
      const { symbol, strike, expiration, direction } = orderData || {};
      if (!symbol || !strike || !expiration || !direction) return null;

      const positions = await this.alpaca.getPositions();

      const expDate = new Date(expiration + 'T00:00:00.000Z');
      const year = expDate.getUTCFullYear().toString().slice(-2);
      const month = String(expDate.getUTCMonth() + 1).padStart(2, '0');
      const day = String(expDate.getUTCDate()).padStart(2, '0');
      const callPut = String(direction).toLowerCase() === 'call' ? 'C' : 'P';
      const strikeFormatted = String(Math.round(Number(strike) * 1000)).padStart(8, '0');
      const optionsSymbol = `${String(symbol).toUpperCase()}${year}${month}${day}${callPut}${strikeFormatted}`;

      const optionsPosition = positions.find(pos => pos.symbol === optionsSymbol);
      if (!optionsPosition) return null;

      const qty = Math.abs(parseFloat(optionsPosition.qty));
      return Number.isFinite(qty) && qty > 0 ? qty : null;
    } catch {
      return null;
    }
  }

  // Cancel an open order by ID. Safe to call on already-filled orders (Alpaca returns 422 which we swallow).
  async cancelOrder(orderId) {
    try {
      await this.alpaca.cancelOrder(orderId);
      console.log(`🚫 [ALPACA] Order ${orderId} cancelled`);
      return true;
    } catch (e) {
      // 422 = order already filled/cancelled — not an error condition
      if (e?.statusCode !== 422) {
        console.warn(`⚠️ [ALPACA] Cancel order ${orderId} failed: ${e.message}`);
      }
      return false;
    }
  }

  // Place a broker-side stop-limit SELL order as protection after entry fill.
  // stop_price = trigger level; limit_price = 3% below trigger (guarantees fill in fast moves).
  async placeOptionsBrokerStop({ symbol, strike, expiration, direction, quantity, stopPrice, limitPrice }) {
    if (!symbol || !strike || !expiration || !direction || !quantity || !stopPrice) {
      throw new Error('placeOptionsBrokerStop: missing required fields');
    }
    const expDate = new Date(expiration + 'T00:00:00.000Z');
    const year  = expDate.getUTCFullYear().toString().slice(-2);
    const month = String(expDate.getUTCMonth() + 1).padStart(2, '0');
    const day   = String(expDate.getUTCDate()).padStart(2, '0');
    const cp    = String(direction).toLowerCase() === 'call' ? 'C' : 'P';
    const strikeFmt = String(Math.round(Number(strike) * 1000)).padStart(8, '0');
    const occSymbol = `${String(symbol).toUpperCase()}${year}${month}${day}${cp}${strikeFmt}`;

    const resolvedLimit = limitPrice ?? Math.round(stopPrice * 0.97 * 100) / 100;

    console.log(`🛡️ [ALPACA] Placing broker stop-limit: ${occSymbol} stop=${stopPrice.toFixed(2)} limit=${resolvedLimit.toFixed(2)} x${quantity}`);
    const order = await this.alpaca.createOrder({
      symbol: occSymbol,
      qty: Math.max(1, Math.round(Math.abs(quantity))),
      side: 'sell',
      type: 'stop_limit',
      stop_price: Math.round(stopPrice * 100) / 100,
      limit_price: Math.round(resolvedLimit * 100) / 100,
      time_in_force: 'gtc',
      position_intent: 'sell_to_close'
    });
    console.log(`✅ [ALPACA] Broker stop placed: ${order.id} (${order.status})`);
    return { id: order.id, status: order.status };
  }

  // NEW: Get detailed order information including fill price
  async getOrderDetails(orderId) {
    try {
      const order = await this.alpaca.getOrder(orderId);
      return {
        id: order.id,
        symbol: order.symbol,
        qty: parseFloat(order.qty),
        side: order.side,
        type: order.type,
        status: order.status,
        filled_qty: parseFloat(order.filled_qty || 0),
        filled_avg_price: parseFloat(order.filled_avg_price || 0), // This is the actual fill price
        created_at: order.created_at,
        filled_at: order.filled_at,
        updated_at: order.updated_at
      };
    } catch (error) {
      console.error(`❌ Error fetching order ${orderId}:`, error.message);
      throw error;
    }
  }

  async getMarketStatus() {
    try {
      const clock = await this.alpaca.getClock();
      return {
        is_open: clock.is_open,
        next_open: clock.next_open,
        next_close: clock.next_close,
        timestamp: clock.timestamp
      };
    } catch (error) {
      console.error('❌ Error fetching market status:', error.message);
      return { is_open: false };
    }
  }

  // Pulls REAL account equity history straight from Alpaca's portfolio-history endpoint.
  // params: { period: '30D', timeframe: '1D', date_end?: 'YYYY-MM-DD', extended_hours?: bool }
  // Returns the raw shape: { timestamp[], equity[], profit_loss[], profit_loss_pct[], base_value, timeframe }
  async getPortfolioHistory(params = {}) {
    try {
      const opts = {
        period: params.period || '30D',
        timeframe: params.timeframe || '1D',
        extended_hours: params.extended_hours ?? false
      };
      if (params.date_end) opts.date_end = params.date_end;
      const history = await this.alpaca.getPortfolioHistory(opts);
      return history;
    } catch (error) {
      console.error('❌ Error fetching portfolio history:', error.message);
      throw error;
    }
  }
}

module.exports = AlpacaService;
