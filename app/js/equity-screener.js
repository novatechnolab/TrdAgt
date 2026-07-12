/**
 * TradeSignal — Equity Screener (F&O Stocks Only)
 * Full NSE F&O universe (~200+ stocks) with live Kite API data.
 * Dynamically discovers additional F&O stocks from Kite API instruments list.
 */
class EquityScreener {
  constructor() {
    this.stocks = [];
    this.filtered = [];
    this.filters = { sector: '', cap: 'all', signal: '', sort: 'score' };
    this._dynamicUniverse = null; // cached from Kite API
    this._scanPromise = null; // single-flight guard to prevent overlapping scans
  }

  // ── Daily Trend from EMA-9 vs EMA-21 on daily closes ──
  computeDailyTrend(closes) {
    if (!closes || closes.length < 21) return 'Neutral';
    const ema = (span) => {
      const k = 2 / (span + 1);
      let val = closes[0];
      for (let i = 1; i < closes.length; i++) val = closes[i] * k + val * (1 - k);
      return val;
    };
    const ema9 = ema(9), ema21 = ema(21), last = closes[closes.length - 1];
    if (ema9 > ema21 && last > ema21) return 'Bullish';
    if (ema9 < ema21 && last < ema21) return 'Bearish';
    return 'Neutral';
  }

  // ── Expiry week detection (NSE F&O weekly/monthly expiry on Thursdays) ──
  isExpiryWeek() {
    const now = new Date();
    const day = now.getDay(); // 0=Sun, 4=Thu
    const daysToThursday = (4 - day + 7) % 7;
    return daysToThursday <= 3; // Thursday is within 3 calendar days
  }

  // ── Compute session VWAP from 15-min candle data ──
  computeSessionVwap(cdata) {
    if (!cdata || !cdata.closes || cdata.closes.length === 0) return null;
    const { closes, highs = [], lows = [], volumes = [] } = cdata;
    let cumTPV = 0, cumVol = 0;
    for (let i = 0; i < closes.length; i++) {
      const tp = ((highs[i] || closes[i]) + (lows[i] || closes[i]) + closes[i]) / 3;
      const vol = volumes[i] || 0;
      cumTPV += tp * vol;
      cumVol += vol;
    }
    return cumVol > 0 ? cumTPV / cumVol : null;
  }

  // ── Compute Nifty bias from pass-1 market breadth ──
  _computeNiftyBias(pass1Results) {
    if (!pass1Results || pass1Results.length === 0) return 'NEUTRAL';
    const bullish = pass1Results.filter(s => s.isBullishTrend && s.score > 50).length;
    const bearish = pass1Results.filter(s => !s.isBullishTrend && s.score > 50).length;
    const total = pass1Results.length;
    const bullPct = bullish / total;
    const bearPct = bearish / total;
    if (bullPct > 0.55) return 'BULLISH';
    if (bearPct > 0.45) return 'BEARISH';
    return 'NEUTRAL';
  }

  // ── Complete NSE F&O Stock Universe ──
  getStaticFNOUniverse() {
    return [
      { symbol: '360ONE', name: '360 One Wam', sector: 'Finance', cap: 'mid', lot: 500 },
      { symbol: 'AARTIIND', name: 'Aarti Industries', sector: 'Chemicals', cap: 'mid', lot: 1000 },
      { symbol: 'AAVAS', name: 'Aavas Financiers', sector: 'Finance', cap: 'mid', lot: 250 },
      { symbol: 'ABB', name: 'ABB India', sector: 'Infra', cap: 'mid', lot: 125 },
      { symbol: 'ABBOTINDIA', name: 'Abbott India', sector: 'Pharma', cap: 'mid', lot: 25 },
      { symbol: 'ABCAPITAL', name: 'Aditya Birla Capital', sector: 'Finance', cap: 'mid', lot: 2700 },
      { symbol: 'ABFRL', name: 'Aditya Birla Fashion', sector: 'Retail', cap: 'mid', lot: 1850 },
      { symbol: 'ACC', name: 'ACC Limited', sector: 'Infra', cap: 'mid', lot: 250 },
      { symbol: 'ADANIENSOL', name: 'Adani Energy Solutions', sector: 'Energy', cap: 'large', lot: 675 },
      { symbol: 'ADANIENT', name: 'Adani Enterprises', sector: 'Infra', cap: 'large', lot: 250 },
      { symbol: 'ADANIGREEN', name: 'Adani Green Energy', sector: 'Energy', cap: 'large', lot: 250 },
      { symbol: 'ADANIPORTS', name: 'Adani Ports', sector: 'Infra', cap: 'large', lot: 500 },
      { symbol: 'ADANIPOWER', name: 'Adani Power', sector: 'Energy', cap: 'large', lot: 1250 },
      { symbol: 'ADANITRANS', name: 'Adani Transmission', sector: 'Energy', cap: 'large', lot: 250 },
      { symbol: 'AIAENG', name: 'AIA Engineering', sector: 'Infra', cap: 'mid', lot: 125 },
      { symbol: 'ALKEM', name: 'Alkem Laboratories', sector: 'Pharma', cap: 'mid', lot: 100 },
      { symbol: 'AMBER', name: 'Amber Enterprises', sector: 'Infra', cap: 'mid', lot: 100 },
      { symbol: 'AMBUJACEM', name: 'Ambuja Cements', sector: 'Infra', cap: 'mid', lot: 750 },
      { symbol: 'ANGELONE', name: 'Angel One', sector: 'Finance', cap: 'mid', lot: 200 },
      { symbol: 'APLAPOLLO', name: 'APL Apollo Tubes', sector: 'Metal', cap: 'mid', lot: 275 },
      { symbol: 'APOLLOHOSP', name: 'Apollo Hospitals', sector: 'Pharma', cap: 'large', lot: 125 },
      { symbol: 'ASHOKLEY', name: 'Ashok Leyland', sector: 'Auto', cap: 'mid', lot: 3125 },
      { symbol: 'ASIANPAINT', name: 'Asian Paints', sector: 'FMCG', cap: 'large', lot: 300 },
      { symbol: 'ASTRAL', name: 'Astral Limited', sector: 'Infra', cap: 'mid', lot: 275 },
      { symbol: 'ATGL', name: 'Adani Total Gas', sector: 'Energy', cap: 'large', lot: 250 },
      { symbol: 'ATUL', name: 'Atul Limited', sector: 'Chemicals', cap: 'mid', lot: 50 },
      { symbol: 'AUBANK', name: 'AU Small Finance Bank', sector: 'Banking', cap: 'mid', lot: 700 },
      { symbol: 'AUROPHARMA', name: 'Aurobindo Pharma', sector: 'Pharma', cap: 'mid', lot: 425 },
      { symbol: 'AXISBANK', name: 'Axis Bank', sector: 'Banking', cap: 'large', lot: 625 },
      { symbol: 'BAJAJ-AUTO', name: 'Bajaj Auto', sector: 'Auto', cap: 'large', lot: 75 },
      { symbol: 'BAJAJFINSV', name: 'Bajaj Finserv', sector: 'Finance', cap: 'large', lot: 500 },
      { symbol: 'BAJAJHLDNG', name: 'Bajaj Holdings', sector: 'Finance', cap: 'mid', lot: 50 },
      { symbol: 'BAJFINANCE', name: 'Bajaj Finance', sector: 'Finance', cap: 'large', lot: 125 },
      { symbol: 'BALKRISIND', name: 'Balkrishna Industries', sector: 'Auto', cap: 'mid', lot: 200 },
      { symbol: 'BANDHANBNK', name: 'Bandhan Bank', sector: 'Banking', cap: 'mid', lot: 2400 },
      { symbol: 'BANKBARODA', name: 'Bank of Baroda', sector: 'Banking', cap: 'mid', lot: 2925 },
      { symbol: 'BANKINDIA', name: 'Bank of India', sector: 'Banking', cap: 'mid', lot: 4500 },
      { symbol: 'BATAINDIA', name: 'Bata India', sector: 'FMCG', cap: 'mid', lot: 275 },
      { symbol: 'BDL', name: 'Bharat Dynamics', sector: 'Defence', cap: 'mid', lot: 350 },
      { symbol: 'BEL', name: 'Bharat Electronics', sector: 'Defence', cap: 'mid', lot: 1950 },
      { symbol: 'BERGEPAINT', name: 'Berger Paints', sector: 'FMCG', cap: 'mid', lot: 950 },
      { symbol: 'BHARATFORG', name: 'Bharat Forge', sector: 'Auto', cap: 'mid', lot: 500 },
      { symbol: 'BHARTIARTL', name: 'Bharti Airtel', sector: 'Telecom', cap: 'large', lot: 475 },
      { symbol: 'BHEL', name: 'Bharat Heavy Elec', sector: 'Infra', cap: 'mid', lot: 2750 },
      { symbol: 'BIOCON', name: 'Biocon', sector: 'Pharma', cap: 'mid', lot: 2300 },
      { symbol: 'BLUESTARCO', name: 'Blue Star', sector: 'Infra', cap: 'mid', lot: 325 },
      { symbol: 'BOSCHLTD', name: 'Bosch Limited', sector: 'Auto', cap: 'mid', lot: 25 },
      { symbol: 'BPCL', name: 'BPCL', sector: 'Energy', cap: 'large', lot: 1800 },
      { symbol: 'BRIGADE', name: 'Brigade Enterprises', sector: 'Realty', cap: 'mid', lot: 500 },
      { symbol: 'BRITANNIA', name: 'Britannia Industries', sector: 'FMCG', cap: 'large', lot: 200 },
      { symbol: 'BSE', name: 'BSE Limited', sector: 'Finance', cap: 'mid', lot: 175 },
      { symbol: 'BSOFT', name: 'Birlasoft', sector: 'IT', cap: 'mid', lot: 750 },
      { symbol: 'CAMS', name: 'Computer Age Mgmt Services', sector: 'Finance', cap: 'mid', lot: 150 },
      { symbol: 'CANBK', name: 'Canara Bank', sector: 'Banking', cap: 'mid', lot: 4500 },
      { symbol: 'CANFINHOME', name: 'Can Fin Homes', sector: 'Finance', cap: 'mid', lot: 600 },
      { symbol: 'CDSL', name: 'CDSL', sector: 'Finance', cap: 'mid', lot: 375 },
      { symbol: 'CENTRALBK', name: 'Central Bank of India', sector: 'Banking', cap: 'mid', lot: 7500 },
      { symbol: 'CENTURYTEX', name: 'Century Textiles', sector: 'Infra', cap: 'mid', lot: 250 },
      { symbol: 'CESC', name: 'CESC Limited', sector: 'Energy', cap: 'mid', lot: 3750 },
      { symbol: 'CGPOWER', name: 'CG Power & Industrial', sector: 'Infra', cap: 'mid', lot: 750 },
      { symbol: 'CHAMBLFERT', name: 'Chambal Fertilisers', sector: 'Chemicals', cap: 'mid', lot: 1450 },
      { symbol: 'CHOLAFIN', name: 'Cholamandalam Inv', sector: 'Finance', cap: 'mid', lot: 500 },
      { symbol: 'CIPLA', name: 'Cipla', sector: 'Pharma', cap: 'large', lot: 650 },
      { symbol: 'CLEAN', name: 'Clean Science & Tech', sector: 'Chemicals', cap: 'mid', lot: 400 },
      { symbol: 'COALINDIA', name: 'Coal India', sector: 'Energy', cap: 'large', lot: 2100 },
      { symbol: 'COCHINSHIP', name: 'Cochin Shipyard', sector: 'Defence', cap: 'mid', lot: 250 },
      { symbol: 'COFORGE', name: 'Coforge', sector: 'IT', cap: 'mid', lot: 75 },
      { symbol: 'COLPAL', name: 'Colgate Palmolive', sector: 'FMCG', cap: 'mid', lot: 175 },
      { symbol: 'CONCOR', name: 'Container Corp', sector: 'Infra', cap: 'mid', lot: 750 },
      { symbol: 'COROMANDEL', name: 'Coromandel International', sector: 'Chemicals', cap: 'mid', lot: 475 },
      { symbol: 'CROMPTON', name: 'Crompton Greaves CE', sector: 'Infra', cap: 'mid', lot: 1500 },
      { symbol: 'CUMMINSIND', name: 'Cummins India', sector: 'Infra', cap: 'mid', lot: 200 },
      { symbol: 'CYIENT', name: 'Cyient Limited', sector: 'IT', cap: 'mid', lot: 350 },
      { symbol: 'DABUR', name: 'Dabur India', sector: 'FMCG', cap: 'mid', lot: 900 },
      { symbol: 'DALBHARAT', name: 'Dalmia Bharat', sector: 'Infra', cap: 'mid', lot: 250 },
      { symbol: 'DATAPATTNS', name: 'Data Patterns India', sector: 'Defence', cap: 'mid', lot: 175 },
      { symbol: 'DEEPAKNTR', name: 'Deepak Nitrite', sector: 'Chemicals', cap: 'mid', lot: 200 },
      { symbol: 'DELHIVERY', name: 'Delhivery', sector: 'Retail', cap: 'mid', lot: 1500 },
      { symbol: 'DEVYANI', name: 'Devyani International', sector: 'FMCG', cap: 'mid', lot: 2500 },
      { symbol: 'DIVISLAB', name: 'Divis Laboratories', sector: 'Pharma', cap: 'large', lot: 175 },
      { symbol: 'DIXON', name: 'Dixon Technologies', sector: 'IT', cap: 'mid', lot: 50 },
      { symbol: 'DLF', name: 'DLF Limited', sector: 'Realty', cap: 'mid', lot: 825 },
      { symbol: 'DMART', name: 'Avenue Supermarts', sector: 'Retail', cap: 'large', lot: 125 },
      { symbol: 'DRREDDY', name: 'Dr Reddys Labs', sector: 'Pharma', cap: 'large', lot: 125 },
      { symbol: 'EICHERMOT', name: 'Eicher Motors', sector: 'Auto', cap: 'large', lot: 175 },
      { symbol: 'ESCORTS', name: 'Escorts Kubota', sector: 'Auto', cap: 'mid', lot: 125 },
      { symbol: 'EXIDEIND', name: 'Exide Industries', sector: 'Auto', cap: 'mid', lot: 1200 },
      { symbol: 'FACT', name: 'Fertilisers & Chemicals', sector: 'Chemicals', cap: 'mid', lot: 500 },
      { symbol: 'FEDERALBNK', name: 'Federal Bank', sector: 'Banking', cap: 'mid', lot: 5000 },
      { symbol: 'FORCEMOT', name: 'Force Motors', sector: 'Auto', cap: 'mid', lot: 50 },
      { symbol: 'FORTIS', name: 'Fortis Healthcare', sector: 'Pharma', cap: 'mid', lot: 1000 },
      { symbol: 'GAIL', name: 'GAIL India', sector: 'Energy', cap: 'mid', lot: 3050 },
      { symbol: 'GLAND', name: 'Gland Pharma', sector: 'Pharma', cap: 'mid', lot: 300 },
      { symbol: 'GLENMARK', name: 'Glenmark Pharma', sector: 'Pharma', cap: 'mid', lot: 575 },
      { symbol: 'GMRINFRA', name: 'GMR Airports Infra', sector: 'Infra', cap: 'mid', lot: 5000 },
      { symbol: 'GNFC', name: 'Gujarat Narmada Valley', sector: 'Chemicals', cap: 'mid', lot: 575 },
      { symbol: 'GODFRYPHLP', name: 'Godfrey Phillips India', sector: 'FMCG', cap: 'mid', lot: 75 },
      { symbol: 'GODREJCP', name: 'Godrej Consumer Products', sector: 'FMCG', cap: 'mid', lot: 500 },
      { symbol: 'GODREJPROP', name: 'Godrej Properties', sector: 'Realty', cap: 'mid', lot: 325 },
      { symbol: 'GRASIM', name: 'Grasim Industries', sector: 'Infra', cap: 'large', lot: 350 },
      { symbol: 'GRINDWELL', name: 'Grindwell Norton', sector: 'Infra', cap: 'mid', lot: 175 },
      { symbol: 'HAL', name: 'Hindustan Aeronautics', sector: 'Defence', cap: 'large', lot: 125 },
      { symbol: 'HAVELLS', name: 'Havells India', sector: 'Infra', cap: 'mid', lot: 350 },
      { symbol: 'HCLTECH', name: 'HCL Technologies', sector: 'IT', cap: 'large', lot: 500 },
      { symbol: 'HDFCAMC', name: 'HDFC AMC', sector: 'Finance', cap: 'mid', lot: 125 },
      { symbol: 'HDFCBANK', name: 'HDFC Bank', sector: 'Banking', cap: 'large', lot: 550 },
      { symbol: 'HDFCLIFE', name: 'HDFC Life Insurance', sector: 'Insurance', cap: 'large', lot: 550 },
      { symbol: 'HEROMOTOCO', name: 'Hero MotoCorp', sector: 'Auto', cap: 'large', lot: 150 },
      { symbol: 'HINDALCO', name: 'Hindalco', sector: 'Metal', cap: 'large', lot: 1400 },
      { symbol: 'HINDCOPPER', name: 'Hindustan Copper', sector: 'Metal', cap: 'mid', lot: 1850 },
      { symbol: 'HINDPETRO', name: 'Hindustan Petroleum', sector: 'Energy', cap: 'mid', lot: 1900 },
      { symbol: 'HINDUNILVR', name: 'Hindustan Unilever', sector: 'FMCG', cap: 'large', lot: 300 },
      { symbol: 'HUDCO', name: 'HUDCO', sector: 'Finance', cap: 'mid', lot: 3000 },
      { symbol: 'HYUNDAI', name: 'Hyundai Motor India', sector: 'Auto', cap: 'large', lot: 250 },
      { symbol: 'ICICIBANK', name: 'ICICI Bank', sector: 'Banking', cap: 'large', lot: 700 },
      { symbol: 'ICICIGI', name: 'ICICI Lombard GIC', sector: 'Insurance', cap: 'mid', lot: 250 },
      { symbol: 'ICICIPRULI', name: 'ICICI Prudential Life', sector: 'Insurance', cap: 'mid', lot: 750 },
      { symbol: 'IDBI', name: 'IDBI Bank', sector: 'Banking', cap: 'mid', lot: 5000 },
      { symbol: 'IDEA', name: 'Vodafone Idea', sector: 'Telecom', cap: 'mid', lot: 47500 },
      { symbol: 'IDFCFIRSTB', name: 'IDFC First Bank', sector: 'Banking', cap: 'mid', lot: 7500 },
      { symbol: 'IEX', name: 'Indian Energy Exchange', sector: 'Energy', cap: 'mid', lot: 3750 },
      { symbol: 'IGL', name: 'Indraprastha Gas', sector: 'Energy', cap: 'mid', lot: 1375 },
      { symbol: 'IIFL', name: 'IIFL Finance', sector: 'Finance', cap: 'mid', lot: 1750 },
      { symbol: 'INDHOTEL', name: 'Indian Hotels', sector: 'Retail', cap: 'mid', lot: 750 },
      { symbol: 'INDIANB', name: 'Indian Bank', sector: 'Banking', cap: 'mid', lot: 1250 },
      { symbol: 'INDIGO', name: 'InterGlobe Aviation', sector: 'Auto', cap: 'large', lot: 150 },
      { symbol: 'INDUSINDBK', name: 'IndusInd Bank', sector: 'Banking', cap: 'large', lot: 400 },
      { symbol: 'INDUSTOWER', name: 'Indus Towers', sector: 'Telecom', cap: 'mid', lot: 1600 },
      { symbol: 'INFY', name: 'Infosys', sector: 'IT', cap: 'large', lot: 400 },
      { symbol: 'INOXGREEN', name: 'Inox Green Energy', sector: 'Energy', cap: 'mid', lot: 2700 },
      { symbol: 'INOXWIND', name: 'Inox Wind', sector: 'Energy', cap: 'mid', lot: 1300 },
      { symbol: 'IOB', name: 'Indian Overseas Bank', sector: 'Banking', cap: 'mid', lot: 7500 },
      { symbol: 'IOC', name: 'Indian Oil Corp', sector: 'Energy', cap: 'mid', lot: 4850 },
      { symbol: 'IPCALAB', name: 'IPCA Laboratories', sector: 'Pharma', cap: 'mid', lot: 350 },
      { symbol: 'IRCTC', name: 'IRCTC', sector: 'Infra', cap: 'mid', lot: 500 },
      { symbol: 'IREDA', name: 'IREDA', sector: 'Energy', cap: 'mid', lot: 3000 },
      { symbol: 'IRFC', name: 'Indian Railway Finance Corp', sector: 'Finance', cap: 'mid', lot: 5000 },
      { symbol: 'ITC', name: 'ITC Limited', sector: 'FMCG', cap: 'large', lot: 1600 },
      { symbol: 'JBCHEPHARM', name: 'JB Chemicals & Pharma', sector: 'Pharma', cap: 'mid', lot: 250 },
      { symbol: 'JINDALSTEL', name: 'Jindal Steel & Power', sector: 'Metal', cap: 'mid', lot: 500 },
      { symbol: 'JIOFIN', name: 'Jio Financial Services', sector: 'Finance', cap: 'large', lot: 1500 },
      { symbol: 'JKCEMENT', name: 'JK Cement', sector: 'Infra', cap: 'mid', lot: 125 },
      { symbol: 'JSWENERGY', name: 'JSW Energy', sector: 'Energy', cap: 'mid', lot: 900 },
      { symbol: 'JSWSTEEL', name: 'JSW Steel', sector: 'Metal', cap: 'large', lot: 900 },
      { symbol: 'JUBLFOOD', name: 'Jubilant FoodWorks', sector: 'FMCG', cap: 'mid', lot: 1250 },
      { symbol: 'JYOTHYLAB', name: 'Jyothy Labs', sector: 'FMCG', cap: 'mid', lot: 850 },
      { symbol: 'KALYANKJIL', name: 'Kalyan Jewellers', sector: 'Retail', cap: 'mid', lot: 950 },
      { symbol: 'KAYNES', name: 'Kaynes Technology', sector: 'IT', cap: 'mid', lot: 200 },
      { symbol: 'KEI', name: 'KEI Industries', sector: 'Infra', cap: 'mid', lot: 125 },
      { symbol: 'KFINTECH', name: 'KFin Technologies', sector: 'Finance', cap: 'mid', lot: 400 },
      { symbol: 'KOTAKBANK', name: 'Kotak Mahindra Bank', sector: 'Banking', cap: 'large', lot: 400 },
      { symbol: 'KPITTECH', name: 'KPIT Technologies', sector: 'IT', cap: 'mid', lot: 350 },
      { symbol: 'L&TFH', name: 'L&T Finance', sector: 'Finance', cap: 'mid', lot: 5117 },
      { symbol: 'LALPATHLAB', name: 'Dr Lal Pathlab', sector: 'Pharma', cap: 'mid', lot: 200 },
      { symbol: 'LAURUSLABS', name: 'Laurus Labs', sector: 'Pharma', cap: 'mid', lot: 950 },
      { symbol: 'LICHSGFIN', name: 'LIC Housing Finance', sector: 'Finance', cap: 'mid', lot: 1000 },
      { symbol: 'LICI', name: 'LIC of India', sector: 'Insurance', cap: 'large', lot: 550 },
      { symbol: 'LODHA', name: 'Macrotech Developers', sector: 'Realty', cap: 'mid', lot: 500 },
      { symbol: 'LT', name: 'Larsen & Toubro', sector: 'Infra', cap: 'large', lot: 150 },
      { symbol: 'LTIM', name: 'LTIMindtree', sector: 'IT', cap: 'large', lot: 150 },
      { symbol: 'LTTS', name: 'L&T Technology Services', sector: 'IT', cap: 'mid', lot: 100 },
      { symbol: 'LUPIN', name: 'Lupin', sector: 'Pharma', cap: 'mid', lot: 425 },
      { symbol: 'M&M', name: 'Mahindra & Mahindra', sector: 'Auto', cap: 'large', lot: 350 },
      { symbol: 'MANAPPURAM', name: 'Manappuram Finance', sector: 'Finance', cap: 'mid', lot: 4000 },
      { symbol: 'MANKIND', name: 'Mankind Pharma', sector: 'Pharma', cap: 'mid', lot: 200 },
      { symbol: 'MANYAVAR', name: 'Vedant Fashions', sector: 'Retail', cap: 'mid', lot: 350 },
      { symbol: 'MARICO', name: 'Marico', sector: 'FMCG', cap: 'mid', lot: 800 },
      { symbol: 'MARUTI', name: 'Maruti Suzuki', sector: 'Auto', cap: 'large', lot: 100 },
      { symbol: 'MAXHEALTH', name: 'Max Healthcare', sector: 'Pharma', cap: 'mid', lot: 550 },
      { symbol: 'MAZDOCK', name: 'Mazagon Dock Shipbuilders', sector: 'Defence', cap: 'large', lot: 125 },
      { symbol: 'MCDOWELL-N', name: 'United Spirits', sector: 'FMCG', cap: 'mid', lot: 250 },
      { symbol: 'MCX', name: 'Multi Commodity Exchange', sector: 'Finance', cap: 'mid', lot: 200 },
      { symbol: 'METROPOLIS', name: 'Metropolis Healthcare', sector: 'Pharma', cap: 'mid', lot: 225 },
      { symbol: 'MFSL', name: 'Max Financial Services', sector: 'Insurance', cap: 'mid', lot: 500 },
      { symbol: 'MGL', name: 'Mahanagar Gas', sector: 'Energy', cap: 'mid', lot: 400 },
      { symbol: 'MOTHERSON', name: 'Motherson Sumi', sector: 'Auto', cap: 'mid', lot: 3650 },
      { symbol: 'MOTILALOFS', name: 'Motilal Oswal Financial', sector: 'Finance', cap: 'mid', lot: 200 },
      { symbol: 'MPHASIS', name: 'Mphasis', sector: 'IT', cap: 'mid', lot: 175 },
      { symbol: 'MRF', name: 'MRF Limited', sector: 'Auto', cap: 'mid', lot: 5 },
      { symbol: 'MUTHOOTFIN', name: 'Muthoot Finance', sector: 'Finance', cap: 'mid', lot: 275 },
      { symbol: 'NAM-INDIA', name: 'Nippon Life India AMC', sector: 'Finance', cap: 'mid', lot: 750 },
      { symbol: 'NATCOPHARM', name: 'Natco Pharma', sector: 'Pharma', cap: 'mid', lot: 400 },
      { symbol: 'NATIONALUM', name: 'National Aluminium', sector: 'Metal', cap: 'mid', lot: 3000 },
      { symbol: 'NAUKRI', name: 'Info Edge (Naukri)', sector: 'IT', cap: 'large', lot: 75 },
      { symbol: 'NAVINFLUOR', name: 'Navin Fluorine', sector: 'Chemicals', cap: 'mid', lot: 150 },
      { symbol: 'NESTLEIND', name: 'Nestle India', sector: 'FMCG', cap: 'large', lot: 40 },
      { symbol: 'NHPC', name: 'NHPC Limited', sector: 'Energy', cap: 'mid', lot: 6750 },
      { symbol: 'NMDC', name: 'NMDC Limited', sector: 'Metal', cap: 'mid', lot: 2250 },
      { symbol: 'NTPC', name: 'NTPC Limited', sector: 'Energy', cap: 'large', lot: 2925 },
      { symbol: 'NTPCGREEN', name: 'NTPC Green Energy', sector: 'Energy', cap: 'mid', lot: 3000 },
      { symbol: 'NYKAA', name: 'FSN E-Commerce (Nykaa)', sector: 'Retail', cap: 'mid', lot: 3500 },
      { symbol: 'OBEROIRLTY', name: 'Oberoi Realty', sector: 'Realty', cap: 'mid', lot: 400 },
      { symbol: 'OFSS', name: 'Oracle Financial', sector: 'IT', cap: 'mid', lot: 50 },
      { symbol: 'OIL', name: 'Oil India', sector: 'Energy', cap: 'mid', lot: 1400 },
      { symbol: 'ONGC', name: 'ONGC', sector: 'Energy', cap: 'large', lot: 3850 },
      { symbol: 'PAGEIND', name: 'Page Industries', sector: 'FMCG', cap: 'mid', lot: 15 },
      { symbol: 'PATANJALI', name: 'Patanjali Foods', sector: 'FMCG', cap: 'mid', lot: 900 },
      { symbol: 'PAYTM', name: 'One 97 Communications', sector: 'Retail', cap: 'mid', lot: 750 },
      { symbol: 'PEL', name: 'Piramal Enterprises', sector: 'Finance', cap: 'mid', lot: 375 },
      { symbol: 'PERSISTENT', name: 'Persistent Systems', sector: 'IT', cap: 'mid', lot: 100 },
      { symbol: 'PETRONET', name: 'Petronet LNG', sector: 'Energy', cap: 'mid', lot: 3000 },
      { symbol: 'PFC', name: 'Power Finance Corp', sector: 'Finance', cap: 'mid', lot: 1500 },
      { symbol: 'PHOENIXLTD', name: 'Phoenix Mills', sector: 'Realty', cap: 'mid', lot: 350 },
      { symbol: 'PIDILITIND', name: 'Pidilite Industries', sector: 'FMCG', cap: 'mid', lot: 250 },
      { symbol: 'PIIND', name: 'PI Industries', sector: 'Chemicals', cap: 'mid', lot: 125 },
      { symbol: 'PNB', name: 'Punjab National Bank', sector: 'Banking', cap: 'mid', lot: 8000 },
      { symbol: 'POLICYBZR', name: 'PB Fintech', sector: 'Retail', cap: 'mid', lot: 550 },
      { symbol: 'POLYCAB', name: 'Polycab India', sector: 'Infra', cap: 'mid', lot: 100 },
      { symbol: 'POONAWALLA', name: 'Poonawalla Fincorp', sector: 'Finance', cap: 'mid', lot: 1250 },
      { symbol: 'POWERGRID', name: 'Power Grid Corp', sector: 'Energy', cap: 'large', lot: 2700 },
      { symbol: 'PPLPHARMA', name: 'Piramal Pharma', sector: 'Pharma', cap: 'mid', lot: 2625 },
      { symbol: 'PRESTIGE', name: 'Prestige Estates', sector: 'Realty', cap: 'mid', lot: 500 },
      { symbol: 'PVRINOX', name: 'PVR INOX', sector: 'Media', cap: 'mid', lot: 350 },
      { symbol: 'RAJESHEXPO', name: 'Rajesh Exports', sector: 'Retail', cap: 'mid', lot: 1000 },
      { symbol: 'RAMCOCEM', name: 'Ramco Cements', sector: 'Infra', cap: 'mid', lot: 500 },
      { symbol: 'RATNAMANI', name: 'Ratnamani Metals & Tubes', sector: 'Metal', cap: 'mid', lot: 150 },
      { symbol: 'RBLBANK', name: 'RBL Bank', sector: 'Banking', cap: 'mid', lot: 3175 },
      { symbol: 'RECLTD', name: 'REC Limited', sector: 'Finance', cap: 'mid', lot: 1200 },
      { symbol: 'RELIANCE', name: 'Reliance Industries', sector: 'Energy', cap: 'large', lot: 250 },
      { symbol: 'RVNL', name: 'Rail Vikas Nigam', sector: 'Infra', cap: 'mid', lot: 1525 },
      { symbol: 'SAIL', name: 'Steel Authority', sector: 'Metal', cap: 'mid', lot: 4550 },
      { symbol: 'SAMMAANCAP', name: 'Sammaan Capital', sector: 'Finance', cap: 'mid', lot: 4300 },
      { symbol: 'SAPPHIRE', name: 'Sapphire Foods India', sector: 'FMCG', cap: 'mid', lot: 400 },
      { symbol: 'SBICARD', name: 'SBI Cards', sector: 'Finance', cap: 'mid', lot: 500 },
      { symbol: 'SBILIFE', name: 'SBI Life Insurance', sector: 'Insurance', cap: 'large', lot: 375 },
      { symbol: 'SBIN', name: 'State Bank of India', sector: 'Banking', cap: 'large', lot: 1500 },
      { symbol: 'SCHAEFFLER', name: 'Schaeffler India', sector: 'Auto', cap: 'mid', lot: 100 },
      { symbol: 'SHREECEM', name: 'Shree Cement', sector: 'Infra', cap: 'mid', lot: 25 },
      { symbol: 'SHRIRAMFIN', name: 'Shriram Finance', sector: 'Finance', cap: 'large', lot: 200 },
      { symbol: 'SIEMENS', name: 'Siemens', sector: 'Infra', cap: 'mid', lot: 75 },
      { symbol: 'SJVN', name: 'SJVN Limited', sector: 'Energy', cap: 'mid', lot: 4500 },
      { symbol: 'SOBHA', name: 'Sobha Limited', sector: 'Realty', cap: 'mid', lot: 500 },
      { symbol: 'SOLARINDS', name: 'Solar Industries India', sector: 'Defence', cap: 'mid', lot: 100 },
      { symbol: 'SONACOMS', name: 'Sona BLW Precision', sector: 'Auto', cap: 'mid', lot: 675 },
      { symbol: 'SRF', name: 'SRF Limited', sector: 'Chemicals', cap: 'mid', lot: 150 },
      { symbol: 'SUNPHARMA', name: 'Sun Pharma', sector: 'Pharma', cap: 'large', lot: 700 },
      { symbol: 'SUNTV', name: 'Sun TV Network', sector: 'Media', cap: 'mid', lot: 750 },
      { symbol: 'SUPREMEIND', name: 'Supreme Industries', sector: 'Infra', cap: 'mid', lot: 75 },
      { symbol: 'SUZLON', name: 'Suzlon Energy', sector: 'Energy', cap: 'mid', lot: 9025 },
      { symbol: 'SWIGGY', name: 'Swiggy', sector: 'Retail', cap: 'large', lot: 1300 },
      { symbol: 'SYNGENE', name: 'Syngene International', sector: 'Pharma', cap: 'mid', lot: 500 },
      { symbol: 'TATACHEM', name: 'Tata Chemicals', sector: 'Chemicals', cap: 'mid', lot: 500 },
      { symbol: 'TATACOMM', name: 'Tata Communications', sector: 'IT', cap: 'mid', lot: 500 },
      { symbol: 'TATACONSUM', name: 'Tata Consumer', sector: 'FMCG', cap: 'mid', lot: 500 },
      { symbol: 'TATAELXSI', name: 'Tata Elxsi', sector: 'IT', cap: 'mid', lot: 75 },
      { symbol: 'TATAMOTORS', name: 'Tata Motors', sector: 'Auto', cap: 'large', lot: 1425 },
      { symbol: 'TATAPOWER', name: 'Tata Power', sector: 'Energy', cap: 'mid', lot: 2700 },
      { symbol: 'TATASTEEL', name: 'Tata Steel', sector: 'Metal', cap: 'large', lot: 5500 },
      { symbol: 'TATATECH', name: 'Tata Technologies', sector: 'IT', cap: 'mid', lot: 800 },
      { symbol: 'TCS', name: 'Tata Consultancy', sector: 'IT', cap: 'large', lot: 175 },
      { symbol: 'TECHM', name: 'Tech Mahindra', sector: 'IT', cap: 'large', lot: 600 },
      { symbol: 'THERMAX', name: 'Thermax', sector: 'Infra', cap: 'mid', lot: 125 },
      { symbol: 'TIINDIA', name: 'Tube Investments of India', sector: 'Infra', cap: 'mid', lot: 100 },
      { symbol: 'TIMKEN', name: 'Timken India', sector: 'Auto', cap: 'mid', lot: 200 },
      { symbol: 'TITAN', name: 'Titan Company', sector: 'FMCG', cap: 'large', lot: 250 },
      { symbol: 'TORNTPHARM', name: 'Torrent Pharma', sector: 'Pharma', cap: 'mid', lot: 175 },
      { symbol: 'TORNTPOWER', name: 'Torrent Power', sector: 'Energy', cap: 'mid', lot: 400 },
      { symbol: 'TRENT', name: 'Trent Limited', sector: 'Retail', cap: 'large', lot: 100 },
      { symbol: 'TVSMOTOR', name: 'TVS Motor', sector: 'Auto', cap: 'mid', lot: 175 },
      { symbol: 'UBL', name: 'United Breweries', sector: 'FMCG', cap: 'mid', lot: 350 },
      { symbol: 'ULTRACEMCO', name: 'UltraTech Cement', sector: 'Infra', cap: 'large', lot: 100 },
      { symbol: 'UNIONBANK', name: 'Union Bank of India', sector: 'Banking', cap: 'mid', lot: 4000 },
      { symbol: 'UNITDSPR', name: 'United Spirits', sector: 'FMCG', cap: 'mid', lot: 400 },
      { symbol: 'UNOMINDA', name: 'Uno Minda', sector: 'Auto', cap: 'mid', lot: 550 },
      { symbol: 'UPL', name: 'UPL Limited', sector: 'Chemicals', cap: 'mid', lot: 650 },
      { symbol: 'VBL', name: 'Varun Beverages', sector: 'FMCG', cap: 'mid', lot: 300 },
      { symbol: 'VEDL', name: 'Vedanta Limited', sector: 'Metal', cap: 'mid', lot: 1550 },
      { symbol: 'VMM', name: 'Vishal Mega Mart', sector: 'Retail', cap: 'mid', lot: 1250 },
      { symbol: 'VOLTAS', name: 'Voltas', sector: 'FMCG', cap: 'mid', lot: 500 },
      { symbol: 'WAAREEENER', name: 'Waaree Energies', sector: 'Energy', cap: 'large', lot: 175 },
      { symbol: 'WELCORP', name: 'Welspun Corp', sector: 'Metal', cap: 'mid', lot: 750 },
      { symbol: 'WHIRLPOOL', name: 'Whirlpool of India', sector: 'FMCG', cap: 'mid', lot: 300 },
      { symbol: 'WIPRO', name: 'Wipro', sector: 'IT', cap: 'large', lot: 1500 },
      { symbol: 'YESBANK', name: 'Yes Bank', sector: 'Banking', cap: 'mid', lot: 31100 },
      { symbol: 'ZENSARTECH', name: 'Zensar Technologies', sector: 'IT', cap: 'mid', lot: 500 },
      { symbol: 'ZOMATO', name: 'Zomato', sector: 'Retail', cap: 'large', lot: 1950 },
      { symbol: 'ZYDUSLIFE', name: 'Zydus Lifesciences', sector: 'Pharma', cap: 'mid', lot: 550 }
    ];
  }

  // ── Get the full F&O universe (static + dynamically discovered from database via /api/equity-list) ──
  async getFNOUniverse() {
    if (this._dynamicUniverse) return this._dynamicUniverse;

    try {
      const res = await fetch('/api/equity-list');
      if (res.ok) {
        const data = await res.json();
        if (data.stocks && data.stocks.length > 0) {
          const staticList = this.getStaticFNOUniverse();
          const staticMap = new Map(staticList.map(s => [s.symbol, s]));

          this._dynamicUniverse = data.stocks.map(s => {
            const sym = s.tradingsymbol;
            const staticItem = staticMap.get(sym);
            return {
              symbol: sym,
              name: s.name || (staticItem ? staticItem.name : sym),
              sector: staticItem ? staticItem.sector : 'Other',
              cap: staticItem ? staticItem.cap : 'mid',
              lot: staticItem ? staticItem.lot : 1
            };
          });
          return this._dynamicUniverse;
        }
      }
    } catch (e) {
      console.warn('Failed to fetch F&O equity list from backend:', e);
    }

    return this.getStaticFNOUniverse();
  }

  // ── Synchronous version for dropdowns (uses cached or static) ──
  getFNOUniverseSync() {
    return this._dynamicUniverse || this.getStaticFNOUniverse();
  }

  // ── Refresh the dynamic universe from Kite API ──
  async refreshUniverse() {
    this._dynamicUniverse = null; // Clear cache
    return await this.getFNOUniverse();
  }

  // ── Fetch live OHLCV data from Kite API for a single stock ──
  async fetchStockData(stock, existingSnapshot = null) {
    const token = kiteAPI.getInstrumentToken(stock.symbol, 'NSE');
    if (!token) return null;

    // Use snapshot data if provided (prevents redundant quote calls)
    const ltp = existingSnapshot?.ltp || stock.ltp;
    const changePercent = existingSnapshot?.change_pct || stock.changePercent;
    const volume = existingSnapshot?.volume || 0;

    const to = new Date().toISOString().split('T')[0];
    const fromDate = new Date();
    fromDate.setDate(fromDate.getDate() - 90);
    const from = fromDate.toISOString().split('T')[0];

    // Historical data is still needed for indicators (RSI, MACD, etc.)
    const data = await kiteAPI.getHistoricalData(token, from, to, 'day');
    const candles = data.candles || data.data?.candles || data;

    if (!Array.isArray(candles) || candles.length < 20) return null;

    const closes = candles.map(c => c[4] || c.close);
    const highs = candles.map(c => c[2] || c.high);
    const lows = candles.map(c => c[3] || c.low);
    const volumes = candles.map(c => c[5] || c.volume);

    // If we have a snapshot, we can skip individual quote calls
    let optionsData = {
      ivPercentile: 0, pcr: 0, suggestedDelta: 0,
      oiChangePercent: 0, buildUp: 'none', oiWallBreached: false, ivHvRatio: 0
    };

    if (existingSnapshot) {
      optionsData.oiChangePercent = existingSnapshot.oi_change_pct || 0;
      
      const fut = existingSnapshot.futures || {};
      if (fut.oi) {
        if (changePercent > 0 && fut.oi_change > 0) optionsData.buildUp = 'long_buildup';
        else if (changePercent < 0 && fut.oi_change > 0) optionsData.buildUp = 'short_buildup';
        else if (changePercent > 0 && fut.oi_change < 0) optionsData.buildUp = 'short_covering';
        else if (changePercent < 0 && fut.oi_change < 0) optionsData.buildUp = 'long_unwinding';
      }
    }

    // ── PDH / PDL / Pivot Levels (from previous daily candle — zero extra API calls) ──
    let pdh = null, pdl = null, pdc = null, pivot = null, r1 = null, s1 = null, r2 = null, s2 = null;
    if (candles.length >= 2) {
      const pd = candles[candles.length - 2];
      pdh = pd[2] || pd.high   || null;
      pdl = pd[3] || pd.low    || null;
      pdc = pd[4] || pd.close  || null;
      if (pdh && pdl && pdc) {
        pivot = (pdh + pdl + pdc) / 3;
        r1    = 2 * pivot - pdl;
        s1    = 2 * pivot - pdh;
        r2    = pivot + (pdh - pdl);
        s2    = pivot - (pdh - pdl);
      }
    }

    return {
      ...stock, closes, highs, lows, volumes, ltp, changePercent,
      fundamentals: {}, sectorData: {}, optionsData,
      snapshot: existingSnapshot,
      pdh, pdl, pdc, pivot, r1, s1, r2, s2,
    };
  }

  // ── Run full scan using LIVE Kite API data ──
  async scan(mode = 'equity') {
    if (this._scanPromise) return this._scanPromise;

    this._scanPromise = (async () => {
      if (!kiteAPI.connected || kiteAPI.instruments.length === 0) {
        this.stocks = [];
        this.filtered = [];
        this.render();
        this._showConnectionError();
        throw new Error('Kite API not connected. Go to Settings → Connect first.');
      }

      const fnoList = await this.getFNOUniverse();
      const symbols = fnoList.map(s => s.symbol);

      // 1. Fetch Batch Snapshots FIRST (much faster than individual quotes)
      const resp = await kiteAPI.getBatchSnapshots(symbols);
      const snapshotsMap = resp.snapshots || {};
      
      // 2. Fetch Historical data in larger parallel batches
      const batchSize = 40; // Increased from 10
      const allStockData = [];

      for (let i = 0; i < fnoList.length; i += batchSize) {
        const batch = fnoList.slice(i, i + batchSize);
        const promises = batch.map(stock => {
          const snapshot = snapshotsMap[stock.symbol];
          return this.fetchStockData(stock, snapshot).catch(() => null);
        });
        
        const results = await Promise.all(promises);
        results.forEach(r => { if (r) allStockData.push(r); });

        document.dispatchEvent(new CustomEvent('scan-progress', {
          detail: { done: Math.min(i + batchSize, fnoList.length), total: fnoList.length }
        }));
      }

      if (allStockData.length === 0) {
        this.stocks = [];
        this.filtered = [];
        this.render();
        throw new Error('No data received from Kite API. Check your connection and instrument list.');
      }

      // Pass 1: preliminary score each stock (sector data not yet available)
      const pass1 = allStockData.map(stock => {
        const result = scoringEngine.scoreEquity(stock);
        return { ...stock, score: result.total, total: result.total, isBullishTrend: result.isBullishTrend };
      });


      // Compute sector relative strength from pass-1 results
      // This populates scoringEngine._sectorScores for use in pass 2
      scoringEngine.computeSectorScores(pass1);

      // ── Batch-fetch 15-min candles for intraday enrichment ──
      const allSymbols = allStockData.map(s => s.symbol);
      let intraday15m = {};
      try {
        const resp15 = await fetch('/api/intraday-candles', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbols: allSymbols.slice(0, 150) })
        });
        if (resp15.ok) intraday15m = await resp15.json();
      } catch (e) {
        console.warn('15-min candle fetch failed (non-fatal):', e.message);
      }

      const isExpiryWeek = this.isExpiryWeek();
      // Compute nifty bias from already-scored pass1 market breadth
      const niftyBias = this._computeNiftyBias(pass1);


      // Merge 15-min data + context into each stock
      allStockData.forEach(stock => {
        const cdata = intraday15m[stock.symbol];
        if (cdata && !cdata.error && cdata.closes && cdata.closes.length >= 5) {
          stock.closes15m  = cdata.closes;
          stock.highs15m   = cdata.highs   || [];
          stock.lows15m    = cdata.lows    || [];
          stock.volumes15m = cdata.volumes || [];
          stock.sessionVwap = this.computeSessionVwap(cdata);
          stock.rsi15m = scoringEngine.computeRSI(cdata.closes);
        }
        stock.isExpiryWeek = isExpiryWeek;
        stock.niftyBias    = niftyBias;
      });

      // Pass 2: re-score with all enriched data
      this.stocks = allStockData.map(stock => {
        const result = mode === 'equity'
          ? scoringEngine.scoreEquity(stock)
          : scoringEngine.scoreOptions(stock);
        return {
          ...stock, score: result.total, signal: result.direction,
          rsi: result.rsi, macd: result.macd, adx: result.adx,
          atr: result.atr, volRatio: result.volRatio, factors: result.factors,
          dailyTrend: this.computeDailyTrend(stock.closes)
        };
      });

      this.applyFilters();
      return this.filtered;
    })();

    try {
      return await this._scanPromise;
    } finally {
      this._scanPromise = null;
    }
  }

  _showConnectionError() {
    const tbody = document.getElementById('screener-body');
    if (tbody) {
      tbody.innerHTML = '<tr><td colspan="11" class="text-muted" style="text-align:center;padding:40px;">' +
        '⚠️ <strong>Kite API not connected.</strong><br>Go to Settings → Enter API Key & Access Token → Click Connect' +
        '</td></tr>';
    }
  }

  // ── Filters ──
  setFilter(key, value) {
    this.filters[key] = value;
    this.applyFilters();
  }

  applyFilters() {
    let list = [...this.stocks];
    if (this.filters.sector) list = list.filter(s => s.sector === this.filters.sector);
    if (this.filters.cap && this.filters.cap !== 'all') list = list.filter(s => s.cap === this.filters.cap);
    if (this.filters.signal) list = list.filter(s => s.signal.toLowerCase() === this.filters.signal.toLowerCase());

    switch (this.filters.sort) {
      case 'score': list.sort((a, b) => b.score - a.score); break;
      case 'change': list.sort((a, b) => b.changePercent - a.changePercent); break;
      case 'volume': list.sort((a, b) => {
        const aLast = a.volumes && a.volumes[a.volumes.length - 1] != null ? a.volumes[a.volumes.length - 1] : 0;
        const bLast = b.volumes && b.volumes[b.volumes.length - 1] != null ? b.volumes[b.volumes.length - 1] : 0;
        return bLast - aLast;
      }); break;
      case 'name': list.sort((a, b) => a.symbol.localeCompare(b.symbol)); break;
    }

    this.filtered = list;
    this.render();
  }

  // ── Render Table ──
  render() {
    const tbody = document.getElementById('screener-body');
    if (!tbody) return;

    if (this.filtered.length === 0) {
      tbody.innerHTML = '<tr><td colspan="11" class="text-muted" style="text-align:center;padding:30px;">No stocks match filters. Ensure Kite API is connected.</td></tr>';
      return;
    }

    tbody.innerHTML = this.filtered.map(s => {
      const chgClass = s.changePercent >= 0 ? 'text-green' : 'text-red';
      const signalTag = s.signal === 'BULLISH' ? 'tag-bullish' : s.signal === 'BEARISH' ? 'tag-bearish' : 'tag-neutral';
      const scoreClass = s.score >= 70 ? 'score-high' : s.score >= 40 ? 'score-medium' : 'score-low';
      const macdSignal = s.macd?.histogram > 0 ? '<span class="text-green">▲ Bull</span>' : '<span class="text-red">▼ Bear</span>';
      const vol = s.volumes ? s.volumes[s.volumes.length - 1] : 0;

      return `<tr>
        <td><span class="stock-name">${s.symbol}</span><br><span class="text-muted" style="font-family:var(--font-body);font-size:0.7rem;">${s.name || ''}</span></td>
        <td style="font-family:var(--font-body);">${s.sector}</td>
        <td>₹${s.ltp?.toFixed(2)}</td>
        <td class="${chgClass}" style="font-weight:600;">${s.changePercent >= 0 ? '+' : ''}${s.changePercent?.toFixed(2)}%</td>
        <td>${(vol / 100000).toFixed(1)}L</td>
        <td>${s.volRatio?.toFixed(1)}x</td>
        <td>${s.rsi?.toFixed(1)}</td>
        <td>${macdSignal}</td>
        <td><span class="tag ${signalTag}">${s.signal}</span></td>
        <td><span class="score-badge ${scoreClass}" style="width:36px;height:36px;font-size:0.8rem;">${s.score}</span></td>
        <td>
          <button class="btn btn-sm btn-secondary" onclick="app.viewStock('${s.symbol}')" title="View Chart">📊</button>
          <button class="btn btn-sm btn-secondary" onclick="app.scoreStock('${s.symbol}')" title="Score Detail">🧠</button>
          <button class="btn btn-sm btn-secondary" onclick="window.watchlist?.toggle('${s.symbol}', this);"
            title="${window.watchlist?.has(s.symbol) ? 'Remove from Watchlist' : 'Add to Watchlist'}">${window.watchlist?.has(s.symbol) ? '⭐' : '☆'}</button>
        </td>
      </tr>`;
    }).join('');
  }
}

window.equityScreener = new EquityScreener();
