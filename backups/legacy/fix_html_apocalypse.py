import re

with open("app/index.html", "r") as f:
    current_html = f.read()

# Extract Multi-Chart Page Block
mc_match = re.search(r'(<div id="page-multi-chart" class="page">.*?</div>\s*</div>\s*</div>\s*</div>)', current_html, re.DOTALL)
if not mc_match:
    # Try a looser match
    mc_match = re.search(r'(<div id="page-multi-chart" class="page">.*?(?:<div class="modal-overlay"|<script))', current_html, re.DOTALL)

mc_page_html = mc_match.group(1).split('<!-- Add Alert Modal -->')[0]
if '</div>' not in mc_page_html[-15:]:
    mc_page_html = current_html[current_html.find('<div id="page-multi-chart" class="page">'):current_html.find('<!-- Add Alert Modal -->')]
    
# Extract Multi-Chart Nav Item
nav_match = re.search(r'(<div class="nav-item"\s+data-page="multi-chart".*?</div>)', current_html, re.DOTALL)
mc_nav_html = nav_match.group(1)

# Now Read the Master Backup (VS Code History)
with open("/home/rajk/.config/Code/User/History/-400d7e9b/bwAX.html", "r") as f:
    master_html = f.read()

# Insert Nav Item
if 'data-page="multi-chart"' not in master_html:
    master_html = master_html.replace('data-page="screener"', mc_nav_html + '\n        <div class="nav-item" data-page="screener"')

# Insert Page Block
if 'id="page-multi-chart"' not in master_html:
    # Find the end of main-content (usually right before <!-- Add Alert Modal -->)
    # The master_html should have <!-- Add Alert Modal -->
    if '<!-- Add Alert Modal -->' in master_html:
        master_html = master_html.replace('<!-- Add Alert Modal -->', mc_page_html + '\n  <!-- Add Alert Modal -->')
    else:
        # Just append before closing body scripts
        master_html = master_html.replace('  <!-- Scripts', mc_page_html + '\n  <!-- Scripts')

# 3. Insert Scripts! (Make sure multi-chart.js is deferred)
if '<script src="js/multi-chart.js" defer></script>' not in master_html:
    master_html = master_html.replace('<script src="js/app.js" defer></script>', '<script src="js/multi-chart.js" defer></script>\n  <script src="js/app.js" defer></script>')

with open("app/index.html", "w") as f:
    f.write(master_html)

print("HTML Apocalypse Averted!")
