# Systemd Service Setup for Twitch Recorder

This sets up the twitch-recorder to automatically start on boot and restart if it crashes.

## Installation

### Option A: Use the install script
```bash
cd ~/twitch-stream-recorder/systemd
sudo bash install.sh
```

### Option B: Manual installation

#### 1. Copy the service file to systemd
```bash
sudo cp ~/twitch-stream-recorder/systemd/twitch-recorder.service /etc/systemd/system/
```

### 2. Reload systemd daemon
```bash
sudo systemctl daemon-reload
```

### 3. Enable the service (auto-start on boot)
```bash
sudo systemctl enable twitch-recorder
```

### 4. Start the service now
```bash
sudo systemctl start twitch-recorder
```

### 5. Verify it's running
```bash
sudo systemctl status twitch-recorder
```

---

## Managing the Service

### Check service status
```bash
systemctl status twitch-recorder
```

### View live logs
```bash
sudo journalctl -u twitch-recorder -f
```

### View last 50 log lines
```bash
sudo journalctl -u twitch-recorder -n 50
```

### Stop the service
```bash
sudo systemctl stop twitch-recorder
```

### Restart the service
```bash
sudo systemctl restart twitch-recorder
```

### Disable auto-start (but keep installed)
```bash
sudo systemctl disable twitch-recorder
```

---

## Service Features

- **Auto-start on boot** - Recorder starts automatically after reboot
- **Auto-restart on crash** - If process dies, restarts after 10 seconds
- **Systemd logging** - All output logged to journalctl (viewable with `journalctl`)
- **Resource limits** - Max 512MB memory, 80% CPU usage
- **Security hardening** - Read-only filesystem except for recording directory
- **Network dependency** - Waits for network to be available before starting

---

## Troubleshooting

### Service won't start
Check the error logs:
```bash
sudo journalctl -u twitch-recorder -n 30 -p err
```

### Too many restarts
Look for crash patterns:
```bash
sudo journalctl -u twitch-recorder --since "1 hour ago"
```

### Want to run custom command
Edit the service file:
```bash
sudo nano /etc/systemd/system/twitch-recorder.service
```

Then reload:
```bash
sudo systemctl daemon-reload
sudo systemctl restart twitch-recorder
```

---

## Combining with Dashboard

Now you can use the dashboard from anywhere on the Pi:
```bash
dashboard  # shows current status
```

And the service handles background operation automatically!
