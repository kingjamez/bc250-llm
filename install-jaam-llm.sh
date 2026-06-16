#!/usr/bin/env bash
# install-jaam-llm.sh — turn a fresh Ubuntu 26.04 Server install on a BC-250 into a
# turnkey AI box (Ollama+Vulkan, 40-CU auto-unlock, governor, chat UI w/ web-search +
# self-upgrade agent, signed auto-update, reboot button).
#
#   PREREQUISITE (firmware, can't be scripted): flash modded BIOS, CLEAR CMOS, set
#   Integrated Graphics=Forces / UMA Mode=UMA_SPECIFIED / UMA Frame Buffer=512MB / IOMMU=Disabled.
#
#   Usage on a fresh box:  curl -fsSL https://raw.githubusercontent.com/kingjamez/bc250-llm/main/install-jaam-llm.sh | sudo bash
set -euo pipefail

# ---------- CONFIG (edit for your repo) ----------
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/kingjamez/bc250-llm/main/fleet-files}"  # where agent.py/index.html/etc live
CTRL_BASE="${CTRL_BASE:-https://bc250.jaamcp.com}"                                          # feed + signed update channel
UPDATE_PUBKEY='bc250@jaamcp.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIgto9ngqBqx575Vp3P4abFFPSWyDmpPGZQpMKMXeLwb'
GOV_VER="v0.4.6"
MODELS=( "gemma|hf.co/unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q3_K_M" "qwen3|hf.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF:Q3_K_S" )
DEFAULT_MODEL="gemma"
HOSTNAME_SET="chatbox"
say(){ echo -e "\n\033[1;36m== $* ==\033[0m"; }
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
USR="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"

# ---------- 0. PRE-CHECKS ----------
say "Pre-flight checks"
. /etc/os-release; echo "OS: $PRETTY_NAME"; [[ "$VERSION_ID" == 26.* ]] || echo "WARN: expected Ubuntu 26.x"
lspci -nn | grep -qi "1002:13fe" || { echo "ERROR: BC-250 (gfx1013) not found"; exit 1; }
VRAM=$(dmesg 2>/dev/null | grep -oiE "VRAM: [0-9]+M" | head -1 | grep -oE "[0-9]+" || echo 0)
if [ "${VRAM:-0}" -gt 2048 ]; then
  echo "ERROR: VRAM carveout is ${VRAM}M, not ~512M. The BIOS UMA setting didn't apply —"
  echo "       you must CLEAR CMOS, then set UMA Mode=UMA_SPECIFIED + UMA Frame Buffer=512MB. Aborting."
  exit 1
fi
echo "VRAM carveout ~${VRAM}M (OK)"; curl -fsSL --max-time 10 -o /dev/null "$CTRL_BASE/models.json" && echo "internet+feed OK" || echo "WARN: feed unreachable"

# ---------- 1. DISK: use the whole SSD ----------
say "Extending root filesystem to full disk"
LV=$(findmnt -no SOURCE /); lvextend -l +100%FREE "$LV" 2>/dev/null && resize2fs "$LV" || echo "(already full or not LVM)"

# ---------- 2. PACKAGES ----------
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq mesa-vulkan-drivers vulkan-tools git build-essential cmake \
  libdrm-dev libpciaccess-dev libncurses-dev pkg-config curl llvm-dev clang avahi-daemon glslang-tools

# ---------- 3. USER access ----------
say "User access"
usermod -aG render,video "$USR"
echo "$USR ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USR}-nopasswd && chmod 440 /etc/sudoers.d/${USR}-nopasswd

# ---------- 4. UNIFIED-MEMORY (TTM) ----------
say "TTM tuning"
echo "options ttm pages_limit=4194304 page_pool_size=4194304" > /etc/modprobe.d/ttm-gpu-memory.conf
update-initramfs -u >/dev/null 2>&1

# ---------- 5. GOVERNOR ----------
say "GPU governor"
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
ExecStart=/etc/cyan-skillfish-governor-smu/cyan-skillfish-governor-smu /etc/cyan-skillfish-governor-smu/config.toml
Restart=on-failure
RestartSec=5s
[Install]
WantedBy=multi-user.target
U

# ---------- 6. OLLAMA (Vulkan) ----------
say "Ollama"
curl -fsSL https://ollama.com/install.sh | sh
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf <<'C'
[Service]
Environment=OLLAMA_VULKAN=1
Environment=OLLAMA_IGPU_ENABLE=1
Environment=OLLAMA_HOST=0.0.0.0:11434
Environment=OLLAMA_FLASH_ATTENTION=1
Environment=OLLAMA_KV_CACHE_TYPE=q4_0
Environment=OLLAMA_KEEP_ALIVE=-1
Environment=OLLAMA_MAX_LOADED_MODELS=1
Environment=OLLAMA_CONTEXT_LENGTH=32768
OOMScoreAdjust=-1000
C
systemctl daemon-reload; systemctl restart ollama; sleep 3

# ---------- 7. umr + WinnieLV ----------
say "Building umr (register tool)"
git clone --depth 1 https://gitlab.freedesktop.org/tomstdenis/umr "$T/umr"
( cd "$T/umr" && cmake -DUMR_NO_GUI=ON -B build -S . >/dev/null 2>&1 && cmake --build build -j"$(nproc)" >/dev/null 2>&1 && cmake --install build >/dev/null 2>&1 )
mkdir -p /opt/bc250
curl -fsSL https://raw.githubusercontent.com/WinnieLV/bc250-cu-live-manager/main/bc250-cu-live-manager.sh -o /opt/bc250/cu-live-manager.sh
chmod +x /opt/bc250/cu-live-manager.sh

# ---------- 8. Fetch our component files ----------
say "Installing chat agent + CU/update scripts"
mkdir -p /opt/bc250-chat
curl -fsSL "$RAW_BASE/agent.py"          -o /opt/bc250-chat/agent.py
curl -fsSL "$RAW_BASE/index.html"        -o /opt/bc250-chat/index.html
curl -fsSL "$RAW_BASE/bc250-auto-unlock" -o /usr/local/bin/bc250-auto-unlock
curl -fsSL "$RAW_BASE/bc250-update"      -o /usr/local/bin/bc250-update
chmod +x /usr/local/bin/bc250-auto-unlock /usr/local/bin/bc250-update
echo "$DEFAULT_MODEL" > /opt/bc250-chat/default.txt
chown -R "$USR:$USR" /opt/bc250-chat
python3 -c "import ast; ast.parse(open('/opt/bc250-chat/agent.py').read())" || { echo "agent.py failed syntax check"; exit 1; }

# ---------- 9. Services (chat, firstboot, warm) ----------
say "Installing services"
cat > /etc/systemd/system/bc250-chat.service <<U
[Unit]
Description=BC-250 chat UI + agent
After=ollama.service network-online.target
Wants=ollama.service
[Service]
ExecStart=/usr/bin/python3 /opt/bc250-chat/agent.py
Restart=on-failure
RestartSec=3s
User=$USR
[Install]
WantedBy=multi-user.target
U
cat > /etc/systemd/system/bc250-firstboot.service <<'U'
[Unit]
Description=BC-250 first-boot provisioning (CU unlock + governor)
After=multi-user.target
ConditionPathExists=!/var/lib/bc250-provisioned
[Service]
Type=oneshot
ExecStartPre=/usr/bin/bash -c 'for _ in {1..30}; do compgen -G /dev/dri/renderD* >/dev/null && exit 0; sleep 1; done; exit 1'
ExecStart=/usr/local/bin/bc250-auto-unlock
ExecStartPost=/usr/bin/systemctl enable --now cyan-skillfish-governor-smu
ExecStartPost=/usr/bin/touch /var/lib/bc250-provisioned
[Install]
WantedBy=multi-user.target
U
cat > /etc/systemd/system/bc250-warm.service <<U
[Unit]
Description=Pre-warm default chat model
After=ollama.service
Wants=ollama.service
[Service]
Type=oneshot
ExecStart=/usr/bin/bash -c 'for i in {1..30}; do curl -sf http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 2; done; curl -s http://127.0.0.1:11434/api/chat -d "{\"model\":\"$(cat /opt/bc250-chat/default.txt)\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"stream\":false}" >/dev/null'
[Install]
WantedBy=multi-user.target
U

# ---------- 10. mDNS + aimodel CLI ----------
say "mDNS + CLI"
hostnamectl set-hostname "$HOSTNAME_SET"
cat > /usr/local/bin/aimodel <<'CLI'
#!/usr/bin/env bash
case "${1:-}" in
  list) ollama list ;;
  add)  ollama pull "$2" && ollama cp "$2" "$3" && ollama rm "$2" && echo "added: $3" ;;
  remove) ollama rm "${2:?}" ;;
  *) echo "usage: aimodel {list|add <hf.co/repo:quant> <name>|remove <name>}" ;;
esac
CLI
chmod +x /usr/local/bin/aimodel

# ---------- 11. Signed auto-update ----------
say "Auto-update channel"
mkdir -p /etc/bc250
echo "$UPDATE_PUBKEY" > /etc/bc250/allowed_signers
echo "BASE_URL=$CTRL_BASE" > /etc/bc250/update.conf
echo 1 > /etc/bc250/version
cat > /etc/systemd/system/bc250-update.service <<'U'
[Unit]
Description=BC-250 fleet update check
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/bc250-update
U
cat > /etc/systemd/system/bc250-update.timer <<'U'
[Unit]
Description=Daily BC-250 fleet update check
[Timer]
OnBootSec=10min
OnUnitActiveSec=1d
RandomizedDelaySec=2h
Persistent=true
[Install]
WantedBy=timers.target
U

# ---------- 12. Pull models ----------
say "Downloading models (this takes a while)"
for spec in "${MODELS[@]}"; do
  name="${spec%%|*}"; ref="${spec#*|}"
  echo "  pulling $name…"
  sudo -u "$USR" ollama pull "$ref" && sudo -u "$USR" ollama cp "$ref" "$name" && sudo -u "$USR" ollama rm "$ref"
done

# ---------- 13. Enable everything ----------
say "Enabling services"
systemctl daemon-reload
systemctl enable --now avahi-daemon ollama bc250-chat >/dev/null 2>&1
systemctl enable bc250-firstboot.service bc250-warm.service bc250-update.timer >/dev/null 2>&1

say "DONE"
cat <<EOF

✅ Installed. Reboot to finish (the CU unlock + governor apply on first boot):

    sudo reboot

After reboot, open from any device on the network:   http://${HOSTNAME_SET}.local:8080
Models: $(for s in "${MODELS[@]}"; do echo -n "${s%%|*} "; done) (default: ${DEFAULT_MODEL})
Manage: 'aimodel list', or just ask the assistant to search the web / upgrade itself.
EOF
