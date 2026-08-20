#!/bin/bash
# Schedule the watch price monitor with launchd.
#
#   ./install.sh            # check every 10 minutes (default)
#   ./install.sh 300        # check every 5 minutes
#   ./install.sh --uninstall
#
# launchd runs it whether or not a terminal is open, and catches up on the next
# interval if the Mac was asleep.

set -euo pipefail

LABEL="com.kannappan.scrapely.watchmon"
# Renamed when the monitor grew past Invicta. Always torn down so a reinstall
# can't leave two jobs scraping the site on the same schedule.
LEGACY_LABELS=("com.kannappan.scrapely.invicta")

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# Daily health report (local time; launchd StartCalendarInterval is local).
REPORT_LABEL="com.kannappan.scrapely.watchmon.report"
REPORT_PLIST="$HOME/Library/LaunchAgents/$REPORT_LABEL.plist"
REPORT_HOUR="${REPORT_HOUR:-9}"
UV="$(command -v uv)"

remove_legacy() {
    for old in "${LEGACY_LABELS[@]}"; do
        if launchctl print "gui/$(id -u)/$old" >/dev/null 2>&1; then
            echo "Removing superseded job $old"
        fi
        launchctl bootout "gui/$(id -u)/$old" 2>/dev/null || true
        rm -f "$HOME/Library/LaunchAgents/$old.plist"
    done
}

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootout "gui/$(id -u)/$REPORT_LABEL" 2>/dev/null || true
    rm -f "$PLIST" "$REPORT_PLIST"
    remove_legacy
    echo "Uninstalled $LABEL"
    exit 0
fi

remove_legacy

INTERVAL="${1:-600}"

if [[ -z "$UV" ]]; then
    echo "error: uv not found on PATH" >&2
    exit 1
fi

mkdir -p "$HERE/logs"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV</string>
        <string>run</string>
        <string>--script</string>
        <string>$HERE/monitor.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$HERE</string>
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>
    <key>RunAtLoad</key>
    <true/>
    <!-- Floor between launches, whatever launchd decides to do on wake.
         monitor.py throttles again in-process; this is just the outer guard. -->
    <key>ThrottleInterval</key>
    <integer>60</integer>
    <!-- Don't fight the system for IO/CPU on battery. -->
    <key>LowPriorityIO</key>
    <true/>
    <key>Nice</key>
    <integer>5</integer>
    <key>StandardOutPath</key>
    <string>$HERE/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$HERE/logs/launchd.err.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# --- daily health report ----------------------------------------------------
# Separate job: a monitor that quietly stopped looks exactly like a monitor
# with nothing to report. This one says which it is, every morning.
cat > "$REPORT_PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$REPORT_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV</string>
        <string>run</string>
        <string>--script</string>
        <string>$HERE/monitor.py</string>
        <string>--report-push</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$HERE</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>$REPORT_HOUR</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$HERE/logs/report.out.log</string>
    <key>StandardErrorPath</key>
    <string>$HERE/logs/report.err.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$(id -u)/$REPORT_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$REPORT_PLIST"

echo "Installed $LABEL — checking every ${INTERVAL}s."
echo "  status:    launchctl list | grep watchmon"
echo "  logs:      tail -f $HERE/logs/monitor.log"
echo "  report:    daily at ${REPORT_HOUR}:00 local -> phone + Mac"
echo "  uninstall: $HERE/install.sh --uninstall"
