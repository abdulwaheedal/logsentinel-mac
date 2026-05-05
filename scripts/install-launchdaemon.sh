#!/usr/bin/env bash
# scripts/install-launchdaemon.sh
# ──────────────────────────────────────────────────────────────────────────────
# Installs the macOS Log Agent as a LaunchDaemon (system-wide, auto-starts
# on boot, runs as root so it can read all ULS subsystems).
#
# Usage (must run as root):
#   sudo bash scripts/install-launchdaemon.sh [--url http://localhost:8000] [--level info]
#
# To uninstall:
#   sudo bash scripts/install-launchdaemon.sh --uninstall
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

LABEL="com.logsentinel.mac-agent"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
PIPELINE_URL="http://localhost:8000"
LOG_LEVEL="info"
UNINSTALL=false
PYTHON_BIN="$(which python3)"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_SCRIPT="${SCRIPT_DIR}/agent/mac_log_agent.py"

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)       PIPELINE_URL="$2"; shift 2 ;;
    --level)     LOG_LEVEL="$2";    shift 2 ;;
    --python)    PYTHON_BIN="$2";   shift 2 ;;
    --uninstall) UNINSTALL=true;    shift   ;;
    *) echo "Unknown arg: $1"; exit 1       ;;
  esac
done

# ── Must be root ──────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  echo "❌  This script must be run as root (sudo)."
  exit 1
fi

# ── Uninstall ─────────────────────────────────────────────────────────────────
if $UNINSTALL; then
  echo "==> Stopping and removing LaunchDaemon…"
  launchctl bootout system "${PLIST}" 2>/dev/null || true
  rm -f "${PLIST}"
  echo "✓  Uninstalled. Agent will not start on next boot."
  exit 0
fi

# ── Validate paths ────────────────────────────────────────────────────────────
if [[ ! -f "${AGENT_SCRIPT}" ]]; then
  echo "❌  Agent script not found: ${AGENT_SCRIPT}"
  echo "    Run this script from the project root or pass the correct path."
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "❌  Python not found at: ${PYTHON_BIN}"
  echo "    Install Python 3.9+ or pass --python /path/to/python3"
  exit 1
fi

# ── Stop existing daemon if running ──────────────────────────────────────────
if launchctl list | grep -q "${LABEL}" 2>/dev/null; then
  echo "==> Stopping existing daemon…"
  launchctl bootout system "${PLIST}" 2>/dev/null || true
  sleep 1
fi

# ── Write plist ───────────────────────────────────────────────────────────────
LOG_DIR="/var/log/logsentinel"
mkdir -p "${LOG_DIR}"

cat > "${PLIST}" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>${AGENT_SCRIPT}</string>
    <string>--url</string>
    <string>${PIPELINE_URL}</string>
    <string>--level</string>
    <string>${LOG_LEVEL}</string>
  </array>

  <!-- Auto-start on boot -->
  <key>RunAtLoad</key>
  <true/>

  <!-- Restart automatically on crash (like Windows service recovery) -->
  <key>KeepAlive</key>
  <dict>
    <key>Crashed</key>
    <true/>
  </dict>

  <!-- stdout / stderr logs -->
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/agent.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/agent_error.log</string>

  <!-- Environment variable (also readable in agent via os.environ) -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PIPELINE_URL</key>
    <string>${PIPELINE_URL}</string>
  </dict>

  <!-- Throttle restart to 10 s minimum interval -->
  <key>ThrottleInterval</key>
  <integer>10</integer>
</dict>
</plist>
PLIST_EOF

chown root:wheel "${PLIST}"
chmod 644 "${PLIST}"

echo "✓  Plist written to ${PLIST}"

# ── Load daemon ───────────────────────────────────────────────────────────────
launchctl bootstrap system "${PLIST}"
sleep 1

if launchctl list | grep -q "${LABEL}"; then
  echo "✓  Daemon loaded and running."
else
  echo "⚠️  Daemon registered but may not be running yet."
  echo "    Check logs: tail -f ${LOG_DIR}/agent.log"
fi

echo ""
echo "════════════════════════════════════════════"
echo "  LogSentinel Mac Agent — Service Installed"
echo "════════════════════════════════════════════"
echo "  Label:        ${LABEL}"
echo "  Pipeline URL: ${PIPELINE_URL}"
echo "  Log level:    ${LOG_LEVEL}"
echo "  Stdout log:   ${LOG_DIR}/agent.log"
echo "  Stderr log:   ${LOG_DIR}/agent_error.log"
echo ""
echo "  Useful commands:"
echo "    View logs:   tail -f ${LOG_DIR}/agent.log"
echo "    Stop:        sudo launchctl bootout system ${PLIST}"
echo "    Start:       sudo launchctl bootstrap system ${PLIST}"
echo "    Uninstall:   sudo bash scripts/install-launchdaemon.sh --uninstall"
