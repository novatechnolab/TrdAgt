with open("app/index.html", "r") as f:
    text = f.read()

# Fix the scripts section exactly as it was
scripts_perfect = """  <!-- Scripts (defer = non-blocking, loads after HTML parse) -->
  <script src="js/lightweight-charts.standalone.production.js" defer></script>
  <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
  <script src="js/kite-api.js" defer></script>
  <script src="js/technical-indicators.js" defer></script>
  <script src="js/gap-analysis-engine.js" defer></script>
  <script src="js/scoring-engine.js" defer></script>
  <script src="js/options-chain.js" defer></script>
  <script src="js/charts.js" defer></script>
  <script src="js/alerts.js" defer></script>
  <script src="js/trade-journal.js" defer></script>
  <script src="js/paper-trading.js" defer></script>
  <script src="js/research-ai.js" defer></script>
  <script src="js/fno-session-analyzer.js" defer></script>
  <script src="js/equity-screener.js" defer></script>
  <script src="js/analysis.js" defer></script>
  <script src="js/watchlist.js" defer></script>
  <script src="js/strategy-builder.js" defer></script>
  <script src="js/backtester.js" defer></script>
  <script src="js/portfolio.js" defer></script>
  <script src="js/news-feed.js" defer></script>
  <script src="js/live-movers.js" defer></script>
  <script src="js/index-movers.js" defer></script>
  <script src="js/chart-signals.js" defer></script>
  <script src="js/reco-tracker.js" defer></script>
  <script src="js/smc-dashboard.js" defer></script>
  <script src="js/entry-validator.js" defer></script>
  <script src="js/trade-tracker.js" defer></script>
  <script src="js/trade-cockpit.js" defer></script>
  <script src="js/multi-chart.js" defer></script>
  <script src="js/app.js" defer></script>
</body>
</html>"""

import re
text = re.sub(r'  <!-- Scripts.*</html>', scripts_perfect, text, flags=re.DOTALL)

# Add nav items if missing
nav_items = """        <div class="nav-item active" data-page="dashboard" id="nav-dashboard">
          <span class="nav-icon">🏠</span>
          <span>Dashboard</span>
        </div>
        <div class="nav-item" data-page="multi-chart" id="nav-multi-chart">
          <span class="nav-icon">🔲</span>
          <span>Multi-Chart Tracking</span>
        </div>
        <div class="nav-item" data-page="screener" id="nav-screener">"""

if "data-page=\"multi-chart\"" not in text:
    text = text.replace("""        <div class="nav-item active" data-page="dashboard" id="nav-dashboard">
          <span class="nav-icon">🏠</span>
          <span>Dashboard</span>
        </div>
        <div class="nav-item" data-page="screener" id="nav-screener">""", nav_items)

# Add multi chart page container at the bottom before modal
multi_chart_page = """      <!-- ══════════════════════════════════════════════════════════════
           Multi-Chart Tracking
      ══════════════════════════════════════════════════════════════ -->
      <div id="page-multi-chart" class="page">
        <div class="flex" style="justify-content:space-between; align-items:center; margin-bottom:16px;">
          <div>
            <h2 style="margin:0;">🔲 Multi-Chart Tracking</h2>
            <p class="text-muted" style="margin:0;">Track up to 4 interactive F&O charts simultaneously</p>
          </div>
          <div class="flex gap-8">
            <input list="mc-stock-list" id="mc-stock-select" class="form-input" placeholder="Select or Search F&O Stock..." style="min-width:200px;">
            <datalist id="mc-stock-list">
              <!-- Populated dynamically -->
            </datalist>
            <input type="date" id="mc-date-select" class="form-input">
            <button class="btn btn-primary" id="mc-add-chart-btn">➕ Add Chart</button>
          </div>
        </div>

        <div id="mc-grid" class="multi-chart-grid layout-1x1">
          <!-- Dynamically inserted chart boxes -->
        </div>
      </div>

  <!-- Add Alert Modal -->"""

if "id=\"page-multi-chart\"" not in text:
    text = text.replace("  <!-- Add Alert Modal -->", multi_chart_page)


with open("app/index.html", "w") as f:
    f.write(text)

print("Done restoring index.html")
