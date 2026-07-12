import re

# Read the Master Backup (VS Code History)
with open("/home/rajk/.config/Code/User/History/-6979f222/UiKS.js", "r") as f:
    master_js = f.read()

# 1. Add multi-chart to titles
if "'multi-chart': 'Multi-Chart Tracking'" not in master_js:
    master_js = master_js.replace("      'trade-cockpit': 'Trade Cockpit',", "      'trade-cockpit': 'Trade Cockpit',\n      'multi-chart': 'Multi-Chart Tracking',")

# 2. Add multiChartManager.init()
if "multiChartManager.init()" not in master_js:
    master_js = master_js.replace("      tradeCockpit.init();\n    }", "      tradeCockpit.init();\n    }\n    if (page === 'multi-chart' && window.multiChartManager) {\n      multiChartManager.init();\n    }")

# 3. Add mcStockList datalist population
if "mc-stock-list" not in master_js:
    mc_dropdown_code = """    // Multi-Chart Dropdown (Datalist)
    const mcStockList = document.getElementById('mc-stock-list');
    if (mcStockList) {
      mcStockList.innerHTML = '';
      ['NIFTY', 'BANKNIFTY'].forEach(s => {
        const o = document.createElement('option'); o.value = s; mcStockList.appendChild(o);
      });
      sorted.forEach(s => {
        const o = document.createElement('option'); o.value = s.symbol; o.text = s.name; mcStockList.appendChild(o);
      });
    }

    // Also populate options chain symbol dropdown"""
    master_js = master_js.replace("// Also populate options chain symbol dropdown", mc_dropdown_code)

# 4. Handle Search Dropdown
if "mc-stock-select" not in master_js:
  search_handler = """              item.addEventListener('click', () => {
                const sym = item.dataset.symbol;
                if (this.currentPage === 'multi-chart') {
                  const mcInput = document.getElementById('mc-stock-select');
                  if (mcInput) mcInput.value = sym;
                } else if (this.currentPage === 'scoring') {
                  this.scoreStock(sym);
                } else {
                  this.viewStock(sym);
                }
                searchInput.value = '';
                searchDropdown.style.display = 'none';
              });"""
  
  # Search for the specific existing click handler in the backup
  match = re.search(r'item\.addEventListener\(\'click\', \(\) => \{\s*const sym = item\.dataset\.symbol;\s*this\.viewStock\(sym\);\s*searchInput\.value = \'\';\s*searchDropdown\.style\.display = \'none\';\s*\}\);', master_js)
  if match:
      master_js = master_js.replace(match.group(0), search_handler)
  else:
      # If viewStock logic differs, try a more resilient replace
      match_alt = re.search(r'item\.addEventListener\(\'click\', \(\) => \{\s*const sym = item\.dataset\.symbol;[\s\w()]+;.*?\}\);', master_js, re.DOTALL)
      if match_alt:
          master_js = master_js.replace(match_alt.group(0), search_handler)

with open("app/js/app.js", "w") as f:
    f.write(master_js)

print("JS Apocalypse Averted!")
