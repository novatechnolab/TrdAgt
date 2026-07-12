import os
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from kiteconnect import KiteTicker
from twisted.internet import reactor

log = logging.getLogger("GlobalTicker")

_manager = None
_manager_lock = threading.Lock()

def get_global_ticker_manager():
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = GlobalTickerManager()
        return _manager

class GlobalTickerManager:
    def __init__(self):
        self.kws = None
        self.lock = threading.Lock()
        self.last_exception = None
        self.callbacks = {}              # key -> callback_fn
        self.tokens_by_key = {}          # key -> set of tokens
        self.modes_by_key = {}           # key -> mode ("FULL" or "LTP")
        
        self.active_tokens = set()       # Combined unique tokens
        self.token_modes = {}            # token -> highest mode ("FULL" or "LTP")
        
        self.is_running = False
        self.api_key = None
        self.access_token = None
        self.executor = ThreadPoolExecutor(max_workers=4)  # Asynchronous thread pool

    def initialize(self, api_key, access_token):
        self.api_key = api_key
        self.access_token = access_token

    def register(self, key, callback, tokens, mode="LTP"):
        with self.lock:
            self.callbacks[key] = callback
            self.tokens_by_key[key] = set(tokens)
            self.modes_by_key[key] = mode
            self._recalculate_subscriptions()

    def unregister(self, key):
        with self.lock:
            if key in self.callbacks:
                del self.callbacks[key]
            if key in self.tokens_by_key:
                del self.tokens_by_key[key]
            if key in self.modes_by_key:
                del self.modes_by_key[key]
            self._recalculate_subscriptions()

    def update_subscription(self, key, to_subscribe, to_unsubscribe):
        with self.lock:
            if key not in self.tokens_by_key:
                self.tokens_by_key[key] = set()
            if to_unsubscribe:
                self.tokens_by_key[key].difference_update(to_unsubscribe)
            if to_subscribe:
                self.tokens_by_key[key].update(to_subscribe)
            self._recalculate_subscriptions()

    def _recalculate_subscriptions(self):
        old_tokens = set(self.active_tokens)
        new_tokens = set()
        new_modes = {}

        # Aggregate tokens and find the highest requested mode per token
        for key, tkns in self.tokens_by_key.items():
            req_mode = self.modes_by_key.get(key, "LTP")
            for t in tkns:
                new_tokens.add(t)
                if new_modes.get(t) == "FULL" or req_mode == "FULL":
                    new_modes[t] = "FULL"
                else:
                    new_modes[t] = "LTP"

        self.active_tokens = new_tokens
        self.token_modes = new_modes

        if not self.kws or not self.kws.is_connected():
            return

        # Diff and apply changes dynamically
        subscribe_diff = new_tokens - old_tokens
        unsubscribe_diff = old_tokens - new_tokens

        if unsubscribe_diff:
            try:
                self.kws.unsubscribe(list(unsubscribe_diff))
                log.info(f"[GlobalTicker] Centralized unsubscribe from {len(unsubscribe_diff)} tokens.")
            except Exception as e:
                log.error(f"[GlobalTicker] Centralized unsubscribe failed: {e}")

        if subscribe_diff:
            try:
                self.kws.subscribe(list(subscribe_diff))
                # Set modes in batches
                full_tokens = [t for t in subscribe_diff if new_modes.get(t) == "FULL"]
                ltp_tokens = [t for t in subscribe_diff if new_modes.get(t) == "LTP"]
                if full_tokens:
                    self.kws.set_mode(self.kws.MODE_FULL, full_tokens)
                if ltp_tokens:
                    self.kws.set_mode(self.kws.MODE_LTP, ltp_tokens)
                log.info(f"[GlobalTicker] Centralized subscribe to {len(subscribe_diff)} tokens.")
            except Exception as e:
                log.error(f"[GlobalTicker] Centralized subscribe failed: {e}")

    def start(self):
        with self.lock:
            if self.is_running:
                return
            self.is_running = True
            threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self):
        while self.is_running:
            try:
                if not self.api_key or not self.access_token:
                    print("[GlobalTicker] Credentials not set yet. Standing by...")
                    time.sleep(5)
                    continue

                # Check if already connected to avoid duplicate connections
                if self.kws and self.kws.is_connected():
                    time.sleep(5)
                    continue

                print(f"[GlobalTicker] Connecting single shared WebSocket session with key={self.api_key[:5]}...")
                log.info("[GlobalTicker] Connecting single shared WebSocket session...")
                kws = KiteTicker(self.api_key, self.access_token)

                def on_ticks(ws, ticks):
                    # print(f"[GlobalTicker] Received {len(ticks)} ticks.")
                    with self.lock:
                        callbacks = list(self.callbacks.values())
                    for cb in callbacks:
                        def run_cb(c=cb, t=ticks, w=ws):
                            try:
                                import inspect
                                sig = inspect.signature(c)
                                if len(sig.parameters) == 1:
                                    c(t)
                                else:
                                    c(w, t)
                            except Exception as e:
                                print(f"[GlobalTicker] Callback invocation failed: {e}")
                                log.error(f"[GlobalTicker] Callback invocation failed: {e}")
                        self.executor.submit(run_cb)

                def on_connect(ws, response):
                    print("[GlobalTicker] Connected successfully to WebSocket.")
                    with self.lock:
                        tokens = list(self.active_tokens)
                        modes = dict(self.token_modes)
                    if tokens:
                        ws.subscribe(tokens)
                        full_tokens = [t for t in tokens if modes.get(t) == "FULL"]
                        ltp_tokens = [t for t in tokens if modes.get(t) == "LTP"]
                        if full_tokens:
                            ws.set_mode(ws.MODE_FULL, full_tokens)
                        if ltp_tokens:
                            ws.set_mode(ws.MODE_LTP, ltp_tokens)
                    print(f"[GlobalTicker] Live! Subscribed to {len(tokens)} combined tokens.")
                    log.info(f"[GlobalTicker] Live! Subscribed to {len(tokens)} combined tokens.")

                def on_error(ws, code, reason):
                    print(f"[GlobalTicker] WS error {code}: {reason}")
                    log.error(f"[GlobalTicker] WS error {code}: {reason}")
                    self.last_exception = f"WS Error {code}: {reason}"

                def on_close(ws, code, reason):
                    print(f"[GlobalTicker] WS closed {code}: {reason}")
                    log.warning(f"[GlobalTicker] WS closed {code}: {reason}")

                kws.on_ticks = on_ticks
                kws.on_connect = on_connect
                kws.on_error = on_error
                kws.on_close = on_close
                self.kws = kws
                reactor.callFromThread(kws.connect, threaded=False)
            except Exception as e:
                print(f"[GlobalTicker] Loop crashed: {e}")
                log.error(f"[GlobalTicker] Loop crashed: {e}")
                self.last_exception = str(e)
            time.sleep(15)

import json

CONFIG_FILE = "ticker_config.json"

def load_ticker_config():
    """
    Load ticker configuration.
    Defaults to dedicated for synergy, centralized for others.
    """
    default_config = {
        "synergy": "centralized",
        "ema_crossover": "centralized",
        "market_stream": "centralized",
        "client": "centralized"
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                for k, v in data.items():
                    default_config[k] = v
        except Exception as e:
            log.error(f"[GlobalTicker] Failed to read config JSON: {e}")
    return default_config

def save_ticker_config(config):
    """
    Save new ticker configuration to JSON file.
    """
    try:
        current_config = load_ticker_config()
        for k, v in config.items():
            current_config[k] = v
        with open(CONFIG_FILE, "w") as f:
            json.dump(current_config, f, indent=4)
        log.info("[GlobalTicker] Ticker configuration updated and saved to disk.")
    except Exception as e:
        log.error(f"[GlobalTicker] Failed to write config JSON: {e}")

def get_ticker_mode(feature_name):
    # Support wildcard match for dynamic client IDs
    if feature_name.startswith("client_"):
        name_key = "client"
    else:
        name_key = feature_name

    # Check JSON config first
    config = load_ticker_config()
    if name_key in config:
        return config[name_key].lower()

    # Fallback to env variable
    env_var = f"TICKER_MODE_{name_key.upper()}"
    return os.environ.get(env_var, "centralized").lower()

def get_ticker_for_feature(feature_name, initial_tokens, on_ticks_cb, mode="FULL"):
    ticker_mode = get_ticker_mode(feature_name)
    log.info(f"[TickerFactory] Setting up ticker for '{feature_name}' in '{ticker_mode}' mode.")
    
    if ticker_mode == "dedicated":
        from server import _load_kite_session
        api_key, access_token = _load_kite_session()
        
        kws = KiteTicker(api_key, access_token)
        kws.on_ticks = on_ticks_cb
        
        # Connect asynchronously in a dedicated daemon thread
        def run_dedicated():
            def on_connect_ded(ws, r):
                try:
                    ws.subscribe(initial_tokens)
                    if mode == "FULL":
                        ws.set_mode(ws.MODE_FULL, initial_tokens)
                    else:
                        ws.set_mode(ws.MODE_LTP, initial_tokens)
                    log.info(f"[TickerFactory] Dedicated ticker connected & subscribed to {len(initial_tokens)} tokens in {mode} mode.")
                except Exception as e:
                    log.error(f"[TickerFactory] Dedicated connect subscription failed: {e}")
            kws.on_connect = on_connect_ded
            reactor.callFromThread(kws.connect, threaded=False)
            
        threading.Thread(target=run_dedicated, daemon=True).start()
        return kws
    else:
        gtm = get_global_ticker_manager()
        gtm.register(feature_name, on_ticks_cb, initial_tokens, mode=mode)
        
        class CentralizedTickerProxy:
            MODE_FULL = "full"
            MODE_QUOTE = "quote"
            MODE_LTP = "ltp"

            def __init__(self):
                self.on_ticks = None
                self.on_connect = None
                self.on_error = None
                self.on_close = None

            def is_connected(self):
                return gtm.kws is not None and gtm.kws.is_connected()
            def subscribe(self, tokens):
                gtm.update_subscription(feature_name, tokens, [])
            def unsubscribe(self, tokens):
                gtm.update_subscription(feature_name, [], tokens)
            def set_mode(self, mode_val, tokens):
                pass
            def connect(self, threaded=True):
                pass
            def close(self):
                gtm.unregister(feature_name)
                
        return CentralizedTickerProxy()
