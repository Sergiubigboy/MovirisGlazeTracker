#!/usr/bin/env bash
# Adds a WiFi network for auto-connect on Raspberry Pi OS (NetworkManager,
# default since Bookworm).
#
#   sudo ./tools/add_wifi.sh "SSID" "password"

set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: sudo $0 <ssid> <password>" >&2
    exit 1
fi

SSID="$1"
PASSWORD="$2"

nmcli connection add \
    type wifi \
    con-name "$SSID" \
    ifname wlan0 \
    ssid "$SSID" \
    -- \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$PASSWORD" \
    connection.autoconnect yes

echo "added '$SSID', will auto-connect when in range"
