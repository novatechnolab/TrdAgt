"""
CLEAN REWRITE OF validate_entry() ENDPOINT
This file documents the exact fix needed.
Copy the function code below and replace lines 3915-4156 in server.py
"""

@app.route('/api/validate-entry', methods=['POST'])
def validate_entry():
    """CLEAN REWRITE: Entry validation endpoint.
    Returns OHLCV candles + live snapshot for frontend indicator calculation.
    
    Implements cache-aside pattern:
    - REPLAY mode: Check SQLite cache first, fallback to Kite API
    - LIVE mode: Always call Kite API for fresh data
    """
    from datetime import datetime, timedelta
    
    body = request.get_json(silent=True) or {}
    symbol = body.get('symbol', '').strip()
    price = float(body.get('price')) if body.get('price') else 0
    direction = body.get('direction', 'CALL')
    interval = body.get('interval', '5minute')
    replay_date_str = body.get('replay_date', '').strip()
    replay_time_str = body.get('replay_time', '').strip()
    
    if not symbol:
        return jsonify({'error': 'symbol required'}), 400
    
    logging.warning(f'\n=== VALIDATE ENTRY: {symbol} Price={price} Date={replay_date_str} Time={replay_time_str} ===\n')
    
    try:
        # Get Kite connection
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Kite not initialized'}), 500
        
        # Resolve instrument token
        exchange = 'NSE' if 'BSE' not in symbol.upper() else 'BSE'
        instruments = cache_get_instruments(get_db())
        token = None
        
        for inst in instruments:
            if inst.get('tradingsymbol') == symbol and inst.get('exchange') == exchange:
                token = inst.get('instrument_token')
                break
        
        if not token:
            return jsonify({'error': f'Token not found: {symbol}'}), 404
        
        logging.warning(f'Token: {token}')
        
        # Parse replay date/time
        is_replay = bool(replay_date_str)
        
        if is_replay:
            try:
                target_dt = datetime.strptime(replay_date_str[:10], '%Y-%m-%d')
                if replay_time_str:
                    hh, mm = map(int, replay_time_str.split(':'))
                    target_dt = target_dt.replace(hour=hh, minute=mm)
                else:
                    target_dt = target_dt.replace(hour=15, minute=30)
            except Exception as e:
                return jsonify({'error': f'Invalid date/time: {repr(e)}'}), 400
        else:
            target_dt = datetime.now()
        
        # Build Kite API query range (10 days lookback)
        from_dt = target_dt - timedelta(days=10)
        from_date = from_dt.strftime('%Y-%m-%d 09:15:00')
        to_dt = target_dt + timedelta(minutes=5)  # +5min for Kite's exclusive upper bound
        to_date = to_dt.strftime('%Y-%m-%d %H:%M:%S')
        
        logging.warning(f'Mode: {"REPLAY" if is_replay else "LIVE"} | Date: {target_dt} | Range: {from_date} → {to_date}')
        
        # Fetch candles with cache
        candles = []
        data_source = 'none'
        
        if is_replay:
            # Try cache first
            db = get_db()
            cached = cache_get_ohlcv(db, token, from_date, to_date, interval)
            if cached and len(cached) > 5:
                candles = cached
                data_source = 'sqlite_cache'
                logging.warning(f'✓ Cache HIT: {len(candles)} candles')
            else:
                data_source = 'kite_api'
                logging.warning(f'Cache miss → Calling Kite API')
        else:
            data_source = 'kite_api'
            logging.warning(f'Live mode → Calling Kite API')
        
        if data_source == 'kite_api':
            try:
                kite_data = kite.historical_data(int(token), from_date, to_date, interval)
                for d in kite_data:
                    candles.append({
                        'date': d['date'].isoformat() if hasattr(d['date'], 'isoformat') else str(d['date']),
                        'open': float(d['open']),
                        'high': float(d['high']),
                        'low': float(d['low']),
                        'close': float(d['close']),
                        'volume': int(d.get('volume', 0)),
                    })
                
                logging.warning(f'✓ Kite returned {len(candles)} candles')
                
                # Cache for replay
                if candles and is_replay:
                    try:
                        db = get_db()
                        cache_store_ohlcv(db, token, candles, interval)
                        db.commit()
                        logging.warning(f'✓ Cached {len(candles)} candles')
                    except Exception as e:
                        logging.warning(f'Cache store failed (non-critical): {e}')
            
            except Exception as e:
                logging.error(f'Kite API error: {e}')
                return jsonify({'error': f'Kite API error: {str(e)}'}), 500
        
        if not candles:
            return jsonify({'error': 'No candles available'}), 404
        
        # Get snapshot
        snapshot = {}
        try:
            q = kite.quote(f'{exchange}:{symbol}')
            if f'{exchange}:{symbol}' in q:
                qd = q[f'{exchange}:{symbol}']
                snapshot = {
                    'ltp': float(qd.get('last_price', 0)),
                    'vwap': float(qd.get('average_price', 0)),
                    'volume': int(qd.get('volume', 0)),
                    'symbol': symbol,
                }
        except Exception:
            pass
        
        logging.warning(f'✓ Response: {len(candles)} candles | First: {candles[0]["close"]} | Last: {candles[-1]["close"]}\n')
        
        return jsonify({
            'success': True,
            'candles': candles,
            'snapshot': snapshot,
            'symbol': symbol,
            'price': price,
            'direction': direction,
            'interval': interval,
            'count': len(candles),
            'data_source': data_source,
            'is_replay': is_replay,
        })
    
    except Exception as e:
        logging.error(f'Exception: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500
