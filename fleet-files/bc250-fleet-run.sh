#!/usr/bin/env bash
# bc250-fleet-run.sh — boot-time orchestrator for the BC-250 characterization appliance.
# Runs at boot (bc250-fleet.service). Resume-aware. Output goes to the console so the
# operator sees progress + the result without SSH. Idempotent: a board that's already
# fully characterized is a quick no-op; a board mid-sweep (watchdog reboot) resumes.
set -uo pipefail
SUITE=/opt/bc250-fleet

banner() { printf '\n========================================================\n  %s\n========================================================\n' "$*"; }

banner "BC-250 FLEET CHARACTERIZATION"

# --- guard: only run on an actual BC-250 (gfx1013) ---
is_bc250=0
lspci -nn 2>/dev/null | grep -qiE "1002:13fe" && is_bc250=1
for v in /sys/class/drm/card*/device/vendor; do
  [ "$(cat "$v" 2>/dev/null)" = "0x1002" ] && is_bc250=1
done
if [ "$is_bc250" != 1 ]; then
  echo "  No BC-250 GPU detected on this host — NOT characterizing. Safe to power off."
  exit 0
fi

# --- safety arm flag: sweep only auto-runs when armed (default armed by setup) ---
if [ ! -e "$SUITE/ARMED" ]; then
  echo "  Appliance is DISARMED ($SUITE/ARMED missing) — running Phase 1 (safe) only."
  python3 "$SUITE/characterize.py" || true
  python3 "$SUITE/characterize.py" --print
  exit 0
fi

modprobe sp5100_tco 2>/dev/null || true

echo "[*] Phase 1 — identify + CU map (safe) ..."
python3 "$SUITE/characterize.py" || true

echo "[*] Phase 2 — undervolt floor-hunt (resume-aware; may hang+reboot at the floor) ..."
python3 "$SUITE/floorhunt.py" || true

banner "RESULT"
python3 - "$SUITE" <<'PY'
import json, glob, os, sys
store = "/var/lib/bc250-fleet/boards.json"
try:
    boards = json.load(open(store))["boards"]
except Exception:
    print("  (no record yet)"); raise SystemExit
# current board by primary up NIC mac
mac = None
best = []
for p in sorted(glob.glob("/sys/class/net/*")):
    n = os.path.basename(p)
    if n == "lo" or n.startswith(("wl","docker","veth","br","virbr","tap")): continue
    try:
        m = open(p+"/address").read().strip(); st = open(p+"/operstate").read().strip()
    except OSError: continue
    if m and m != "00:00:00:00:00:00": best.append((st=="up", m))
best.sort(reverse=True)
mac = best[0][1] if best else None
b = boards.get(mac, {})
d = b.get("die", {}); u = b.get("undervolt") or {}
print("  MAC        : %s" % mac)
print("  die        : %-9s  max-safe CUs: %s/40" % (d.get("type","?"), d.get("max_safe_cus","?")))
print("  CU mask    : %s" % " ".join("%s=%s" % kv for kv in sorted(d.get("safe_cu_mask",{}).items())))
if u.get("status") == "complete":
    print("  undervolt  : 2000MHz @ %smV  (floor %s / stable %s, +%smV)  validated=%s"
          % (u.get("final_mv"), u.get("crash_floor_mv"), u.get("last_stable_mv"),
             u.get("margin_mv"), u.get("validated")))
    print("  STATUS     : COMPLETE")
else:
    print("  undervolt  : %s" % (u.get("status") or "PENDING"))
    print("  STATUS     : INCOMPLETE — re-run / check logs")
PY

banner "DONE — SAFE TO POWER OFF.  MOVE THE DRIVE TO THE NEXT BOARD."
