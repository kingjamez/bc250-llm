#!/usr/bin/env bash
# bc250-fleet-setup.sh — provision a fresh Ubuntu 26.04 install (on a portable USB SSD)
# as the BC-250 fleet CHARACTERIZATION APPLIANCE. Run ONCE on the USB:
#   curl -fsSL https://raw.githubusercontent.com/kingjamez/bc250-llm/main/fleet-files/bc250-fleet-setup.sh | sudo bash
# Then boot the USB on each BC-250 in turn; it auto-characterizes (CU map + undervolt
# floor-hunt), MAC-keyed, into /var/lib/bc250-fleet/boards.json, and prints the result.
set -euo pipefail
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/kingjamez/bc250-llm/main/fleet-files}"
GOV_VER="v0.4.6"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
say(){ echo -e "\n\033[1;36m== $* ==\033[0m"; }

say "1. dependencies"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential glslang-tools libvulkan-dev vulkan-tools mesa-vulkan-drivers \
  git cmake llvm-dev clang libdrm-dev python3 dmidecode pciutils curl \
  pkg-config libpciaccess-dev libncurses-dev libjson-c-dev zlib1g-dev   # umr build deps (minimal images lack these)

say "2. hardware watchdog auto-load at boot"
echo sp5100_tco > /etc/modules-load.d/bc250-watchdog.conf
modprobe sp5100_tco || echo "WARN: sp5100_tco didn't load now (will retry at boot)"

say "3. GPU governor (binary + service; config is managed by the floor-hunt)"
T=$(mktemp -d); ( cd "$T"
  B="https://github.com/filippor/cyan-skillfish-governor/releases/download/${GOV_VER}"
  curl -fsSL -O "$B/cyan-skillfish-governor-smu-${GOV_VER}-x86_64-linux.tar.gz"
  curl -fsSL -O "$B/cyan-skillfish-governor-smu-${GOV_VER}-x86_64-linux.tar.gz.sha256"
  sha256sum -c ./*.sha256 && tar -xf ./*.tar.gz && cd cyan-skillfish-governor-smu-*/
  mkdir -p /etc/cyan-skillfish-governor-smu
  install -m755 cyan-skillfish-governor-smu /etc/cyan-skillfish-governor-smu/
  sed 's/^enabled = true/enabled = false/' config.toml > /etc/cyan-skillfish-governor-smu/config.toml )
cat > /etc/systemd/system/cyan-skillfish-governor-smu.service <<'U'
[Unit]
Description=Cyan Skillfish GPU Governor
After=multi-user.target
[Service]
Type=simple
ExecStart=/etc/cyan-skillfish-governor-smu/cyan-skillfish-governor-smu /etc/cyan-skillfish-governor-smu/config.toml
Restart=on-failure
RestartSec=5s
[Install]
WantedBy=multi-user.target
U

say "4. umr + WinnieLV CU live-manager (for masking defective pairs during testing)"
git clone --depth 1 https://gitlab.freedesktop.org/tomstdenis/umr "$T/umr"
( cd "$T/umr" && cmake -DUMR_NO_GUI=ON -B build -S . >/dev/null 2>&1 \
  && cmake --build build -j"$(nproc)" >/dev/null 2>&1 && cmake --install build >/dev/null 2>&1 )
curl -fsSL https://raw.githubusercontent.com/WinnieLV/bc250-cu-live-manager/main/bc250-cu-live-manager.sh \
  -o /usr/local/bin/bc250-cu-live-manager
chmod +x /usr/local/bin/bc250-cu-live-manager

say "5. the characterization suite"
mkdir -p /opt/bc250-fleet
for f in bc250-fleet-characterize.py:characterize.py \
         bc250-fleet-floorhunt.py:floorhunt.py \
         bc250-gpu-stress.sh:stress.sh \
         bc250-fleet-run.sh:run.sh ; do
  src="${f%%:*}"; dst="${f##*:}"
  curl -fsSL "$RAW_BASE/$src" -o "/opt/bc250-fleet/$dst"
done
chmod +x /opt/bc250-fleet/*.sh /opt/bc250-fleet/*.py
touch /opt/bc250-fleet/ARMED        # armed = auto-sweep on boot; `rm` for inspect-only

say "6. boot-time auto-characterize service (console output)"
cat > /etc/systemd/system/bc250-fleet.service <<'U'
[Unit]
Description=BC-250 fleet auto-characterize
After=multi-user.target network.target
[Service]
Type=oneshot
ExecStart=/opt/bc250-fleet/run.sh
StandardOutput=journal+console
StandardError=journal+console
TimeoutStartSec=0
[Install]
WantedBy=multi-user.target
U
systemctl daemon-reload
systemctl enable bc250-fleet.service
# the floor-hunt's own resume unit would double-run on boot — make sure it's not enabled
systemctl disable bc250-floorhunt.service 2>/dev/null || true

say "DONE — appliance provisioned. Reboot on a BC-250 and it self-characterizes."
echo "  store: /var/lib/bc250-fleet/boards.json   |   disarm: rm /opt/bc250-fleet/ARMED"
