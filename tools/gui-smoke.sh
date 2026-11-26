#!/usr/bin/env bash
# Headless GUI check (HABIT 7): start kodesk in a headless sway against a fake reader tree,
# drive it with the keyboard like a user and save screenshots.
#
#   nix develop -c tools/gui-smoke.sh OUTDIR [KODESK_COMMAND...]
#
# Default command is `python -m kodesk` from the source tree; pass `result/bin/kodesk` to check
# the nix build. Never touches a real reader: the device is a fresh fake tree in a temp dir,
# and all XDG dirs are isolated.
set -euo pipefail

out="$(realpath -m "${1:?usage: tools/gui-smoke.sh OUTDIR [KODESK_COMMAND...]}")"
shift
if [ "$#" -eq 0 ]; then set -- python -m kodesk; fi
repo="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$out"

work="$(mktemp -d)"
sway_pid=""
app_pid=""
keeper_pid=""
cleanup() {
    [ -n "$keeper_pid" ] && kill "$keeper_pid" 2>/dev/null || true
    [ -n "$app_pid" ] && kill "$app_pid" 2>/dev/null || true
    [ -n "$sway_pid" ] && kill "$sway_pid" 2>/dev/null || true
    rm -rf "$work"
}
trap cleanup EXIT

export HOME="$work/home" XDG_CONFIG_HOME="$work/config" XDG_DATA_HOME="$work/data"
export XDG_CACHE_HOME="$work/cache" XDG_RUNTIME_DIR="$work/run"
# Same look as CI: Qt's built-in style, not the desktop's theme engine (Kvantum, qt6ct).
unset QT_STYLE_OVERRIDE QT_QPA_PLATFORMTHEME QT_PLUGIN_PATH_OVERRIDE
# No session bus: point at a socket that never exists, so neither the desktop's bus nor an
# autolaunched one (which would start portals/gvfs and leave FUSE mounts in the temp dir) is used.
export DBUS_SESSION_BUS_ADDRESS="unix:path=$work/no-session-bus"
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

device="$work/PB634K3"
PYTHONPATH="$repo/src:$repo" python -c \
    'import sys; from pathlib import Path; from tests.conftest import make_pocketbook; make_pocketbook(Path(sys.argv[1]))' \
    "$device"
# Store: a fake catalogue in the cache and --offline below, so the run never goes online.
PYTHONPATH="$repo/src:$repo" python -c \
    'import sys; from pathlib import Path; from tests.conftest import write_fake_catalogue; write_fake_catalogue(Path(sys.argv[1]))' \
    "$XDG_CACHE_HOME/kodesk/catalogue"

cat > "$work/sway.conf" <<'CONF'
output HEADLESS-1 resolution 1600x1000
default_border none
CONF

WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman \
    sway --unsupported-gpu -c "$work/sway.conf" >"$out/sway.log" 2>&1 &
sway_pid=$!

wait_for() {  # wait_for SECONDS COMMAND...: poll until COMMAND succeeds
    local deadline=$((SECONDS + $1))
    shift
    until "$@" 2>/dev/null; do
        if [ "$SECONDS" -ge "$deadline" ]; then return 1; fi
        sleep 0.2
    done
}
socket_ready() { ls "$XDG_RUNTIME_DIR"/wayland-* >/dev/null; }
wait_for 15 socket_ready || { echo "sway did not start, see $out/sway.log" >&2; exit 1; }
first_match() {  # first_match GLOB-RESULTS...: first existing path that is not a lock file
    local path
    for path in "$@"; do
        if [ -e "$path" ] && [ "${path%.lock}" = "$path" ]; then echo "$path"; return; fi
    done
    return 1
}
WAYLAND_DISPLAY="$(basename "$(first_match "$XDG_RUNTIME_DIR"/wayland-*)")"
export WAYLAND_DISPLAY
ipc_ready() { ls "$XDG_RUNTIME_DIR"/sway-ipc.*.sock >/dev/null; }
wait_for 5 ipc_ready || { echo "sway IPC socket missing" >&2; exit 1; }
SWAYSOCK="$(first_match "$XDG_RUNTIME_DIR"/sway-ipc.*.sock)"
export SWAYSOCK

QT_QPA_PLATFORM=wayland "$@" --offline "$device" >"$out/app.log" 2>&1 &
app_pid=$!
window_up() { swaymsg -t get_tree | grep -q '"app_id": "io.github.riddmaker.kodesk"\|"app_id": "kodesk"'; }
wait_for 20 window_up || {
    swaymsg -t get_tree >"$out/tree.json" 2>&1 || true
    echo "kodesk window did not appear, see $out/app.log and tree.json" >&2
    exit 1
}
sleep 1.5

shot() { grim "$out/$1.png"; echo "$out/$1.png"; }
# The headless seat has no keyboard of its own and every wtype call adds and removes a virtual
# one, which deactivates the window in between (no focus to see on screenshots). A long-lived
# idle wtype keeps the seat's keyboard present for the whole run.
wtype -s 3600000 &
keeper_pid=$!
sleep 0.5
press() { wtype "$@"; sleep 0.4; }
swaymsg '[app_id="io.github.riddmaker.kodesk"] focus' >/dev/null

# Keyboard only, like a user without a mouse. The reader's Home key has the initial focus;
# Tab walks Home -> Back -> Forward -> Menu, Space presses the focused key.
shot 01-start-home-key-focused
press -k Tab -k Tab -k space
shot 02-forward-pressed-page-2
press -M shift -k Tab -m shift -k space
shot 03-back-pressed-page-1
press -k Tab -k Tab -k space
shot 04-menu-pressed-menu-open
press -M shift -k Tab -k Tab -k Tab -m shift -k space
shot 05-home-pressed-locked
# Reader menu (Alt+R) -> Snapshots, take one with Alt+T, close the window with Escape.
press -M alt -k r -m alt -k Return
sleep 0.5
shot 06-snapshots-empty
press -M alt -k t -m alt
sleep 1
shot 07-snapshot-taken
press -k Escape
# Store tab (Alt+S): focus lands in the search field; type, Enter goes to the first result.
press -M alt -k s -m alt
sleep 1
shot 08-store-offline-catalogue
press clock -k Return
sleep 1
shot 09-store-search-selected
press -M ctrl -k q -m ctrl
exited() { ! kill -0 "$app_pid"; }
if wait_for 5 exited; then app_pid=""; echo "app quit via Ctrl+Q"; else echo "app did not quit" >&2; exit 1; fi
