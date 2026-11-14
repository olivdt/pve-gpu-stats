#!/usr/bin/env bash
#
# pve-gpu-stats installer. Run as root on the Proxmox VE node:
#
#   bash install.sh               install or update
#   bash install.sh --uninstall   remove (the history and settings are kept)
#   bash install.sh --uninstall --purge   remove everything
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${PVE_GPU_STATS_ROOT:-}"          # only set by the tests

PVE_DIR="$ROOT/usr/share/pve-manager"
TPL="$PVE_DIR/index.html.tpl"
JS_DIR="$PVE_DIR/js"
BIN="$ROOT/usr/local/sbin/pve-gpu-stats"
UI="$ROOT/usr/local/sbin/pve-gpu-stats-ui"
UNIT="$ROOT/etc/systemd/system/pve-gpu-stats.service"
DEFAULTS="$ROOT/etc/default/pve-gpu-stats"
APT_HOOK="$ROOT/etc/apt/apt.conf.d/99-pve-gpu-stats"
STATE_DIR="$ROOT/var/lib/pve-gpu-stats"
SERVICE=pve-gpu-stats.service

say()  { printf '%s\n' "$*"; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

[ -n "$ROOT" ] || [ "$(id -u)" -eq 0 ] || fail "run this script as root on the Proxmox VE node."

uninstall() {
    local purge="${1:-}"
    say "Removing pve-gpu-stats..."
    systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
    if [ -x "$UI" ]; then "$UI" remove || true; fi
    rm -f "$UNIT" "$BIN" "$UI" "$APT_HOOK" "$JS_DIR/pve-gpu-stats.js"
    systemctl daemon-reload >/dev/null 2>&1 || true
    if [ "$purge" = "--purge" ]; then
        rm -rf "$STATE_DIR" "$DEFAULTS"
        say "History and settings deleted."
    else
        say "History ($STATE_DIR) and settings ($DEFAULTS) kept; add --purge to delete them."
    fi
    say "Done. Reload the web interface (Ctrl+Shift+R)."
}

case "${1:-}" in
    --uninstall) uninstall "${2:-}"; exit 0 ;;
    "") ;;
    *) fail "unknown option '$1' (use --uninstall [--purge])" ;;
esac

# --- checks ------------------------------------------------------------------

[ -f "$TPL" ] && [ -d "$JS_DIR" ] || fail "$PVE_DIR not found: this is not a Proxmox VE node."
grep -q 'pvemanagerlib\.js' "$TPL" || fail "unexpected content in $TPL, nothing was changed."
command -v python3 >/dev/null 2>&1 || fail "python3 is required: apt install python3"
for file in pve-gpu-stats.py pve-gpu-stats.js; do
    [ -f "$HERE/$file" ] || fail "$file is missing next to install.sh."
done
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$HERE/pve-gpu-stats.py" \
    || fail "pve-gpu-stats.py does not parse with this python3."

# --- files -------------------------------------------------------------------

say "Installing pve-gpu-stats..."
mkdir -p "$(dirname "$BIN")" "$(dirname "$UNIT")" "$(dirname "$DEFAULTS")" "$(dirname "$APT_HOOK")" "$STATE_DIR"

# untouched copy of the template, in case it is ever needed
if ! grep -q 'pve-gpu-stats\.js' "$TPL"; then
    cp -p "$TPL" "$STATE_DIR/index.html.tpl.orig"
fi

install -m 0755 "$HERE/pve-gpu-stats.py" "$BIN"
install -m 0644 "$HERE/pve-gpu-stats.js" "$JS_DIR/pve-gpu-stats.js"

cat > "$UI" <<EOF
#!/bin/sh
# Adds (apply) or removes (remove) the pve-gpu-stats hook of the Proxmox VE
# web interface: one <script> line in index.html.tpl and the links to the
# statistics. "apply" runs again after every package operation (see
# $APT_HOOK), because a pve-manager upgrade restores the original template.
TPL="$TPL"
JS_DIR="$JS_DIR"
RUN_DIR=/run/pve-gpu-stats
EOF
cat >> "$UI" <<'EOF'
JS="$JS_DIR/pve-gpu-stats.js"

links() {
    for timeframe in hour day week month year; do
        for cf in AVERAGE MAX; do
            name="gpu-stats-$timeframe-$cf.json"
            if [ "$1" = add ]; then
                ln -sfn "$RUN_DIR/$name" "$JS_DIR/$name"
            else
                rm -f "$JS_DIR/$name"
            fi
        done
    done
}

remove_line() {
    [ -f "$TPL" ] || return 0
    grep -q 'pve-gpu-stats\.js' "$TPL" || return 0
    grep -v 'pve-gpu-stats\.js' "$TPL" > "$TPL.pve-gpu-stats.tmp" || true
    [ -s "$TPL.pve-gpu-stats.tmp" ] && cat "$TPL.pve-gpu-stats.tmp" > "$TPL"
    rm -f "$TPL.pve-gpu-stats.tmp"
}

add_line() {
    [ -f "$TPL" ] && [ -f "$JS" ] || return 0
    version=$(cksum < "$JS" | cut -d' ' -f1)
    line="    <script type=\"text/javascript\" src=\"/pve2/js/pve-gpu-stats.js?ver=$version\"></script>"
    grep -qF "$line" "$TPL" && return 0
    remove_line
    if ! grep -q 'pvemanagerlib\.js' "$TPL"; then
        echo "pve-gpu-stats: pvemanagerlib.js not found in $TPL, web interface left untouched" >&2
        return 1
    fi
    awk -v line="$line" '{ print } /pvemanagerlib\.js/ && !done { print line; done = 1 }' \
        "$TPL" > "$TPL.pve-gpu-stats.tmp"
    if grep -qF "$line" "$TPL.pve-gpu-stats.tmp"; then
        cat "$TPL.pve-gpu-stats.tmp" > "$TPL"
    fi
    rm -f "$TPL.pve-gpu-stats.tmp"
    grep -qF "$line" "$TPL"
}

case "$1" in
    apply)  links add; add_line ;;
    remove) remove_line; links remove ;;
    *) echo "usage: $0 apply|remove" >&2; exit 2 ;;
esac
EOF
chmod 0755 "$UI"

cat > "$UNIT" <<EOF
[Unit]
Description=GPU statistics for the Proxmox VE node summary
After=pve-cluster.service

[Service]
Type=simple
EnvironmentFile=-/etc/default/pve-gpu-stats
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/local/sbin/pve-gpu-stats daemon
RuntimeDirectory=pve-gpu-stats
RuntimeDirectoryMode=0755
StateDirectory=pve-gpu-stats
Restart=always
RestartSec=10
Nice=10

[Install]
WantedBy=multi-user.target
EOF

if [ ! -f "$DEFAULTS" ]; then
    cat > "$DEFAULTS" <<'EOF'
# Settings of the pve-gpu-stats collector. After a change:
#   systemctl restart pve-gpu-stats

# Seconds between two readings of the host GPUs (nvidia-smi on the host).
PVE_GPU_HOST_INTERVAL=10

# Seconds between two readings of the GPUs passed through to VMs
# (nvidia-smi run in the guest through the QEMU guest agent).
PVE_GPU_VM_INTERVAL=30

# Set to 0 to never run anything inside the VMs.
PVE_GPU_VM_POLLING=1

# nvidia-smi command in the guests (a full path if it is not in the PATH).
PVE_GPU_VM_NVIDIA_SMI=nvidia-smi
EOF
fi

cat > "$APT_HOOK" <<'EOF'
// pve-gpu-stats: put the GPU charts back after a pve-manager upgrade.
DPkg::Post-Invoke { "if [ -x /usr/local/sbin/pve-gpu-stats-ui ]; then /usr/local/sbin/pve-gpu-stats-ui apply || true; fi"; };
EOF

# --- activation --------------------------------------------------------------

"$UI" apply || fail "could not add the charts to the web interface (the collector is installed but not started)."

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 3
systemctl is-active --quiet "$SERVICE" \
    || fail "the service did not start, see: journalctl -u pve-gpu-stats -n 30"

say ""
say "What the collector sees right now:"
say "-----------------------------------"
"$BIN" check || true
say "-----------------------------------"
say ""
say "Installed. Reload the web interface (Ctrl+Shift+R) and open the node Summary:"
say "the GPU charts appear after about one minute, once the first values are in."
say "Logs: journalctl -u pve-gpu-stats    Settings: /etc/default/pve-gpu-stats"
