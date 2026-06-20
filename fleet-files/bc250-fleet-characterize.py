#!/usr/bin/env python3
"""bc250-fleet-characterize.py — BC-250 fleet characterization suite.

Phase 1 (this file): SAFE characterization only — no GPU load, no risk.
  - identifies the board by primary NIC MAC (the table key)
  - collects system info (BIOS, UMA/VRAM, GTT, CPU, RAM, kernel)
  - reads the die harvest (libdrm) -> clean/defective, defective WGP pairs,
    the max-safe CU mask, and max-safe CU count
  - merges the result into /var/lib/bc250-fleet/boards.json keyed by MAC

Phases 2 (undervolt floor-hunt) and 3 (thermal) plug into the same record/store.
Run as root (libdrm render node + dmidecode).
"""
import ctypes, struct, os, json, subprocess, glob, datetime, argparse

STORE = "/var/lib/bc250-fleet/boards.json"
AMDGPU_INFO_DEV_INFO = 0x16   # amdgpu_query_info: device info struct (has cu_bitmap)


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


def primary_nic():
    """First 'up' wired NIC (else first wired NIC). Returns (name, mac)."""
    best = None
    for p in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(p)
        if name == "lo" or name.startswith(("wl", "docker", "veth", "br", "virbr", "tap")):
            continue
        try:
            mac = open(p + "/address").read().strip()
            state = open(p + "/operstate").read().strip()
        except OSError:
            continue
        if not mac or mac == "00:00:00:00:00:00":
            continue
        if state == "up":
            return name, mac
        best = best or (name, mac)
    return best or (None, None)


def find_gpu_sysfs():
    for c in glob.glob("/sys/class/drm/card*/device"):
        try:
            if open(c + "/vendor").read().strip() == "0x1002" and \
               os.path.exists(c + "/pp_dpm_sclk"):
                return c
        except OSError:
            pass
    return None


def read_harvest():
    """Read the per-shader-array CU bitmap via libdrm. Returns (nse, nsh, [(se,sh,bm)])."""
    try:
        d = ctypes.CDLL("libdrm_amdgpu.so.1")
    except OSError:
        return None
    nodes = sorted(glob.glob("/dev/dri/renderD*"))
    if not nodes:
        return None
    fd = os.open(nodes[0], os.O_RDWR)
    try:
        dev = ctypes.c_void_p()
        a = ctypes.c_uint32(); b = ctypes.c_uint32()
        if d.amdgpu_device_initialize(fd, ctypes.byref(a), ctypes.byref(b),
                                      ctypes.byref(dev)) != 0:
            return None
        buf = (ctypes.c_uint8 * 1024)()
        d.amdgpu_query_info(dev, AMDGPU_INFO_DEV_INFO, 1024, ctypes.byref(buf))
        raw = bytes(buf)
        nse = struct.unpack_from("<I", raw, 20)[0]
        nsh = struct.unpack_from("<I", raw, 24)[0]
        arrays = []
        for se in range(nse):
            for sh in range(nsh):
                bm = struct.unpack_from("<I", raw, 56 + (se * 4 + sh) * 4)[0] & 0x3ff
                arrays.append((se, sh, bm))
        return nse, nsh, arrays
    finally:
        os.close(fd)


def analyze_die(harvest):
    """Compute clean/defective, defective WGP pairs, and the max-safe CU mask.

    Rule (validated on real clean + defective dies): within a shader array, a WGP
    (CUs 2k, 2k+1) is DEFECTIVE if both its CUs read 0 AND some higher-indexed CU in
    the same array reads 1 (a hole). Trailing zeros (no higher CU set) are just
    not-default-enabled good silicon and ARE safe to route. Safe mask = every
    non-defective WGP enabled.
    """
    nse, nsh, arrays = harvest
    out = {"harvest": [], "defective_pairs": [], "safe_cu_mask": {},
           "max_safe_cus": 0, "type": "clean"}
    for se, sh, bm in arrays:
        bits = [(bm >> i) & 1 for i in range(10)]
        out["harvest"].append("SE%d.SH%d:%s" %
                              (se, sh, "".join("#" if x else "." for x in bits)))
        highest = max((i for i in range(10) if bits[i]), default=-1)
        mask = 0
        for w in range(5):
            c0, c1 = 2 * w, 2 * w + 1
            hole = (not bits[c0] and not bits[c1]) and (highest > c1)
            if hole:
                out["defective_pairs"].append("SE%d.SH%d.WGP%d" % (se, sh, w))
                out["type"] = "defective"
            else:
                mask |= (1 << w)
        out["safe_cu_mask"]["SE%d.SH%d" % (se, sh)] = "0x%02x" % mask
        out["max_safe_cus"] += bin(mask).count("1") * 2
    return out


def _rd_int(path):
    try:
        return int(open(path).read().strip())
    except (OSError, ValueError):
        return None


def system_info(gpu):
    info = {}
    info["bios_version"] = (sh("cat /sys/class/dmi/id/bios_version 2>/dev/null")
                            or sh("dmidecode -s bios-version 2>/dev/null"))
    info["bios_date"] = sh("cat /sys/class/dmi/id/bios_date 2>/dev/null")
    info["kernel"] = sh("uname -r")
    info["hostname"] = sh("hostname")
    info["cpu"] = sh("grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | xargs")
    mt = sh("awk '/MemTotal/{print int($2/1024)}' /proc/meminfo")
    info["ram_mb"] = int(mt) if mt.isdigit() else None
    if gpu:
        vt = _rd_int(gpu + "/mem_info_vram_total")
        gt = _rd_int(gpu + "/mem_info_gtt_total")
        info["vram_mb"] = vt // (1024 * 1024) if vt else None
        info["gtt_mb"] = gt // (1024 * 1024) if gt else None
    return info


def load_store():
    try:
        return json.load(open(STORE))
    except (OSError, ValueError):
        return {"version": 1, "boards": {}}


def save_store(store):
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2, sort_keys=True)
    os.replace(tmp, STORE)


def main():
    global STORE
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=STORE)
    ap.add_argument("--print", action="store_true", help="just print this board's record")
    args = ap.parse_args()
    STORE = args.store

    name, mac = primary_nic()
    if not mac:
        raise SystemExit("ERROR: no wired NIC MAC found (can't key the board)")
    now = datetime.datetime.now().isoformat(timespec="seconds")
    gpu = find_gpu_sysfs()

    store = load_store()
    rec = store["boards"].get(mac, {"mac": mac, "first_seen": now})

    if args.print:
        print(json.dumps(rec, indent=2, sort_keys=True))
        return

    rec["mac"] = mac
    rec["nic"] = name
    rec["last_updated"] = now
    rec["system"] = system_info(gpu)
    h = read_harvest()
    if h:
        rec["die"] = analyze_die(h)
    rec.setdefault("undervolt", None)   # Phase 2 fills
    rec.setdefault("thermal", None)     # Phase 3 fills
    rec.setdefault("status", "cu_mapped")
    store["boards"][mac] = rec
    save_store(store)

    d = rec.get("die", {})
    s = rec["system"]
    print("=== board %s (%s) ===" % (mac, name))
    print("  BIOS %s (%s)  kernel %s  RAM %sMB" %
          (s.get("bios_version"), s.get("bios_date"), s.get("kernel"), s.get("ram_mb")))
    print("  VRAM %sMB  GTT %sMB" % (s.get("vram_mb"), s.get("gtt_mb")))
    if d:
        print("  die: %s   max-safe CUs: %d/40" % (d.get("type"), d.get("max_safe_cus", 0)))
        for line in d.get("harvest", []):
            print("    " + line)
        if d.get("defective_pairs"):
            print("  defective: " + ", ".join(d["defective_pairs"]))
        print("  safe mask: " + " ".join("%s=%s" % (k, v)
              for k, v in sorted(d.get("safe_cu_mask", {}).items())))
    print("  status: %s   stored -> %s" % (rec["status"], STORE))


if __name__ == "__main__":
    main()
