#!/bin/bash
# ╔══════════════════════════════════════════════════════╗
# ║   TradeSignal — Git Secrets Purge Helper             ║
# ║   This script resets Git history to remove .env      ║
# ╚══════════════════════════════════════════════════════╝

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &>/dev/null && pwd )"
cd "$DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}✅ $*${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }

# 1. Back up .env file
info "Backing up .env file..."
cp "$DIR/.env" "$DIR/../env_backup"

# 2. Delete local Git history
info "Deleting existing Git history (.git)..."
rm -rf "$DIR/.git"

# 3. Re-initialize Git
info "Initializing fresh Git repository..."
git init

# 4. Configure .gitignore
if ! grep -qF ".env" "$DIR/.gitignore" 2>/dev/null; then
    info "Adding .env to .gitignore..."
    echo "" >> "$DIR/.gitignore"
    echo "# Environment secrets" >> "$DIR/.gitignore"
    echo ".env" >> "$DIR/.gitignore"
fi

# 5. Create initial commit on 'main' branch
info "Creating initial clean commit..."
git checkout -b main 2>/dev/null || git branch -M main
git add -A
git commit -m "Initial commit (secrets purged)"

# 6. Re-link GitHub remote
info "Re-linking remote repository..."
git remote add origin https://github.com/novatechnolab/TradeSignal.git

# 7. Restore the backup .env file
info "Restoring .env backup..."
mv "$DIR/../env_backup" "$DIR/.env"

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Local Git history reset successfully!${NC}"
echo -e "${YELLOW}  Next step (Run this command manually to update GitHub):${NC}"
echo -e "  ${GREEN}git push origin main --force${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Self-destruct: remove this script
rm -- "$0"
