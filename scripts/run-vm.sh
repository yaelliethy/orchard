#!/usr/bin/env bash
# Boot macOS on QEMU's `apple-vm` machine through the real Apple chain:
# AVPBooter -> iBootStage1 (LLB) -> iBootStage2 (iBoot) -> stock XNU.
#
# Everything below was measured on the reference host (Linux, AMD RENOIR,
# Mesa RADV). The notes say why each choice is what it is, because most of
# them cost a boot to find.
#
# Copyright (c) 2026 Youssef Elliethy (yaelliethy)
# SPDX-License-Identifier: GPL-2.0-or-later
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

QEMU="${QEMU:-$ROOT/qemu/build/qemu-system-aarch64}"
IMAGES="${IMAGES:-$ROOT/images}"
DISK="${DISK:-$IMAGES/disk.raw}"          # tart image, read-only
OVERLAY="${OVERLAY:-$IMAGES/overlay.qcow2}"
AUX="${AUX:-$IMAGES/aux.img}"             # tart NVRAM + LocalPolicy
ROM="${ROM:-$IMAGES/AVPBooter.patched.bin}"
ECID="${ECID:-}"                          # from images/config.json
# 10G, not more: the whole RAMBlock is imported into one Vulkan heap, and this
# class of host tops out around 10.5 GiB of importable memory. Past that the
# import is refused (`guest_ram_map_import_exceeds_heap`) and every texture bind
# falls back to a CPU byte loader - slow, with black backgrounds. Measured at
# 10G: `imported_mib=10241`, import kept.
RAM="${RAM:-10G}"
# 12 vCPUs boots to the desktop in ~75 s here. The earlier ceiling of 6 was a
# livelock in XNU's startup pen: secondaries came up without PAC keys and
# faulted there. hw/vmapple/hvc.c now gives each core the keys when the kernel
# asks (SET_INITIAL_STATE); no WFE arm is needed.
CPUS="${CPUS:-12}"
SERIAL_LOG="${SERIAL_LOG:-$ROOT/serial.txt}"
# Host address for the guest SSH forward (port 2222). The image logs in with
# the well-known admin/admin, so the default keeps it off the network; set
# SSH_ADDR=0.0.0.0 to reach the guest from another machine.
SSH_ADDR="${SSH_ADDR:-127.0.0.1}"

for f in "$QEMU" "$DISK" "$AUX" "$ROM"; do
  [ -e "$f" ] || { echo "missing: $f" >&2; exit 1; }
done
if [ -z "$ECID" ]; then
  ECID=$(python3 - "$IMAGES/config.json" <<'PY'
import base64, json, plistlib, sys
try:
    cfg = json.load(open(sys.argv[1]))
    print(plistlib.loads(base64.b64decode(cfg["ecid"]))["ECID"])
except Exception:
    pass
PY
)
fi
[ -n "$ECID" ] || { echo "no ECID: set ECID=... (see images/config.json)" >&2; exit 1; }

# The guest's shaders arrive as AIR bitcode. reims-vgpu's metal2vulkan
# disassembles them with llvm-dis and checks the SPIR-V it makes with spirv-val;
# without either tool every shader is refused and the window stays black.
for t in llvm-dis spirv-val; do
  v=METAL2VULKAN_$(tr 'a-z-' 'A-Z_' <<< "$t")
  [ -n "${!v:-}" ] || command -v "$t" >/dev/null ||
    echo "warning: $t not found, so the window will stay black: install LLVM and SPIRV-Tools, or set $v" >&2
done

# Guest writes go to an overlay; the tart image stays pristine and read-only.
if [ ! -e "$OVERLAY" ]; then
  qemu-img create -f qcow2 -F raw -b "$DISK" "$OVERLAY" >/dev/null
  echo "created overlay $OVERLAY"
fi

# --- host GPU ---------------------------------------------------------------
# An absolute ICD path is dlopen'd directly and cannot be hijacked by
# LD_LIBRARY_PATH, which matters when a second Vulkan stack (linuxbrew, conda)
# is on the search path and fails to load against the system libwayland.
[ -n "${VK_ICD_FILENAMES:-}" ] && export VK_ICD_FILENAMES
export REIMS_VGPU_WINDOW="${REIMS_VGPU_WINDOW:-1}"
# Present the scanout framebuffer as well as the composited rail. This is what
# puts the *early boot* picture on screen -- the Apple logo and progress bar are
# drawn by iBoot and the kernel through the framebuffer, long before the guest's
# graphics stack produces its first composited frame. Without it the window
# stays blank until the desktop appears.
export REIMS_VGPU_FORCE_SCANOUT="${REIMS_VGPU_FORCE_SCANOUT:-1}"

# A Wayland session needs these to be present and sane for the device's own
# window; they are inherited when the script is run from a desktop terminal and
# defaulted here when it is not (e.g. from a service or over ssh).
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export GDK_BACKEND="${GDK_BACKEND:-wayland}"

# --- Tart's version port ----------------------------------------------------
# Cirrus Labs' images run tart-guest-agent as a LaunchDaemon. Unless the guest
# has /dev/cu.tart-version-<version>, the dummy virtio-console port Tart adds,
# the agent sends SIGTERM to its parent -- launchd -- and macOS shuts down and
# restarts a couple of minutes into every boot. Give it the port. The name must
# carry a Tart version, semver 2.0.0 or later, not the agent's own version.
# The same virtio-serial device carries org.qemu.guest_agent.0, for macOS's own
# guest agent; scripts/guest.py talks to it through $ROOT/qga.sock. The agent
# runs commands as root, so the socket is as sensitive as the guest itself.

exec "$QEMU" \
  -M apple-vm,uuid="$ECID" \
  -accel tcg,thread=multi,tb-size=2048 \
  -m "$RAM" -smp "$CPUS" \
  -bios "$ROM" \
  -drive file="$AUX",if=pflash,format=raw \
  -drive file="$DISK",if=pflash,format=raw,readonly=on \
  -drive file="$AUX",if=none,id=aux,format=raw \
  -device vmapple-virtio-blk-pci,variant=aux,drive=aux,share-rw=on \
  -drive file="$OVERLAY",if=none,id=root,format=qcow2,cache=writeback,aio=threads,discard=unmap \
  -device vmapple-virtio-blk-pci,variant=root,drive=root \
  -netdev user,id=net0,hostfwd=tcp:"$SSH_ADDR":2222-:22 \
  -device virtio-net-pci,netdev=net0 \
  -chardev null,id=tartver \
  -device virtio-serial-pci,id=vser0 \
  -device virtserialport,bus=vser0.0,chardev=tartver,name=tart-version-2.41.1 \
  -chardev socket,id=qga0,path="$ROOT/qga.sock",server=on,wait=off \
  -device virtserialport,bus=vser0.0,chardev=qga0,name=org.qemu.guest_agent.0 \
  -chardev socket,id=ser0,path="$ROOT/serial.sock",server=on,wait=off,logfile="$SERIAL_LOG" \
  -serial chardev:ser0 \
  -qmp unix:"$ROOT/qmp.sock",server=on,wait=off \
  -display none \
  "$@"
