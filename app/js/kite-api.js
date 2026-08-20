/**
 * TradeSignal — Kite Connect API Integration
 * Handles REST API calls and WebSocket live ticks via backend proxy
 */
class KiteAPI {
  constructor() {
    this.apiKey = '';
    this.accessToken = '';
    this.backendUrl = window.location.origin;
    this.ws = null;
    this.connected = false;
    this.instruments = [];
    this.fnoInstruments = [];
    this.tickCallbacks = [];
    this.lastTicks = {};
    this.requestTimeoutMs = 15000; // Prevent UI hangs on slow/unresponsive API
  }

  configure(apiKey, accessToken, backendUrl) {
    this.apiKey = apiKey;
    this.accessToken = accessToken;
    if (backendUrl) this.backendUrl = backendUrl;
  }

  _authHeaders(extra = {}) {
    return {
      ...extra,
      'X-Kite-Api-Key': this.apiKey || '',
      'X-Kite-Access-Token': this.accessToken || ''
    };
  }

  // ── REST helpers ──
  async _get(endpoint, params = {}) {
    const url = new URL(`${this.backendUrl}${endpoint}`);
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    try {
      const res = await fetch(url.toString(), {
        headers: this._authHeaders(),
        credentials: 'include',
        signal: controller.signal
      });
      if (!res.ok) throw new Error(`API Error ${res.status}: ${await res.text()}`);
      return res.json();
    } catch (e) {
      if (e?.name === 'AbortError') {
        throw new Error(`Request timed out after ${this.requestTimeoutMs}ms`);
      }
      throw e;
    } finally {
      clearTimeout(timeoutId);
    }
  }

  async _post(endpoint, body = {}) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    try {
      const res = await fetch(`${this.backendUrl}${endpoint}`, {
        method: 'POST',
        headers: this._authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
        credentials: 'include',
        signal: controller.signal
      });
      if (!res.ok) throw new Error(`API Error ${res.status}: ${await res.text()}`);
      return res.json();
    } catch (e) {
      if (e?.name === 'AbortError') {
        throw new Error(`Request timed out after ${this.requestTimeoutMs}ms`);
      }
      throw e;
    } finally {
      clearTimeout(timeoutId);
    }
  }

  // ── Authentication ──
  getLoginUrl() {
    return `https://kite.zerodha.com/connect/login?v=3&api_key=${this.apiKey}`;
  }

  async generateSession(requestToken, apiSecret = '') {
    const body = {
      api_key: this.apiKey,
      request_token: requestToken
    };
    if (apiSecret) body.api_secret = apiSecret;
    return this._post('/api/login', body);
  }

  // ── Instruments ──
  async getInstruments() {
    const data = await this._get('/api/instruments');
    this.instruments = data.instruments || data;
    this.fnoInstruments = this.instruments.filter(i =>
      i.segment === 'NFO-FUT' || i.segment === 'NFO-OPT' ||
      (i.segment === 'NSE' && i.exchange === 'NSE')
    );
    return this.instruments;
  }

  getFNOStockList() {
    // Returns unique underlying F&O stock names
    const fnoNames = new Set();
    this.instruments.forEach(i => {
      if (i.segment === 'NFO-FUT' || i.segment === 'NFO-OPT') {
        fnoNames.add(i.name || i.tradingsymbol?.replace(/\d.*/,''));
      }
    });
    return [...fnoNames].sort();
  }

  getInstrumentToken(symbol, exchange = 'NSE') {
    const inst = this.instruments.find(i =>
      i.tradingsymbol === symbol && i.exchange === exchange
    );
    return inst ? inst.instrument_token : null;
  }

  // ── Quotes ──
  async getQuote(symbols) {
    // symbols: array like ['NSE:RELIANCE', 'NSE:TCS']
    return this._get('/api/quote', { symbols: symbols.join(',') });
  }

  async getLTP(symbols) {
    return this._get('/api/ltp', { symbols: symbols.join(',') });
  }

  async getOHLC(symbols) {
    return this._get('/api/ohlc', { symbols: symbols.join(',') });
  }

  // ── Historical Data ──
  async getHistoricalData(instrumentToken, from, to, interval = 'day') {
    return this._get('/api/historical', {
      token: instrumentToken,
      from: from,
      to: to,
      interval: interval
    });
  }

  // ── Options Chain ──
  async getOptionChain(symbol, expiry) {
    return this._get('/api/option-chain', {
      symbol: symbol,
      expiry: expiry || ''
    });
  }

  async getExpiries(symbol) {
    return this._get('/api/expiries', { symbol: symbol });
  }

  // ── WebSocket (via backend proxy) ──
  connectWebSocket(tokens, onTick) {
    if (this.ws) this.disconnectWebSocket();

    // Use SocketIO for WebSocket connection
    // Socket.IO client expects the http(s) origin (it handles WS upgrade internally).
    const socketUrl = this.backendUrl;
    this.ws = io(socketUrl, { transports: ['polling'] });

    this.ws.on('connect', () => {
      this.connected = true;
      this.ws.emit('subscribe', {
        tokens: tokens,
        api_key: this.apiKey,
        access_token: this.accessToken
      });
      this._notifyConnection(true);
    });

    this.ws.on('ticks', (ticks) => {
      if (Array.isArray(ticks)) {
        ticks.forEach(t => {
          this.lastTicks[t.instrument_token] = t;
        });
        if (onTick) onTick(ticks);
        this.tickCallbacks.forEach(cb => cb(ticks));
      }
    });

    this.ws.on('error', (error) => {
      console.error('WebSocket error:', error);
      this.connected = false;
      this._notifyConnection(false);
    });

    this.ws.on('disconnect', () => {
      this.connected = false;
      this._notifyConnection(false);
    });

    this.ws.on('subscribed', (data) => {
      console.log('WebSocket subscribed:', data.message);
    });
  }

  disconnectWebSocket() {
    if (this.ws) {
      this.ws.disconnect();
      this.ws = null;
    }
    this.connected = false;
    this._notifyConnection(false);
  }

  onTick(callback) {
    this.tickCallbacks.push(callback);
  }

  _notifyConnection(status) {
    document.dispatchEvent(new CustomEvent('kite-connection', { detail: { connected: status } }));
  }

  // ── Connection Test ──
  async testConnection(retries = 2) {
    for (let attempt = 1; attempt <= retries; attempt++) {
      try {
        const data = await this._get('/api/test');
        this.connected = true;
        this._notifyConnection(true);
        return { success: true, data };
      } catch (e) {
        if (attempt < retries) {
          console.warn(`Kite connection attempt ${attempt} failed, retrying in 2s...`);
          await new Promise(r => setTimeout(r, 2000));
        } else {
          this.connected = false;
          this._notifyConnection(false);
          return { success: false, error: e.message };
        }
      }
    }
  }

  // ── Market Overview ──
  async getMarketOverview() {
    return this._get('/api/market-overview');
  }

  // ── Stock Snapshot (enriched: equity + futures + ATM options) ──
  async getStockSnapshot(symbol) {
    return this._get('/api/stock-snapshot', { symbol });
  }

  // ── Batch Snapshots (equity + futures for multiple stocks) ──
  async getBatchSnapshots(symbols) {
    return this._get('/api/batch-snapshots', { symbols: symbols.join(',') });
  }
}

// Global singleton
window.kiteAPI = new KiteAPI();
