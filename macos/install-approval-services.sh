#!/bin/sh
set -eu

source_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
service_root=${ORCHESTRA_OMCP_SERVICE_ROOT:-"$HOME/Library/Application Support/Orchestra/omcp-service"}
state_dir=${ORCHESTRA_OMCP_STATE_DIR:-"$service_root/state"}
approval_root="$service_root/approval"
app="$approval_root/Orchestra Approval.app"
app_binary="$app/Contents/MacOS/OrchestraApprovalNotifier"
portal_token="$state_dir/approval-portal.token"
portal_log="$approval_root/portal.log"
notifications_log="$approval_root/notifications.log"
launch_agents="$HOME/Library/LaunchAgents"
portal_label='com.mr-m13.orchestra.omcp-approval-portal'
notifications_label='com.mr-m13.orchestra.omcp-approval-notifications'

umask 077
/bin/mkdir -p "$app/Contents/MacOS" "$approval_root" "$launch_agents" "$state_dir"
/bin/chmod 700 "$approval_root" "$state_dir"

/usr/bin/swiftc -framework Cocoa -framework UserNotifications \
  "$source_root/macos/OrchestraApprovalNotifier.swift" -o "$app_binary"
/bin/chmod 700 "$app_binary"

info_plist="$app/Contents/Info.plist"
/usr/bin/plutil -create xml1 "$info_plist"
/usr/bin/plutil -insert CFBundleIdentifier -string 'com.mr-m13.orchestra.approval' "$info_plist"
/usr/bin/plutil -insert CFBundleName -string 'Orchestra Approval' "$info_plist"
/usr/bin/plutil -insert CFBundleDisplayName -string 'Orchestra Approval' "$info_plist"
/usr/bin/plutil -insert CFBundleExecutable -string 'OrchestraApprovalNotifier' "$info_plist"
/usr/bin/plutil -insert CFBundlePackageType -string 'APPL' "$info_plist"
/usr/bin/plutil -insert CFBundleShortVersionString -string '1.0' "$info_plist"
/usr/bin/plutil -insert CFBundleVersion -string '1' "$info_plist"
/usr/bin/plutil -insert LSUIElement -bool true "$info_plist"
/bin/chmod 600 "$info_plist"

portal_plist="$launch_agents/$portal_label.plist"
/usr/bin/plutil -create xml1 "$portal_plist"
/usr/bin/plutil -insert Label -string "$portal_label" "$portal_plist"
/usr/bin/plutil -insert ProgramArguments -xml "<array><string>/usr/bin/python3</string><string>-m</string><string>omcp.approval_portal</string><string>--state-dir</string><string>$state_dir</string><string>--listen-host</string><string>127.0.0.1</string><string>--listen-port</string><string>8767</string><string>--token-path</string><string>$portal_token</string></array>" "$portal_plist"
/usr/bin/plutil -insert WorkingDirectory -string "$source_root" "$portal_plist"
/usr/bin/plutil -insert RunAtLoad -bool true "$portal_plist"
/usr/bin/plutil -insert KeepAlive -bool true "$portal_plist"
/usr/bin/plutil -insert LimitLoadToSessionType -string Aqua "$portal_plist"
/usr/bin/plutil -insert StandardOutPath -string "$portal_log" "$portal_plist"
/usr/bin/plutil -insert StandardErrorPath -string "$portal_log" "$portal_plist"
/bin/chmod 600 "$portal_plist"

notifications_plist="$launch_agents/$notifications_label.plist"
/usr/bin/plutil -create xml1 "$notifications_plist"
/usr/bin/plutil -insert Label -string "$notifications_label" "$notifications_plist"
/usr/bin/plutil -insert ProgramArguments -xml "<array><string>/usr/bin/python3</string><string>-m</string><string>omcp.approval_notifications</string><string>--state-dir</string><string>$state_dir</string><string>--notifier-executable</string><string>$app_binary</string><string>--portal-base-url</string><string>http://127.0.0.1:8767</string><string>--portal-token-path</string><string>$portal_token</string><string>--poll-seconds</string><string>2</string></array>" "$notifications_plist"
/usr/bin/plutil -insert WorkingDirectory -string "$source_root" "$notifications_plist"
/usr/bin/plutil -insert RunAtLoad -bool true "$notifications_plist"
/usr/bin/plutil -insert KeepAlive -bool true "$notifications_plist"
/usr/bin/plutil -insert LimitLoadToSessionType -string Aqua "$notifications_plist"
/usr/bin/plutil -insert StandardOutPath -string "$notifications_log" "$notifications_plist"
/usr/bin/plutil -insert StandardErrorPath -string "$notifications_log" "$notifications_plist"
/bin/chmod 600 "$notifications_plist"

for label in "$portal_label" "$notifications_label"; do
  /bin/launchctl bootout "gui/$(/usr/bin/id -u)/$label" 2>/dev/null || true
  /bin/launchctl bootstrap "gui/$(/usr/bin/id -u)" "$launch_agents/$label.plist"
done

/bin/launchctl kickstart -k "gui/$(/usr/bin/id -u)/$portal_label"
/bin/launchctl kickstart -k "gui/$(/usr/bin/id -u)/$notifications_label"
printf 'Installed %s and %s\n' "$portal_label" "$notifications_label"
