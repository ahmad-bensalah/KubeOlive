#!/bin/bash
# =============================================================================
# recover.sh — Kubeflow cluster recovery script
# Run from your LOCAL machine after a power cut / Proxmox reboot
#
# Usage:
#   chmod +x recover.sh
#   ./recover.sh
# =============================================================================

set -uo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
CONTROL_PLANE="ubuntu@10.110.188.144"
SSH_KEY="$HOME/.ssh/pfa_bouri_terraform"
SSH_CMD="ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes"
KUBECTL="$SSH_CMD $CONTROL_PLANE kubectl"

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()     { echo -e "${CYAN}[$(date '+%H:%M:%S')]${NC} $*"; }
success() { echo -e "${GREEN}[$(date '+%H:%M:%S')] ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠  $*${NC}"; }
section() { echo -e "\n${BOLD}${CYAN}$*${NC}\n"; }

# ── Safe integer: strips whitespace/newlines, defaults to 0 if empty ─────────
safe_int() { echo "${1}" | tr -d '[:space:]' | grep -E '^[0-9]+$' || echo "0"; }

# =============================================================================
# PHASE 0 — API server + nodes
# =============================================================================

wait_for_api_server() {
  section "════ Phase 0a: Waiting for Kubernetes API server ════"
  local attempts=0
  until $KUBECTL cluster-info &>/dev/null; do
    attempts=$(( attempts + 1 ))
    warn "API server not ready yet (attempt $attempts) — retrying in 10s..."
    sleep 10
  done
  success "API server is responding."
}

wait_for_nodes() {
  section "════ Phase 0b: Waiting for all nodes to be Ready ════"
  while true; do
    local out
    out=$($KUBECTL get nodes --no-headers 2>/dev/null || true)

    local total not_ready
    total=$(safe_int "$(echo "$out" | grep -c '.' || true)")
    # NotReady nodes explicitly have "NotReady" in the STATUS column
    not_ready=$(safe_int "$(echo "$out" | grep -c 'NotReady' || true)")

    if [ "$total" -gt 0 ] && [ "$not_ready" -eq 0 ]; then
      success "All $total nodes are Ready."
      $KUBECTL get nodes
      break
    fi
    warn "Nodes not all Ready ($not_ready/$total not ready) — retrying in 10s..."
    sleep 10
  done
}

# =============================================================================
# PRINT STATUS
# =============================================================================

print_status() {
  local ns=$1
  echo ""
  log "  Pods:"
  $KUBECTL get pods -n "$ns" 2>/dev/null || true
  echo ""
  log "  Deployments:"
  $KUBECTL get deployments -n "$ns" 2>/dev/null || true
  echo ""
  log "  DaemonSets:"
  $KUBECTL get daemonsets -n "$ns" 2>/dev/null || true
}

# =============================================================================
# CLEANUP — delete ghost/Unknown, CrashLoopBackOff, Error, OOMKilled pods
#
# KEY FIX: Kubernetes never sets status.phase=Unknown on stuck pods after a
# node failure — the phase stays "Running" while the STATUS *column* shows
# "Unknown". Using --field-selector=status.phase=Unknown always returns 0
# results. Instead we parse column 3 of `kubectl get pods --no-headers`.
# =============================================================================

cleanup_namespace() {
  local ns=$1
  local pods
  pods=$($KUBECTL get pods -n "$ns" --no-headers 2>/dev/null || true)

  # ── Ghost pods: STATUS column = "Unknown" ─────────────────────────────────
  local ghost_pods
  ghost_pods=$(echo "$pods" | awk '$3 ~ /^Unknown/ {print $1}')

  if [ -n "$ghost_pods" ]; then
    warn "  Ghost (Unknown) pods in $ns — force deleting:"
    for pod in $ghost_pods; do
      warn "    → $pod"
      $KUBECTL delete pod "$pod" -n "$ns" --force --grace-period=0 2>/dev/null || true
    done
  else
    log "  No ghost pods in $ns."
  fi

  # ── CrashLoopBackOff pods ─────────────────────────────────────────────────
  local crash_pods
  crash_pods=$(echo "$pods" | awk '$3 == "CrashLoopBackOff" {print $1}')

  if [ -n "$crash_pods" ]; then
    warn "  CrashLoopBackOff pods in $ns — deleting:"
    for pod in $crash_pods; do
      warn "    → $pod"
      $KUBECTL delete pod "$pod" -n "$ns" --force --grace-period=0 2>/dev/null || true
    done
  else
    log "  No CrashLoopBackOff pods in $ns."
  fi

  # ── Error / OOMKilled / CreateContainerError pods ─────────────────────────
  local bad_pods
  bad_pods=$(echo "$pods" | awk '$3 ~ /^(Error|OOMKilled|CreateContainerError|ImagePullBackOff|ErrImagePull)$/ {print $1}')

  if [ -n "$bad_pods" ]; then
    warn "  Error/OOMKilled/CreateContainerError pods in $ns — deleting:"
    for pod in $bad_pods; do
      warn "    → $pod"
      $KUBECTL delete pod "$pod" -n "$ns" --force --grace-period=0 2>/dev/null || true
    done
  fi
}

# =============================================================================
# HEALTH CHECK — count genuinely broken resources
#
# KEY FIX: Exclude "Terminating" — pods go Terminating right after deletion
# and would spike the counter, causing an infinite wait loop. Also only flag
# deployments/daemonsets where AVAILABLE=0 (truly down), not just rolling.
# =============================================================================

count_broken() {
  local ns=$1

  local pods deploys ds
  pods=$(    $KUBECTL get pods        -n "$ns" --no-headers 2>/dev/null || true)
  deploys=$( $KUBECTL get deployments -n "$ns" --no-headers 2>/dev/null || true)
  ds=$(      $KUBECTL get daemonsets  -n "$ns" --no-headers 2>/dev/null || true)

  # Terminal bad pod states only — NOT Terminating, PodInitializing, Init:
  local bp
  bp=$(safe_int "$(echo "$pods" | awk '$3 ~ /^(Unknown|CrashLoopBackOff|OOMKilled|Error|CreateContainerError|ImagePullBackOff|ErrImagePull)$/ {c++} END{print c+0}' || true)")

  # Deployments: flag only if AVAILABLE=0 and DESIRED>0 (completely down)
  local bd
  bd=$(safe_int "$(echo "$deploys" | awk '{split($2,a,"/"); des=a[2]+0; avail=$5+0; if(des>0 && avail==0) c++} END{print c+0}')")

  # DaemonSets: flag only if READY=0 and DESIRED>0
  local bds
  bds=$(safe_int "$(echo "$ds" | awk '{des=$2+0; ready=$4+0; if(des>0 && ready==0) c++} END{print c+0}')")

  echo $(( bp + bd + bds ))
}

# =============================================================================
# WAIT — poll until healthy; re-clean every 4 ticks to catch new bad pods
# =============================================================================

wait_healthy() {
  local ns=$1 label=$2
  log "  Waiting for all resources in $ns to be healthy..."
  local tick=0
  while true; do
    local broken
    broken=$(safe_int "$(count_broken "$ns")")

    if [ "$broken" -eq 0 ]; then
      success "$label: all resources healthy."
      return 0
    fi

    tick=$(( tick + 1 ))

    # Every 4 ticks (~60s): print detail and re-run cleanup
    # This handles new CrashLoopBackOff pods that appear during recovery
    if (( tick % 4 == 0 )); then
      warn "  Still $broken resource(s) not ready in $ns after $(( tick * 15 ))s — re-cleaning..."
      $KUBECTL get pods -n "$ns" --no-headers 2>/dev/null \
        | awk '$3 ~ /^(Unknown|CrashLoopBackOff|OOMKilled|Error|CreateContainerError)$/ {printf "    pod/%-44s %s\n", $1, $3}' || true
      $KUBECTL get deployments -n "$ns" --no-headers 2>/dev/null \
        | awk '{split($2,a,"/"); if(a[2]+0>0 && $5+0==0) printf "    deploy/%-42s avail=%s\n", $1, $5}' || true
      $KUBECTL get daemonsets -n "$ns" --no-headers 2>/dev/null \
        | awk '{if($2+0>0 && $4+0==0) printf "    ds/%-46s ready=%s/%s\n", $1, $4, $2}' || true
      cleanup_namespace "$ns"
    else
      warn "  $ns: $broken resource(s) not ready — waiting 15s..."
    fi

    sleep 15
  done
}

# =============================================================================
# RECOVER
# =============================================================================

recover_namespace() {
  local ns=$1 label=$2

  section "════ $label ════"
  log "State BEFORE recovery:"
  print_status "$ns"

  log "Cleanup pass..."
  cleanup_namespace "$ns"

  # Brief pause so deletions propagate before the first health poll
  sleep 10

  wait_healthy "$ns" "$label"

  success "$label is fully healthy."
}

# =============================================================================
# MAIN
# =============================================================================

echo ""
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}${CYAN}   Kubeflow Cluster Recovery                        ${NC}"
echo -e "${BOLD}${CYAN}   Target : $CONTROL_PLANE              ${NC}"
echo -e "${BOLD}${CYAN}   Started: $(date)           ${NC}"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════${NC}"
echo ""

wait_for_api_server
wait_for_nodes

recover_namespace "longhorn-system" "Phase 1 — Longhorn (storage)"
recover_namespace "kubeflow"        "Phase 2 — Kubeflow"

echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}${GREEN}   Recovery complete — $(date)${NC}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════${NC}"
echo ""
log "Final node status:"
$KUBECTL get nodes
echo ""
log "Remaining issues in longhorn-system:"
$KUBECTL get pods -n longhorn-system --no-headers 2>/dev/null \
  | grep -E 'Unknown|CrashLoopBackOff|Error|OOMKilled|CreateContainerError' \
  || echo "  None."
echo ""
log "Remaining issues in kubeflow:"
$KUBECTL get pods -n kubeflow --no-headers 2>/dev/null \
  | grep -E 'Unknown|CrashLoopBackOff|Error|OOMKilled|CreateContainerError' \
  || echo "  None."
