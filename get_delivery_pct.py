import datetime
import requests
import csv
import io
import sys

def get_delivery_data(symbol, date_obj=None):
    """
    Downloads the NSE Daily Full Bhavcopy and Deliverable data for a specific date,
    steps backward if it is a holiday/weekend, and prints the delivery statistics.
    """
    symbol = symbol.strip().upper()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }

    if date_obj is None:
        date_obj = datetime.date.today()

    max_attempts = 10
    attempts = 0
    response = None

    # Step backward to locate the latest available trading day's file
    while attempts < max_attempts:
        date_str = date_obj.strftime("%d%m%Y")
        url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"
        
        print(f"Checking data for {date_obj.strftime('%Y-%m-%d')}...", file=sys.stderr)
        r = requests.get(url, headers=headers)
        
        if r.status_code == 200:
            response = r
            break
        elif r.status_code == 404:
            # Weekend or trading holiday, step back 1 day
            date_obj -= datetime.timedelta(days=1)
            attempts += 1
        else:
            print(f"Error {r.status_code} while fetching data.", file=sys.stderr)
            break

    if not response:
        print("Failed to retrieve data for the last 10 days.", file=sys.stderr)
        return

    print(f"\nSuccessfully loaded data for: {date_obj.strftime('%Y-%m-%d')}")
    
    # Parse CSV content
    f = io.StringIO(response.text)
    reader = csv.reader(f)
    
    # Read and clean headers
    header = next(reader)
    header = [col.strip().upper() for col in header]
    
    # Locate column indices
    try:
        sym_idx = header.index("SYMBOL")
        series_idx = header.index("SERIES")
        close_idx = header.index("CLOSE_PRICE")
        traded_idx = header.index("TTL_TRD_QNTY")
        deliv_qty_idx = header.index("DELIV_QTY")
        deliv_pct_idx = header.index("DELIV_PER")
    except ValueError as e:
        print(f"Failed to find required columns in header: {e}", file=sys.stderr)
        return

    # Extract target symbol details
    match_found = False
    for row in reader:
        if not row:
            continue
        row = [val.strip() for val in row]
        if len(row) <= max(sym_idx, series_idx):
            continue
            
        row_sym = row[sym_idx].upper()
        row_series = row[series_idx].upper()
        
        if row_sym == symbol and row_series == "EQ":
            close_price = row[close_idx]
            traded_qty = int(row[traded_idx])
            deliv_qty = int(row[deliv_qty_idx])
            deliv_pct = float(row[deliv_pct_idx])
            
            print("=" * 45)
            print(f"  NSE Delivery Details for: {symbol}")
            print("=" * 45)
            print(f"  Close Price     : ₹{close_price}")
            print(f"  Traded Qty      : {traded_qty:,}")
            print(f"  Deliverable Qty : {deliv_qty:,}")
            print(f"  Delivery %      : {deliv_pct}%")
            print("=" * 45)
            match_found = True
            break

    if not match_found:
        print(f"Symbol '{symbol}' (EQ series) not found in the bhavcopy.", file=sys.stderr)

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "KFINTECH"
    get_delivery_data(target)
