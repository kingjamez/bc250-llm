#!/usr/bin/env python3
"""bc250-fleet-floorhunt.py — BC-250 fleet suite, Phase 2: undervolt floor-hunt.

Per-board (MAC-keyed) automated 2000 MHz voltage floor-finder. Hang-resilient:
  - the SP5100 hardware watchdog is armed ONCE and kept armed across the ENTIRE
    sweep (petted through tests AND setup, disarmed only on clean completion), so a
    HARD lock in ANY window freezes this process -> no pet -> watchdog fires -> reboot.
  - a SOFT GPU hang (VK_ERROR_DEVICE_LOST / dmesg reset) is caught in-process.
  - silent corruption is caught by comparing stress --verify checksum to a golden
    captured at a known-safe voltage.
  - every risky step is checkpointed (fsync) so a watchdog reboot resumes exactly
    where it left off (the interrupted voltage = the crash floor).

Records into boards.json (the `undervolt` field) + emits a governor curve. Run as root.
"""
import struct, os, json, subprocess, glob, datetime, argparse, time, fcntl, re, sys

STORE  = "/var/lib/bc250-fleet/boards.json"
CKPT   = "/var/lib/bc250-fleet/checkpoint.json"
GOVCFG = "/etc/cyan-skillfish-governor-smu/config.toml"
STRESS = os.environ.get("BC250_STRESS", "/opt/bc250-fleet/stress.sh")
WD_DEV = "/dev/watchdog"
WDIOC_SETTIMEOUT = 0xc0045706
TESTLOG = "/tmp/floorhunt_test.log"

OFFSETS = [(2000, 0), (1850, -30), (1700, -40), (1600, -50),
           (1500, -60), (1175, -110), (1000, -160), (500, -260)]
VMIN = 600


def log(msg):
    print("[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def primary_mac():
    cands = []
    for p in sorted(glob.glob("/sys/class/net/*")):
        n = os.path.basename(p)
        if n == "lo" or n.startswith(("wl", "docker", "veth", "br", "virbr", "tap")):
            continue
        try:
            mac = open(p + "/address").read().strip()
            st = open(p + "/operstate").read().strip()
        except OSError:
            continue
        if mac and mac != "00:00:00:00:00:00":
            cands.append((st == "up", mac))
    cands.sort(reverse=True)
    return cands[0][1] if cands else None


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def dmesg_faults():
    r = sh("dmesg 2>/dev/null | grep -icE 'amdgpu.*(reset|fault|hang|timeout|GPU reset)'")
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


# ---------------- watchdog (tolerant of a missing device) ----------------
class Watchdog:
    def __init__(self, timeout):
        self.fd = None
        if not os.path.exists(WD_DEV):
            return
        try:
            self.fd = os.open(WD_DEV, os.O_WRONLY)
            fcntl.ioctl(self.fd, WDIOC_SETTIMEOUT, struct.pack("i", int(timeout)))
        except OSError:
            self.fd = None

    def pet(self):
        if self.fd is None:
            return
        try:
            os.write(self.fd, b"1")
        except OSError:
            pass

    def disarm(self):
        if self.fd is None:
            return
        try:
            os.write(self.fd, b"V")   # magic close -> clean stop (nowayout=0)
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None


def ensure_watchdog():
    if not os.path.exists(WD_DEV):
        sh("modprobe sp5100_tco")
        time.sleep(1)
    return os.path.exists(WD_DEV)


# ---------------- governor ----------------
def write_gov_curve(v2000, throttle, maxf=2000):
    pts = sorted((f, max(VMIN, v2000 + off)) for f, off in OFFSETS)
    body = ('[timing.intervals]\nsample = 250\nadjust = 100_000\n'
            '[gpu-usage]\nfix-metrics = true\nmethod = "busy-flag"\nflush-every = 10\n'
            '[gpu]\nset-method = "smu"\n[dbus]\nenabled = false\n'
            '[frequency-range]\nmin = 1000\nmax = %d\n'
            '[timing.ramp-rates]\nnormal = 1\nburst = 50\n'
            '[timing]\nburst-samples = 60\ndown-events = 5\n'
            '[frequency-thresholds]\nadjust = 10\n'
            '[load-target]\nupper = 0.65\nlower = 0.50\n'
            '[temperature]\nthrottling = %d\nthrottling_recovery = %d\n'
            % (maxf, throttle, throttle - 8))
    for f, v in pts:
        body += "[[safe-points]]\nfrequency = %d\nvoltage = %d\n" % (f, v)
    with open(GOVCFG, "w") as fp:
        fp.write(body)
    sh("systemctl restart cyan-skillfish-governor-smu")
    time.sleep(3)
    return sh("systemctl is-active cyan-skillfish-governor-smu").stdout.strip() == "active"


def apply_cu_mask(mask):
    for sh_name, mhex in sorted(mask.items()):
        m = int(mhex, 16)
        se, shi = int(sh_name[2]), int(sh_name[6])
        for w in range(5):
            if not (m >> w) & 1:
                sh("/usr/local/bin/bc250-cu-live-manager --yes disable-wgp %d.%d.%d" % (se, shi, w))
    sh("/usr/local/bin/bc250-cu-live-manager --yes write-service-table")


# ---------------- checkpoint ----------------
def write_ckpt(mac, test_v, last_stable, golden, state):
    save_json(CKPT, {"mac": mac, "test_v": test_v, "last_stable": last_stable,
                     "golden": golden, "state": state,
                     "ts": datetime.datetime.now().isoformat(timespec="seconds")})


def clear_ckpt():
    try:
        os.remove(CKPT)
    except OSError:
        pass


# ---------------- one voltage test (uses a shared, already-armed watchdog) ----------------
def run_test(wd, duration, golden):
    """Run stress --verify at the currently-applied voltage. Returns (outcome, checksum).
    outcome in {pass, corrupt, hang}. Pets the shared watchdog throughout; never disarms it."""
    f0 = dmesg_faults()
    deadline = time.time() + duration + 90
    with open(TESTLOG, "w") as lf:
        proc = subprocess.Popen(["bash", STRESS, "--duration", str(duration), "--verify"],
                                stdout=lf, stderr=subprocess.STDOUT)
        last_pet = 0.0
        while proc.poll() is None:
            now = time.time()
            if now - last_pet > 8:
                wd.pet()
                last_pet = now
            if now > deadline:
                proc.kill()
                break
            time.sleep(1)
    wd.pet()
    out = open(TESTLOG).read()
    ret = proc.returncode
    m = re.search(r"verify_checksum=(0x[0-9a-f]+)", out)
    ck = m.group(1) if m else None
    fault = dmesg_faults() > f0
    bad = (ret != 0) or fault or ("VK_ERROR" in out) or ("DEVICE_LOST" in out) \
        or ("=-4 " in out) or ("done dispatches" not in out)
    if bad:
        return "hang", ck
    if not ck:
        return "hang", ck
    if golden and ck != golden:
        return "corrupt", ck
    return "pass", ck


# ---------------- finalize / record ----------------
def finalize(wd, store, rec, mac, crash, last_stable, golden, args):
    final = (crash + args.margin) if crash is not None else last_stable
    log("FLOOR: crash=%s last_stable=%s -> final=%dmV (margin %d)"
        % (crash, last_stable, final, args.margin))
    write_gov_curve(final, args.throttle)
    wd.pet()
    outcome, ck = run_test(wd, max(args.duration, 60), golden)
    validated = (outcome == "pass")
    if not validated:
        log("WARN: final %dmV failed validation (%s) -> +%d" % (final, outcome, args.step))
        final += args.step
        write_gov_curve(final, args.throttle)
        wd.pet()
        outcome, ck = run_test(wd, max(args.duration, 60), golden)
        validated = (outcome == "pass")
    curve = {str(f): max(VMIN, final + off) for f, off in OFFSETS}
    rec["undervolt"] = {
        "freq_mhz": 2000, "crash_floor_mv": crash, "last_stable_mv": last_stable,
        "margin_mv": args.margin, "final_mv": final, "throttle_c": args.throttle,
        "curve_mv": curve, "golden_checksum": golden, "validated": validated,
        "status": "complete",
        "tested_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    rec["status"] = "uv_complete"
    store["boards"][mac] = rec
    save_json(STORE, store)
    log("RECORDED undervolt: final=%dmV validated=%s" % (final, validated))


def floor_hunt(store, rec, mac, args):
    mask = rec.get("die", {}).get("safe_cu_mask", {})
    if mask:
        log("applying safe CU mask (no bad pairs routed during test)")
        apply_cu_mask(mask)
    if not ensure_watchdog():
        log("WARNING: no hardware watchdog — hard hangs will NOT auto-reboot (soft-fail only)")
    wd = Watchdog(args.wd)                       # armed once for the entire sweep
    log("capturing golden @ %dmV" % args.start)
    write_gov_curve(args.start, args.throttle)
    wd.pet()
    outcome, golden = run_test(wd, args.duration, None)
    if outcome != "pass" or not golden:
        log("ABORT: start voltage %dmV not stable (%s) — raise --start" % (args.start, outcome))
        wd.disarm()
        return
    log("golden @ %dmV = %s" % (args.start, golden))
    last_stable, crash = args.start, None
    v = args.start - args.step
    while v >= args.floor_min:
        write_ckpt(mac, v, last_stable, golden, "testing")   # so a reboot resumes here
        log("testing %dmV ..." % v)
        wd.pet()
        write_gov_curve(v, args.throttle)
        wd.pet()
        outcome, ck = run_test(wd, args.duration, golden)
        if outcome == "pass":
            log("  %dmV PASS (ck=%s)" % (v, ck))
            last_stable = v
            write_ckpt(mac, v, last_stable, golden, "pass")
            v -= args.step
        else:
            log("  %dmV FAIL (%s) -> crash floor" % (v, outcome))
            crash = v
            break

    if crash is not None:
        # The GPU may be wedged (CPU/this process can still be alive). Do NOT try to
        # finalize on a wedged GPU. STOP petting the watchdog so it hard-resets the box;
        # on reboot bc250-fleet.service resumes from the (testing@crash) checkpoint and
        # finalizes on a clean GPU. This is the ONLY reliable recovery from a hard hang.
        log("CRASH at %dmV — HALTING watchdog pets; box will hard-reset in ~%ds, then "
            "resume-finalize at %d+%dmV on a clean boot" % (crash, args.wd, crash, args.margin))
        deadline = time.time() + args.wd + 120
        while time.time() < deadline:
            time.sleep(5)                       # deliberately NOT petting -> watchdog fires
        # Fallback: watchdog never fired (e.g. GPU recovered soft) -> finalize in-process.
        log("watchdog did not fire within %ds — finalizing in-process (fallback)" % (args.wd + 120))
        finalize(wd, store, rec, mac, crash, last_stable, golden, args)
        clear_ckpt()
        wd.disarm()
    else:
        # Swept to floor_min with no crash — GPU healthy, safe to finalize in-process.
        finalize(wd, store, rec, mac, None, last_stable, golden, args)
        clear_ckpt()
        wd.disarm()


RESUME_UNIT = "/etc/systemd/system/bc250-floorhunt.service"


def install_resume_service():
    unit = ("[Unit]\nDescription=BC-250 floor-hunt resume\nAfter=multi-user.target\n"
            "[Service]\nType=oneshot\n"
            "ExecStart=/usr/bin/env python3 %s --resume-boot\n"
            "[Install]\nWantedBy=multi-user.target\n" % os.path.abspath(sys.argv[0]))
    with open(RESUME_UNIT, "w") as f:
        f.write(unit)
    sh("systemctl daemon-reload")
    sh("systemctl enable bc250-floorhunt.service")
    log("installed + enabled bc250-floorhunt.service (auto-resume after watchdog reboot)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=920)
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--floor-min", type=int, default=820, dest="floor_min")
    ap.add_argument("--margin", type=int, default=40)
    ap.add_argument("--duration", type=int, default=45)
    ap.add_argument("--throttle", type=int, default=88)
    ap.add_argument("--wd", type=int, default=120, help="watchdog timeout (s)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--install-resume-service", action="store_true")
    ap.add_argument("--resume-boot", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("run as root")
    if args.install_resume_service:
        install_resume_service()
        return

    mac = primary_mac()
    if not mac:
        sys.exit("no NIC MAC")
    store = load_json(STORE, {"version": 1, "boards": {}})
    rec = store["boards"].get(mac)
    if not rec:
        sys.exit("no Phase-1 record for %s — run characterize first" % mac)

    ckpt = load_json(CKPT, {})
    if ckpt.get("mac") == mac and ckpt.get("state") == "testing":
        log("RESUME: test @ %dmV was interrupted (hard hang -> reboot) = crash floor" % ckpt["test_v"])
        if not ensure_watchdog():
            log("WARNING: no watchdog available for resume validation")
        wd = Watchdog(args.wd)
        try:
            finalize(wd, store, rec, mac, ckpt["test_v"], ckpt["last_stable"], ckpt["golden"], args)
            clear_ckpt()
        finally:
            wd.disarm()
        return
    if args.resume_boot:
        log("boot-resume: no checkpoint, nothing to do")
        return
    if rec.get("undervolt") and rec["undervolt"].get("status") == "complete" and not args.force:
        log("already characterized: final=%dmV (use --force)" % rec["undervolt"]["final_mv"])
        return

    floor_hunt(store, rec, mac, args)


if __name__ == "__main__":
    main()
