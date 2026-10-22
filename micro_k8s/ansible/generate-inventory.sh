#!/usr/bin/env bash
# =============================================================================
# Regenerate inventory.ini from group_vars/all.yml + inventory.j2
# =============================================================================
# Run this script whenever you change an IP, add a node, or change vm_user
# in group_vars/all.yml.
#
# Usage:
#   ./generate-inventory.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> Rendering inventory.j2 → inventory.ini ..."
ansible-playbook generate_inventory.yml

echo ""
echo "==> Done. Current inventory.ini:"
echo "------------------------------------------------------------"
cat inventory.ini
