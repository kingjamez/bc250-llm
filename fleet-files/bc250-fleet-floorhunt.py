#!/usr/bin/env python3
"""bc250-fleet-floorhunt.py — BC-250 fleet suite, Phase 2: undervolt characterization.

Two modes (both watchdog-armed + checkpoint/resume; on a HARD hang they stop petting
so the SP5100 watchdog hard-resets, then bc250-fleet.service resumes on a clean boot):

  FULL  (default): 2000 MHz undervolt FLOOR-hunt via sustained soaks -> production mV.
                   Thermally influenced — only valid on a well-cooled board.

  RANK  (--rank):  cooling-INDEPENDENT silicon-quality score. Short (~4s) compute-verify
                   bursts from a COOLED start, with a temp-ceiling guard, so failures are
                   the silicon VOLTAGE floor (fast+cold) not the thermal floor (heat-soak).
                   Also records the chip's native vddgfx (AVFS). Use for the good-vs-poor
                   binary sort on un-modified heatsinks; run FULL later on the keepers.

Records into boards.json (`undervolt` for FULL, `rank` for RANK). Run as root.
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


def gpu_sysfs():
    for c in glob.glob("/sys/class/drm/card*/device"):
        try:
            if open(c + "/vendor").read().strip() == "0x1002" and os.path.exists(c + "/pp_dpm_sclk"):
                return c
        except OSError:
            pass
    return None


def hwmon_dir(gpu):
    for h in sorted(glob.glob((gpu or "") + "/hwmon/hwmon*")):
        return h
    return None


def _rdint(path):
    try:
        return int(open(path).read().strip())
    except (OSError, ValueError, TypeError):
        return None


def vddgfx_mv(hw):
    return _rdint((hw or "") + "/in0_input")          # amdgpu vddgfx, millivolts


def edge_c(hw):
    v = _rdint((hw or "") + "/temp1_input")
    return v // 1000 if v else None


def dmesg_faults():
    r = sh("dmesg 2>/dev/null | grep -icE 'amdgpu.*(reset|fault|hang|timeout|GPU reset)'")
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def cooldown(hw, target, maxwait=45):
    """Idle (no load) until edge temp <= target, plateaus, or times out. Returns final edge."""
    if not hw:
        time.sleep(3)
        return None
    t0 = time.time()
    prev = None
    while time.time() - t0 < maxwait:
        e = edge_c(hw)
        if e is None or e <= target:
            return e
        if prev is not None and prev - e < 1:        # plateaued — fans can't get it lower
            return e
        prev = e
        time.sleep(3)
    return edge_c(hw)


def native_vddgfx(gpu, hw, dur=6):
    """Chip's NATIVE vddgfx (governor OFF, kernel auto-DPM) under a brief load. (mv, clk)."""
    sh("systemctl stop cyan-skillfish-governor-smu")
    sh("sh -c 'echo auto > %s/power_dpm_force_performance_level' 2>/dev/null" % gpu)
    proc = subprocess.Popen(["bash", STRESS, "--duration", str(dur)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples = []
    t0 = time.time()
    while proc.poll() is None and time.time() - t0 < dur + 8:
        clk = sh("grep '\\*' %s/pp_dpm_sclk" % gpu).stdout
        m = re.search(r"(\d+)Mhz", clk)
        mv = vddgfx_mv(hw)
        if mv:
            samples.append((mv, int(m.group(1)) if m else 0))
        time.sleep(1)
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    if not samples:
        return (None, None)
    return max(samples, key=lambda s: s[1])           # vddgfx at the highest clock reached


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
            os.write(self.fd, b"V")
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
def write_ckpt(mac, test_v, last_stable, golden, state, mode="full", native=None):
    d = {"mac": mac, "test_v": test_v, "last_stable": last_stable, "golden": golden,
         "state": state, "mode": mode,
         "ts": datetime.datetime.now().isoformat(timespec="seconds")}
    if native:
        d["native_vddgfx_mv"], d["native_vddgfx_clk"] = native
    save_json(CKPT, d)


def clear_ckpt():
    try:
        os.remove(CKPT)
    except OSError:
        pass


# ---------------- one test (shared); returns (outcome, checksum, peak_edge_C) ----------------
def run_test(wd, duration, golden, hw=None):
    f0 = dmesg_faults()
    deadline = time.time() + duration + 90
    peak = 0
    with open(TESTLOG, "w") as lf:
        proc = subprocess.Popen(["bash", STRESS, "--duration", str(duration), "--verify"],
                                stdout=lf, stderr=subprocess.STDOUT)
        last_pet = 0.0
        while proc.poll() is None:
            now = time.time()
            if now - last_pet > 8:
                wd.pet()
                last_pet = now
            if hw:
                e = edge_c(hw)
                if e and e > peak:
                    peak = e
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
    if bad or not ck:
        return "hang", ck, peak
    if golden and ck != golden:
        return "corrupt", ck, peak
    return "pass", ck, peak


def _wait_for_watchdog(wd_timeout, what):
    """Stop petting and let the watchdog hard-reset the box (reboot -> resume finalizes)."""
    log("%s — HALTING watchdog pets; box will hard-reset in ~%ds, then resume on a clean boot"
        % (what, wd_timeout))
    deadline = time.time() + wd_timeout + 120
    while time.time() < deadline:
        time.sleep(5)
    log("watchdog did not fire within %ds — falling back to in-process finalize" % (wd_timeout + 120))


# ================= FULL mode (production undervolt floor) =================
def finalize(wd, store, rec, mac, crash, last_stable, golden, args):
    final = (crash + args.margin) if crash is not None else last_stable
    log("FLOOR: crash=%s last_stable=%s -> final=%dmV (margin %d)"
        % (crash, last_stable, final, args.margin))
    write_gov_curve(final, args.throttle)
    wd.pet()
    outcome, ck, _ = run_test(wd, max(args.duration, 60), golden)
    validated = (outcome == "pass")
    if not validated:
        final += args.step
        write_gov_curve(final, args.throttle)
        wd.pet()
        outcome, ck, _ = run_test(wd, max(args.duration, 60), golden)
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
        log("applying safe CU mask")
        apply_cu_mask(mask)
    if not ensure_watchdog():
        log("WARNING: no hardware watchdog — hard hangs will NOT auto-reboot")
    wd = Watchdog(args.wd)
    log("capturing golden @ %dmV" % args.start)
    write_gov_curve(args.start, args.throttle)
    wd.pet()
    outcome, golden, _ = run_test(wd, args.duration, None)
    if outcome != "pass" or not golden:
        log("ABORT: start voltage %dmV not stable (%s)" % (args.start, outcome))
        wd.disarm()
        return
    log("golden @ %dmV = %s" % (args.start, golden))
    last_stable, crash = args.start, None
    v = args.start - args.step
    while v >= args.floor_min:
        write_ckpt(mac, v, last_stable, golden, "testing", mode="full")
        log("testing %dmV ..." % v)
        wd.pet()
        write_gov_curve(v, args.throttle)
        wd.pet()
        outcome, ck, _ = run_test(wd, args.duration, golden)
        if outcome == "pass":
            log("  %dmV PASS (ck=%s)" % (v, ck))
            last_stable = v
            write_ckpt(mac, v, last_stable, golden, "pass", mode="full")
            v -= args.step
        else:
            log("  %dmV FAIL (%s)" % (v, outcome))
            crash = v
            break
    if crash is not None:
        _wait_for_watchdog(args.wd, "CRASH at %dmV" % crash)
        finalize(wd, store, rec, mac, crash, last_stable, golden, args)
        clear_ckpt()
        wd.disarm()
    else:
        finalize(wd, store, rec, mac, None, last_stable, golden, args)
        clear_ckpt()
        wd.disarm()


# ================= RANK mode (cooling-independent silicon score) =================
def rank_finalize(store, rec, mac, last_stable, crash, native, args):
    nv, nclk = native if native else (None, None)
    rec["rank"] = {
        "freq_mhz": 2000,
        "silicon_rank_mv": last_stable,     # lowest COLD-stable 2000MHz voltage; LOWER = better silicon
        "cool_crash_mv": crash,
        "native_vddgfx_mv": nv,
        "native_vddgfx_clk_mhz": nclk,
        "burst_s": args.burst,
        "method": "cool-burst-2000MHz",
        "note": "silicon-quality score for good/poor sort — NOT a production undervolt; "
                "run a full (no --rank) floor-hunt on a cooled board for the production mV",
        "tested_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    rec["status"] = "ranked"
    store["boards"][mac] = rec
    save_json(STORE, store)
    log("RANK RECORDED: silicon_rank_mv=%smV  native_vddgfx=%smV@%sMHz"
        % (last_stable, nv, nclk))


def rank_sweep(store, rec, mac, args):
    gpu = gpu_sysfs()
    hw = hwmon_dir(gpu)
    mask = rec.get("die", {}).get("safe_cu_mask", {})
    if mask:
        log("applying safe CU mask")
        apply_cu_mask(mask)
    nv, nclk = (None, None)
    if hw:
        log("reading native vddgfx (governor off, brief load) ...")
        nv, nclk = native_vddgfx(gpu, hw)
        log("native vddgfx = %smV @ %sMHz" % (nv, nclk))
    if not ensure_watchdog():
        log("WARNING: no hardware watchdog — a cold-burst hard hang would not auto-reboot")
    wd = Watchdog(args.wd)
    log("RANK golden @ %dmV (cool %ds burst)" % (args.start, args.burst))
    if hw:
        cooldown(hw, args.cool_target)
    write_gov_curve(args.start, args.throttle)
    wd.pet()
    outcome, golden, peak = run_test(wd, args.burst, None, hw)
    if outcome != "pass" or not golden:
        log("RANK ABORT: start %dmV not stable cold (%s)" % (args.start, outcome))
        wd.disarm()
        return
    log("RANK golden @ %dmV = %s (peak %sC)" % (args.start, golden, peak))
    last_stable, crash = args.start, None
    v = args.start - args.step
    while v >= args.floor_min:
        write_ckpt(mac, v, last_stable, golden, "testing", mode="rank", native=(nv, nclk))
        if hw:
            cooldown(hw, args.cool_target)
        log("RANK testing %dmV (cold) ..." % v)
        wd.pet()
        write_gov_curve(v, args.throttle)
        wd.pet()
        outcome, ck, peak = run_test(wd, args.burst, golden, hw)
        hot = bool(peak and peak > args.cool_ceiling)
        if outcome == "pass":
            log("  %dmV PASS (peak %sC%s)" % (v, peak, " — HOT, inconclusive" if hot else ""))
            last_stable = v
            write_ckpt(mac, v, last_stable, golden, "pass", mode="rank", native=(nv, nclk))
            v -= args.step
        else:
            log("  %dmV FAIL (%s, peak %sC) -> cold silicon floor" % (v, outcome, peak))
            crash = v
            break
    if crash is not None:
        _wait_for_watchdog(args.wd, "RANK CRASH at %dmV" % crash)
        rank_finalize(store, rec, mac, last_stable, crash, (nv, nclk), args)
        clear_ckpt()
        wd.disarm()
    else:
        rank_finalize(store, rec, mac, last_stable, None, (nv, nclk), args)
        clear_ckpt()
        wd.disarm()


# ---------------- resume service unit ----------------
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
    log("installed + enabled bc250-floorhunt.service")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rank", action="store_true",
                    help="cooling-independent silicon-rank (cold short bursts), not the production floor")
    ap.add_argument("--start", type=int, default=920)
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--floor-min", type=int, default=820, dest="floor_min")
    ap.add_argument("--margin", type=int, default=40)
    ap.add_argument("--duration", type=int, default=45, help="FULL-mode soak seconds")
    ap.add_argument("--burst", type=int, default=4, help="RANK-mode cold burst seconds")
    ap.add_argument("--cool-target", type=int, default=60, dest="cool_target",
                    help="RANK: cool to this edge °C before each burst")
    ap.add_argument("--cool-ceiling", type=int, default=82, dest="cool_ceiling",
                    help="RANK: flag a burst inconclusive if peak edge exceeds this °C")
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
        mode = ckpt.get("mode", "full")
        log("RESUME (%s): test @ %dmV interrupted (hard hang -> reboot) = crash floor"
            % (mode, ckpt["test_v"]))
        if not ensure_watchdog():
            log("WARNING: no watchdog for resume validation")
        wd = Watchdog(args.wd)
        try:
            if mode == "rank":
                native = (ckpt.get("native_vddgfx_mv"), ckpt.get("native_vddgfx_clk"))
                rank_finalize(store, rec, mac, ckpt["last_stable"], ckpt["test_v"], native, args)
            else:
                finalize(wd, store, rec, mac, ckpt["test_v"], ckpt["last_stable"], ckpt["golden"], args)
            clear_ckpt()
        finally:
            wd.disarm()
        return

    if args.rank:
        if rec.get("rank") and not args.force:
            log("already ranked: silicon_rank_mv=%smV (use --force)" % rec["rank"].get("silicon_rank_mv"))
            return
        rank_sweep(store, rec, mac, args)
    else:
        if rec.get("undervolt") and rec["undervolt"].get("status") == "complete" and not args.force:
            log("already characterized: final=%dmV (use --force)" % rec["undervolt"]["final_mv"])
            return
        floor_hunt(store, rec, mac, args)


if __name__ == "__main__":
    main()
