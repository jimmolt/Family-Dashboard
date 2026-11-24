#!/usr/bin/env bash
# Turn a fresh Raspberry Pi OS Lite install (or Orange Pi / Armbian Debian
# "server" image) into a full-screen dashboard.
# On the Pi:
#   curl -sLO https://jimmolt.github.io/Family-Dashboard/setup-kiosk.sh
#   bash setup-kiosk.sh 'YOUR-PASSPHRASE'
set -euo pipefail
URL_BASE="https://jimmolt.github.io/Family-Dashboard/"
PASS="${1:-}"
ME="$(whoami)"
[ "$ME" = "root" ] && { echo "Run as your normal user (not sudo)."; exit 1; }

# Passphrase rides after '#', which browsers never send to the server.
URL="$URL_BASE"
if [ -n "$PASS" ]; then
  URL="$URL_BASE#k=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$PASS")"
fi

# systemd treats '%' specially, so escape it in the unit file.
SD_URL="${URL//%/%%}"

echo "== Installing browser (15-30 min on a Zero) =="
sudo apt-get update
sudo apt-get install -y cage fonts-noto-color-emoji fonts-dejavu-core curl
if grep -qi ubuntu /etc/os-release; then
  echo "This looks like Ubuntu, whose Chromium is a snap package that won't run in"
  echo "this kiosk. Please flash the Debian (Bookworm) server image instead."
  exit 1
fi
if apt-cache show chromium >/dev/null 2>&1; then
  sudo apt-get install -y chromium; CHROME=/usr/bin/chromium
else
  sudo apt-get install -y chromium-browser; CHROME=/usr/bin/chromium-browser
fi

echo "== Kiosk service =="
sudo tee /etc/systemd/system/kiosk.service >/dev/null <<EOF
[Unit]
Description=Family dashboard kiosk
After=network-online.target systemd-user-sessions.service
Wants=network-online.target
Conflicts=getty@tty1.service

[Service]
User=$ME
PAMName=login
TTYPath=/dev/tty1
StandardInput=tty
StandardOutput=journal
UtmpIdentifier=tty1
UtmpMode=user
Environment=XDG_SESSION_TYPE=wayland
# Wait for internet so Chromium doesn't open on an error page.
ExecStartPre=/bin/sh -c 'until curl -sfI ${URL_BASE} >/dev/null; do sleep 5; done'
ExecStart=/usr/bin/cage -d -s -- $CHROME --kiosk --ozone-platform=wayland \\
  --noerrdialogs --disable-infobars --no-first-run --password-store=basic \\
  --check-for-update-interval=31536000 --disable-features=Translate,MediaRouter \\
  --renderer-process-limit=1 '$SD_URL'
Restart=always
RestartSec=10

[Install]
WantedBy=graphical.target
EOF
sudo chmod 600 /etc/systemd/system/kiosk.service   # passphrase lives here

echo "== Keep screen on, add swap =="
# Raspberry Pi keeps kernel options in cmdline.txt; Orange Pi / Armbian in
# orangepiEnv.txt / armbianEnv.txt. Whichever exists gets consoleblank=0.
for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
  if [ -f "$f" ]; then
    grep -q consoleblank=0 "$f" || sudo sed -i '1 s/$/ consoleblank=0/' "$f"
  fi
done
for f in /boot/orangepiEnv.txt /boot/armbianEnv.txt; do
  if [ -f "$f" ] && ! grep -q consoleblank=0 "$f"; then
    if grep -q '^extraargs=' "$f"; then
      sudo sed -i 's/^extraargs=\(.*\)/extraargs=\1 consoleblank=0/' "$f"
    else
      echo 'extraargs=consoleblank=0' | sudo tee -a "$f" >/dev/null
    fi
  fi
done
if [ -f /etc/dphys-swapfile ]; then
  sudo sed -i 's/^#\?CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
  sudo systemctl restart dphys-swapfile || true
fi

sudo systemctl disable getty@tty1.service >/dev/null 2>&1 || true
sudo systemctl set-default graphical.target
sudo systemctl daemon-reload
sudo systemctl enable kiosk.service
echo
echo "Done. Reboot with:  sudo reboot"
