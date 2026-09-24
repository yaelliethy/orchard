# Attribution

This tree combines three bodies of work. Earlier versions also carried Apple
pointer-authentication register emulation derived from
[Inferno](https://github.com/ChefKissInc/Inferno); the guest never uses those
registers, and that code has been removed.

## QEMU

`qemu/` is a checkout of [upstream QEMU](https://github.com/qemu/qemu) with the
changes listed in `TECHNICAL.md` applied. QEMU is licensed GPL-2.0-only with
parts under compatible licences; see `qemu/LICENSE`. Our changes are
GPL-2.0-or-later.

The upstream `vmapple` machine (`hw/vmapple/`) is by Alexander Graf and
contributors; this tree extends it.

## reims-vgpu

`reims-vgpu/` is **[steelbrain/reims-vgpu](https://github.com/steelbrain/reims-vgpu)**,
not our work. It is the paravirtual GPU: Apple's ParavirtualizedGraphics
protocol, AIR shader translation (via the `metal2vulkan` crate) and a Vulkan
renderer. Licensed **LGPL-3.0** (see `reims-vgpu/LICENSE`). This tree includes
it and links it into QEMU as a static library through the C ABI in
`reims-vgpu/crates/reims-vgpu/include/reims_vgpu_qemu_abi.h`; the QEMU-side
transports (`hw/display/reims-vgpu-*.c`) are ours.

## Virtual-iBoot-Fun

The AVPBooter patch — which routine has to be stubbed for Apple's VM firmware to
run outside Apple's own hypervisor — is from NyanSatan's
[Virtual-iBoot-Fun](https://github.com/NyanSatan/Virtual-iBoot-Fun). Ours is
only `scripts/patch-avpbooter.py`, which applies it to a firmware image the user
supplies after checking that the bytes at the site are what the patch expects.

## Not distributed here

* **AVPBooter** — Apple firmware. Extracted at setup time from the macOS image
  you download, where `Virtualization.framework` carries it.
* **macOS** — fetched at run time from the public `cirruslabs/macos-ventura-base`
  image. Apple's licence permits macOS virtualisation only on Apple-branded
  hardware; check your use against it.
