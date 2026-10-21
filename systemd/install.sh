#!/bin/bash

# Setup script for Twitch Recorder systemd service
# Run this on the Raspberry Pi as: bash install-service.sh

echo "Installing Twitch Recorder as systemd service..."
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo "ERROR: This script must be run with sudo"
   exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/twitch-recorder.service"

if [ ! -f "$SERVICE_FILE" ]; then
    echo "ERROR: Service file not found at $SERVICE_FILE"
    echo "Make sure the file is in the systemd directory"
    exit 1
fi

echo "Step 1: Copying service file to /etc/systemd/system/"
cp "$SERVICE_FILE" /etc/systemd/system/

echo "Step 2: Reloading systemd daemon..."
systemctl daemon-reload

echo "Step 3: Enabling service (auto-start on boot)..."
systemctl enable twitch-recorder

echo "Step 4: Starting service..."
systemctl start twitch-recorder

echo ""
echo "✓ Installation complete!"
echo ""
echo "Check status:"
echo "  systemctl status twitch-recorder"
echo ""
echo "View live logs:"
echo "  sudo journalctl -u twitch-recorder -f"
echo ""
echo "Check dashboard:"
echo "  dashboard"
echo ""
