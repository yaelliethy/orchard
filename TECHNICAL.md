# How this works

Three things have to be true at once for a stock macOS arm64 kernel to run on a
Linux host: the **boot chain** must complete, the **CPU** must behave like an
Apple virtual machine, and the **GPU** must answer Apple's paravirtual graphics
protocol. This document takes them in that order, then lists what we changed and
what is still broken.

---

## 1. The boot chain

Apple's VM firmware chain is unmodified except for one instruction pair:

```
AVPBooter (-bios)  →  iBootStage1 (LLB)  →  iBootStage2 (iBoot)  →  XNU
```

Each stage verifies the next, so nothing downstream of AVPBooter can be patched
without breaking image4 verification — and none of it is. What makes the chain
work is supplying the three things it expects to find:

**The real installed disk.** The tart image has the layout `fsboot` requires:
GPT part0 `iBootSystemContainer` (0.5 GB, APFS) + part1 `Container` (41 GB,
APFS with System/Data/Preboot/Recovery). A BaseSystem-only image can never boot
this way — no Preboot, no LocalPolicy, no `iBoot.img4`. It is attached **twice**:
as the root `pflash` (LLB reads boot objects through that) and as
`vmapple-virtio-blk-pci,variant=root`.

**The image's own NVRAM.** `aux.img` carries the boot variables *and* the
LocalPolicy iBoot checks. tart publishes it with a **16 KiB header**, so every
CHRP bank sits `0x4000` too high until it is stripped;
`fetch-tart-image.py` detects and removes it (and says so), because attached
unstripped the guest never finds its boot variables. A synthesised aux carrying only LLB makes LLB reload
itself forever (measured: 226 identical `type illb` iterations).

**The image's own ECID.** The on-disk LocalPolicy is personalised to it, so the
machine is started as `-M apple-vm,uuid=<ECID>`; `fetch-tart-image.py` prints
the ECID from the image's config layer.

### The one firmware patch

**The patch is not ours.** It comes from NyanSatan's
[Virtual-iBoot-Fun](https://github.com/NyanSatan/Virtual-iBoot-Fun), which
worked out what AVPBooter checks and which routine has to be neutered to run it
outside Apple's own hypervisor. Our contribution is only the script that applies
it safely to a firmware image you supply.

`scripts/patch-avpbooter.py` replaces the first two instructions of that routine
(`pacibsp; sub sp, sp, #0x10` at file offset `0x2314`) with `mov x0, #0; ret`.
That routine does not return success on this machine, and the boot stops before
LLB is loaded. Everything after it runs stock. The script checks the bytes at
that offset before writing, so a firmware build it does not recognise is refused
rather than corrupted.

### Boot arguments

There is no `-append` on this path: boot-args come from NVRAM. The aux holds two
CHRP banks (at `0xa00000` length `0x2000`, and `0xa80000` length `0x80000`),
each with an adler32 over `[base+0x14, base+banklen)` stored LE at `base+0x10`.
The live one is the bank with the higher generation.

**Append-only works; re-serialising the entry list does not.** A rewritten list
makes iBoot reject the NVRAM, set `iboot-failure-reason=0x65` and
`boot-command=recover`, and loop (measured: 523 iterations of stage 1, no stage
2). `scripts/prepare-aux.py` therefore appends one `KEY=VALUE\0` after the last
entry and fixes only that bank's checksum — 25 bytes differ for
`boot-args=-v serial=3`.

---

## 2. The CPU

The guest kernel is `RELEASE_ARM64_VMAPPLE` — built for Apple's *virtual*
machine, so it does not use the GXF/SPRR/APRR machinery a bare-metal Apple
kernel needs. It needs real pointer authentication, a small set of Apple
hypercalls, a GIC-delivered timer and PSCI — all of which a generic arm64
hypervisor can provide. This tree carries **no Apple-specific CPU emulation**:
`target/arm` is upstream QEMU apart from a small SMCCC hook.

### Pointer authentication is real, not a no-op

The stock kernel verifies its own signed pointers, so a no-op PAC cannot satisfy
it — it dies with *"JOP Hash Mismatch Detected (PC, CPSR, or LR corruption)"*.
`apple-vm-cpu` selects QEMU's real PAC with the implementation-defined hash
(`pauth-impdef`): self-consistent like QARMA5, and cheaper.

It is plain architected PAC. A scan of every stage for PAC register accesses
settles what the guest touches:

| Image | Writes APIA/APIB/APDA/APDB/APGA | Touches Apple `KERNELKEY*`/`APCTL`/`APCFG` | Sets `SCTLR_EL1` EnIA/IB/DA/DB |
|---|---|---|---|
| AVPBooter, LLB, iBoot | once, on the boot CPU | no | yes |
| XNU kernelcache | never | no (one read, in a register-dump path) | yes |

The kernel never writes a key register. Everything else it wants from PAC it
asks the hypervisor for.

### PAC keys come from the hypervisor

xnu's `VMAPPLE` config sets `HAS_PARAVIRTUALIZED_PAC` (macOS 13 through 26), and
the kernel issues "Apple CPU service" SMCCC calls over `hvc #0`
(`osfmk/arm64/hv_hvc.h`). `hw/vmapple/hvc.c` implements that contract with
architected PAC only:

| Call | Where the kernel makes it | What this tree does |
|---|---|---|
| `0xc1000000` `PAC_SET_INITIAL_STATE` | each core's start path in `start.s`, MMU off, before SCTLR enables PAC | boot CPU: record the keys firmware left; any other core: install them |
| `0xc1000001` `PAC_GET_DEFAULT_KEYS` | once | return a random, **non-zero** ROP/JOP pid pair (x2/x3) |
| `0xc1000002`–`06` key and EL0-diversifier switches | every context switch | succeed and change nothing |

A core started by PSCI `CPU_ON` leaves reset with zero keys; `SET_INITIAL_STATE`
is where Apple's hypervisor hands it the machine's, and so is where this tree
does. Ignoring the per-process switches means the kernel and every process
share one key set: signing stays self-consistent and only cross-process PAC
isolation is given up.

Measured details that fixed the contract:

* **Every call returns 0.** xnu 26 spins on `cbnz x0, .` after most of them;
  Ventura tolerates `NOT_SUPPORTED`, 26 does not.
* **The default pids must be non-zero.** With zero pids launchd dies with SIGBUS
  right after dyld (*"initproc failed to start -- exit reason namespace 2
  subcode 0xa"*) and the guest reboot-loops.
* Per-process diversifier calls are frequent — about 460k
  `SET_EL0_DIVERSIFIER_AT_EL1` in 90 s on cpu0 — which matters for the exit cost
  under a hardware accelerator.

The calls reach the service through `target/arm/smccc.{c,h}`, a board-level
handler that TCG's PSCI path (`target/arm/tcg/psci.c`) offers every call to
first, and that KVM and WHPX offer the calls their hypervisor forwards (see
*Performance, and the KVM question*).

### The timer is a GIC interrupt

The virtual timer is wired to GIC PPI 27, exactly as upstream QEMU wires it.
xnu programs the GIC for it itself — `pe_init_fiq()` (`pexpert/arm/pe_fiq.c`)
puts PPIs 25–30 in group 0 (`GICR_IGROUPR0 = 0x81FFFFFF`), enables PPI 27, and
sets `GICD_CTLR.EnableGrp0` and `ICC_IGRPEN0_EL1` — and GICv3 signals group 0 as
FIQ, which is where xnu's `sleh_fiq` looks for the timer. That is also how KVM
and WHPX deliver it, so nothing Apple-specific is needed on either side.

### The counter rate and the device tree

xnu takes its timebase from `/cpus/<cpu>/timebase-frequency` in the device tree
(the running CPU's node), falling back to a built-in 24 MHz; it never reads
`CNTFRQ_EL0` (`pexpert/arm/pe_identify_machine.c`). Apple's device tree says
24 MHz, and iBoot does not correct it.

Under TCG the counter is emulated at 24 MHz, so they agree. Under a hardware
accelerator the guest reads the host's counter, which no hypervisor can
rescale. So on the boot CPU's `SET_INITIAL_STATE` — before `arm_init()` reaches
`pe_identify_machine()` — `hvc.c` rewrites every `timebase-frequency` record
(exact name, length 8, value 24000000) in the device tree iBoot already loaded
and verified, to the real counter rate. It does nothing when the counter is
24 MHz.

Measured under TCG with the counter at 19.2 MHz, guest clock against host clock
over 120 s: **1.0006 ± 0.003** with the rewrite (`hw.tbfrequency` 19200000),
**0.801 ± 0.0003** without it.

### How many vCPUs

12, measured: desktop in ~75 s on the reference host.

There used to be a ceiling of 6. Above it the boot livelocked with zero disk
I/O, because XNU parks the cores it does not bring online in a `wfe; ldxr; cbnz`
pen and WFE is unmodelled under MTTCG, so each parked core burned a host CPU. A
WFE-parking patch was written for that and is **not in this tree**: the livelock
turned out to be secondaries running without PAC keys and faulting in the pen.
With keys installed at `SET_INITIAL_STATE` they leave it, and 12 vCPUs boot
with no WFE arm at all.

MTTCG costs still grow with vCPU count (every added core costs `CF_PARALLEL`
atomics on exclusive ops and turns each guest TLBI into an all-thread
rendezvous), so more is not automatically better; 12 is where this host
measured best.

---

## 3. The GPU

`reims-vgpu` ([steelbrain/reims-vgpu](https://github.com/steelbrain/reims-vgpu))
implements Apple's **ParavirtualizedGraphics** device: it decodes
the guest's command stream, translates Apple's AIR shaders to SPIR-V
(`metal2vulkan`), and renders on the host's Vulkan. The composited frame is
presented in its own window.

Two details shape everything else:

**Guest RAM is the GPU's memory.** The whole RAMBlock is imported once through
`VK_EXT_external_memory_host`, so a render target created over guest pages is
written by the host GPU directly — one copy of surface content, not two. This is
why `-m 8G` is a hard requirement rather than a preference: if the RAMBlock does
not fit the host's importable heap the import is refused
(`guest_ram_map_import_exceeds_heap`) and every bind falls back to a CPU byte
loader — slow, with black backgrounds.

**Plain anonymous guest RAM, not memfd.** A `memory-backend-memfd` RAMBlock
would let QEMU alias fragmented guest page runs into one contiguous host view
(`qemu_map_pages_alias_failed` disappears), but amdgpu's userptr rejects shared
file mappings, so the whole-RAM import then fails with
`ERROR_INVALID_EXTERNAL_HANDLE` — which costs far more than the aliases buy. The
alias failures in the log on an anonymous-RAM host are **expected**; those maps
take the copying rails.

---

### Where the firmware comes from, and why that matters

Upstream QEMU's vmapple documentation tells you to copy `AVPBooter.vmapple2.bin`
off a Mac, and that requirement is what made this route "needs Apple hardware to
set up" even though it runs on Linux. It does not: `Virtualization.framework`
lives in macOS, the macOS image is macOS, so the firmware is in the image.

Verified rather than assumed — the copy in the image and a copy from a Mac
differ by **six bytes at offset 657**, which is the version string
(`iBoot-8422.141.2.700.1` against a shorter one, zero-padded); the code is
byte-identical and the patch site at `0x2314` is the same. A boot from the
image-extracted firmware reaches the desktop in 105 s.

---

## What is ours

This project is not an assembly of other people's parts. Upstream QEMU can boot
a macOS 12 guest on a **macOS host with HVF**, where Apple's own hypervisor
supplies the CPU behaviour and Apple's ParavirtualizedGraphics framework
supplies the GPU. Neither exists on Linux. The work below is what stands
between that and a rendered desktop on a Linux/x86-64 box.

**Not the GPU.** `reims-vgpu` is
[steelbrain/reims-vgpu](https://github.com/steelbrain/reims-vgpu) — an
independent implementation of Apple's ParavirtualizedGraphics device, and the
single largest component here. It is included and wired into QEMU by this tree,
not written by it.

**Running Apple's firmware chain under emulation.** Getting `AVPBooter → LLB →
iBoot → XNU` to complete on TCG meant finding, one boot at a time, what the
chain requires: the disk attached both as pflash and as virtio so `fsboot` can
read boot objects; the image's *own* NVRAM, because the LocalPolicy is
personalised; the image's *own* ECID passed as the machine UUID; and NVRAM
edited append-only, because a re-serialised variable list makes iBoot set
`iboot-failure-reason=0x65` and loop forever. Each of those is a measured fact
with a failure mode attached, and none of them is documented anywhere.

**Apple's hypervisor contract, on architected hardware.** The vmapple kernel
leans on its hypervisor for PAC keys, and its firmware assumes a 24 MHz
counter. Finding that out — from the binaries and xnu's source rather than by
emulating Apple's PAC registers — let this tree drop every Apple-specific CPU
change: the hypercall service in `hw/vmapple/hvc.c` hands each core its keys,
answers the per-process key calls, and corrects the device-tree timebase, with
upstream `target/arm`. The same service is reachable from KVM and WHPX, which is
what makes an arm64 host plausible at all.

**The device model gaps.** Upstream's `bdif` silently drops NVRAM block writes,
so a guest can never persist a boot variable; ours implements the write path.
`cfg` needed a guest-visible random seed at a fixed offset. And the GICv2m
needed bit 31 of `V2M_MSI_TYPER` before macOS would use the MSI frame at all.

**Portability work on upstream itself.** `CONFIG_VMAPPLE` was gated on HVF and
shipped off; `hw/vmapple/aes.c` contains a VLA that only ever compiled under
clang. Those are upstream bugs or upstream assumptions that a Linux host exposes
for the first time.

**The operating knowledge.** Which of these matter is itself a result: 10 GB of
guest RAM because the RAMBlock must fit one Vulkan heap; plain anonymous RAM
rather than memfd because amdgpu's userptr rejects shared file mappings; 12
vCPUs because MTTCG's costs overtake the parallelism past that; and leaving
guest state alone across restarts because macOS finishes its install phases
across reboots.

---

## What we changed in QEMU

Against upstream QEMU master. `target/arm` carries no Apple-specific CPU
emulation; the vmapple changes live in `hw/vmapple`.

| File | What and why |
|---|---|
| `hw/vmapple/vmapple.c` | The `apple-vm` machine: the reims-vgpu device, CHRP NVRAM building, the GICv2m/MSI wiring newer guests need, a 24 MHz counter under TCG, and the GIC layout published as `gic-dist-base`/`gic-redist-base`/`gic-its-base` properties for WHPX. |
| `hw/vmapple/hvc.c` (new) | The Apple hypercall service: PAC keys per core, the per-process key calls, and the device-tree timebase rewrite (§2). |
| `hw/vmapple/bdif.c` | The backdoor interface: **NVRAM block writes** (upstream drops them, so the guest could never persist a boot variable), plus the virtual-USB queue lifecycle. |
| `hw/vmapple/cfg.c` | A guest-visible 64-byte random seed at the fixed offset `0x5c8`, and `production-mode` as a property. |
| `hw/vmapple/apple_vm_cpu.c` (new) | The `apple-vm-cpu` type: a thin subclass of `max` that selects real PAC. |
| `hw/display/reims-vgpu-*.c` (new) | The QEMU side of the GPU: MMIO and PCI transports, the dirty-page bridge, and the C↔Rust shim. |
| `hw/intc/arm_gicv2m.c` | A `macos-compat` property that sets bit 31 of `V2M_MSI_TYPER`; macOS will not use the MSI frame without it. Off by default. |
| `target/arm/smccc.{c,h}` (new), `target/arm/tcg/psci.c` | A board-level handler for non-PSCI SMCCC calls, offered each call before PSCI. |
| `target/arm/emulate-ldst.{c,h}` (new) | Decode and perform an A64 load/store whose data abort had no syndrome (ISV=0: pre/post-index and pairs), for KVM and WHPX. |
| `target/arm/kvm.c`, `kvm_arm.h`, `kvm-stub.c`, `hw/intc/arm_gicv3_kvm.c` | `kvm_arm_smccc_forward()` (an SMCCC filter forwarding a range to userspace), `KVM_EXIT_HYPERCALL` handling, and `KVM_EXIT_ARM_NISV` emulation that routes GIC addresses to the in-kernel vGIC (`kvm_arm_gicv3_mmio()`). **Not run yet.** |
| `target/arm/whpx/whpx-all.c`, `whpx_arm.h`, `hw/intc/arm_gicv3_whpx.c` | Hypercall exits, ISV=0 MMIO emulation instead of an assert, and the GIC distributor/redistributor/ITS addresses taken from the machine instead of the `virt` layout. **Not compiled or run yet.** |
| `hw/vmapple/Kconfig` | Selects `ARM_GIC`, so the accelerator's in-kernel GIC device is built for vmapple on its own. |

What we deliberately left out: the direct-kernelcache (`-kernel`) boot path from
the [Inferno](https://github.com/ChefKissInc/Inferno) fork, which needs its
Apple-silicon device-tree and Mach-O loader, and Inferno's emulation of Apple's
PAC registers, which this guest never touches (§2).
This tree boots the real firmware chain instead, so that dependency is gone.

---

## Porting notes

Upstream gates `CONFIG_VMAPPLE` on `depends on HVF` and ships
`CONFIG_VMAPPLE=n` for aarch64, because its own vmapple support assumes a macOS
host virtualising Apple silicon natively. This tree drops that dependency and
turns the machine on — running it under TCG on Linux is the point of the
project — and selects `REIMS_VGPU` where upstream selects `MAC_PVG_MMIO`.

Details the ported code had to be adapted to, since upstream has moved on since
the fork:

* headers moved: `hw/sysbus.h` → `hw/core/sysbus.h`, `block/aio.h` →
  `qemu/aio.h`, and similar for `boards`/`irq`/`loader`/`qdev-properties`/`usb`;
* the console and input APIs were renamed to what our compatibility macros
  already called them (`graphic_console_init` → `qemu_graphic_console_create`,
  `dpy_gfx_replace_surface` → `qemu_console_set_surface`, and so on), so the
  shims were deleted rather than extended; `qemu_input_event_send_key_qcode` is
  gone and `qemu_input_event_send_key_linux` does the evdev translation itself;
* `cpu_physical_memory_get_dirty_flag` is now `physical_memory_get_dirty_flag`
  in `system/physmem.h`;
* `CharBackend` is now `CharFrontend` (the `qemu_chr_fe_*` entry points keep
  their names);
* `TYPE_VMAPPLE_AES` is now `TYPE_APPLE_AES`, and `hw/arm/primecell.h` is gone;
* the RAMBlock queries moved to `system/ramblock.h`. This one is worth stating
  plainly because it does not fail the build: with the declaration missing, C
  defaults `qemu_ram_block_from_host` to returning `int`, the `RAMBlock *` is
  truncated to 32 bits, and QEMU segfaults ~80 s into the guest boot the first
  time the device maps a fragmented page run. Configuring with
  `--disable-werror` turns that into a warning you will scroll past;
* `hw/vmapple/aes.c` declares `static const size_t MAX_LEN` and then sizes an
  initialised array with it, which is a VLA in C and which GCC refuses. It only
  ever compiled because upstream builds vmapple with clang on macOS; this tree
  makes it an `enum`. That one is worth sending upstream.

---

## Environment switches

Every switch the QEMU side reads, and nothing else — the diagnostic arms from
the bring-up (forced GFX stub, read-only NVRAM, forced dirty answers, QARMA
hashing, per-command tracers, and the PAC, timer and hypercall-mode switches)
were removed once they had answered their questions.

| Variable | Effect |
|---|---|
| `ORCHARD_DIRTY_DEBUG=1` | Prints the dirty-tracking harvest split. Diagnostic; no effect on behaviour. |
| `REIMS_VGPU_WINDOW=1` | The GPU opens its own window. Set by `run-vm.sh`. |
| `REIMS_VGPU_FORCE_SCANOUT=1` | Also enqueue the scanout framebuffer, not only the composited rail. This is what shows the *early boot* picture — the Apple logo and progress bar are drawn through the framebuffer by iBoot and the kernel, before the guest's graphics stack produces a composited frame. Set by `run-vm.sh`; it is the one change this tree carries against `reims-vgpu`. |

`reims-vgpu` reads further `REIMS_VGPU_*` variables of its own; see that
project.

## Known problems

These are open, measured, and not worked around. A run that hits them is
behaving as expected for this tree.

### The guest restarts a few times before it settles

**Wait through 2–5 restarts on a fresh image.** macOS completes install and
update sequences *across* reboots (installer phases, MobileSoftwareUpdate,
firmware update) and records its progress in NVRAM and on disk. Until those
phases are satisfied the guest keeps restarting — and it converges only if the
state is left alone. Resetting the aux or the overlay between boots wipes the
progress and the guest retries phase 1 forever, which looks exactly like a
permanent reboot loop. Real hardware (and tart) persists both; so must you.

A restart takes 200–340 s of guest time and is kernel-initiated and orderly.

### A GPU page-table panic

Occasionally, with the desktop up, the guest panics:

```
panic(cpu N caller ...): "hitting assertion" @AppleParavirtPageTable.cpp:210
   apvAssert(vmNode->nodes[index])
```

Disassembled, line 210 is the release path refusing to clear a PTE word that is
already zero, in the Dock's GPU work. Either the guest released the same range
twice (its `release_pte` has no re-entry guard) or a host write reached a page
holding PTEs; the device's own `node_guard` alarm has never fired, but its
coverage is thin. Unresolved.

### Graphics defects

* **Anything with a corner radius fills black.** A partial draw whose colour
  attachment declares `DontCare` and for which no seed resolved keys
  `AttachmentLoadOp::DONT_CARE` against `initialLayout = UNDEFINED`, which
  licenses the driver to discard the contents — so texels outside the draw's
  scissor are lost. Measured on one boot: 718 such draws, ~5.0 M texels, every
  one of them `DontCare` into a guest-backed target. The repair is to preserve
  from the engine resident when it holds the target's contents; two attempts at
  it both hung the same conformance case and were withdrawn.
* **The wallpaper is striped** with 1px dark rows, periodic at ~8.5 rows. They
  are already present in the texture the guest hands the device, they are not
  in the artwork, and coverage, interpolation precision, compute, page
  boundaries and codec blocks have each been eliminated by measurement. Open.
* **Pointer input does not reach the guest.** Keyboard does.

### Host-side capture

The composited frame lives only in the device's Vulkan window, and GNOME
refuses screenshots to non-portal clients. What works:

* **Mutter ScreenCast over PipeWire** (a D-Bus `RecordMonitor` session piped into
  `gst-launch-1.0 pipewiresrc`) captures the whole monitor, so the guest window
  has to be in front.
* `REIMS_VGPU_PRESENT_DUMP=<path>` writes a presented frame as PPM, but only for
  presents that carry CPU pixels. The normal window path presents zero-copy
  from the GPU resident, so on this host it writes nothing.
* A QMP `screendump` reads guest pages and shows a *different* image.

For anything that is not a pixel question, use the guest itself: `run-vm.sh`
forwards host port 2222 to the guest's SSH (`ssh -p 2222 admin@127.0.0.1`, the
image's `admin`/`admin`). The first login after boot takes one to three minutes
under TCG while launchd starts sshd. The forward listens on all host interfaces.

### Performance, and the KVM question

On an x86-64 host this is TCG only — the guest is arm64, so there is nothing to
accelerate. Expect minutes to the desktop and a compositor that is usable but
not smooth.

**On an arm64 host, the guest needs nothing a generic hypervisor cannot
give — but no accelerated boot has been run yet.** What is established, and
what is not:

* **No Apple CPU features.** The firmware touches no Apple IMPDEF register; the
  kernel's Apple `c15` accesses (performance counters, a register dump) never
  run — the TCG boot uses upstream `target/arm`, which faults on any register it
  does not implement, and still reaches the desktop. KVM traps IMPDEF registers
  (`HCR_EL2.TIDCP`), so any that did run would show up at once.
* **PAC and the timer are standard** (§2): architected PAC with keys handed out
  at `SET_INITIAL_STATE`, and the timer as a group-0 GIC interrupt.
* **KVM (stock Linux, no host patch).** `hvc.c` calls `kvm_arm_smccc_forward()`
  at machine init, installing an SMCCC filter (`KVM_SMCCC_FILTER_FWD_TO_USER`,
  Linux 6.4+) for the Apple range `0xc1000000`–`0xc100ffff`; KVM reserves only
  the Arm architecture range, so the filter is allowed. Calls arrive as
  `KVM_EXIT_HYPERCALL` with the PC already past the `hvc`; PAC keys written into
  `env` reach the vCPU because every KVM system register, the key registers
  included, is written back at `KVM_PUT_RUNTIME_STATE`. The counter rate comes
  from the host's `CNTFRQ_EL0`. The code is syntax-checked against the arm64 KVM
  headers; it has not run here. The same forwarding has run elsewhere:
  [steelbrain/experiment-macOS-arm64-on-asahi-linux-arm64](https://github.com/steelbrain/experiment-macOS-arm64-on-asahi-linux-arm64)
  boots Ventura to Setup Assistant on KVM (Asahi, M2 Pro) with an SMCCC filter
  on `0xc1000000` answered in QEMU. It implements Apple's full key model on the
  Apple-silicon `KERNKEY`/`APCTL` registers through a host kernel patch, which
  a generic arm64 host cannot do; this tree's shared key set is the generic
  form. Its `GET_DEFAULT_KEYS` also returns fixed non-zero values, and it pins
  PSCI to 1.1 for the same ApplePSCI reason this tree does.
* **One MMIO store KVM cannot decode, now emulated in QEMU.** xnu's
  `pe_init_fiq()` writes `GICR_IGROUPR0` with a pre-indexed store
  (`str w9, [x8, #0x80]!`). A store with writeback carries no instruction
  syndrome (data abort with ISV=0), so KVM cannot emulate it against its
  in-kernel GIC and returns `KVM_EXIT_ARM_NISV`. A TCG trace of every
  `GICR_IGROUPR0` store with its instruction confirms the guest does it on all
  12 cores every boot; the firmware's and the kernel's other writes to that
  register are plain stores KVM handles itself. The steelbrain project rewrites
  the instruction in guest RAM at handoff. Here `target/arm/emulate-ldst.c`
  decodes it (pre/post-index and pair forms, tested standalone against the
  exact instruction), `kvm_arm_gicv3_mmio()` applies the access to the vGIC
  through its redistributor/distributor device attributes, anything else goes
  to QEMU's MMIO, and the base register and PC are updated. Not run on KVM
  yet.
* **WHPX (Hyper-V).** ISV=0 aborts on QEMU-owned MMIO now go through the same
  decoder instead of an assert; VirtualBox's WHP arm64 backend decodes the same
  classes (a pre-indexed `ldrb` from its UEFI flash, an `ldp` from Windows' TPM
  driver), confirming WHP delivers them to the VMM with the final address.
  Whether Hyper-V's own GIC decodes xnu's pre-indexed `GICR_IGROUPR0` store is
  unknown. Hypercall exits are enabled through
  `WHvPartitionPropertyCodeExtendedVmExits` (`HypercallExit`), and the GIC
  distributor, per-vCPU redistributor (`WHvArm64RegisterGicrBaseGpa`) and ITS
  addresses come from the machine's properties rather than the `virt` layout.
  Checked against Microsoft's OpenVMM, which drives WHP on arm64: it enables the
  same exit, receives it with the PC still *at* the `hvc` and advances it by 4
  itself (as this tree does), identifies `hvc #0` as the SMCCC convention, and
  passes `GitsTranslatorBaseAddress = 0` for a machine that uses a GICv2m frame
  (as vmapple does). **Still open, and probably not:** whether Hyper-V forwards
  an SMCCC function ID it does not own, such as Apple's `0xc1…`. The evidence:
  - VirtualBox's WHP arm64 backend (`NEMR3Native-win-armv8.cpp`) enables
    `HypercallExit` and has a PSCI handler for it, yet states that *"Hyper-V
    doesn't cause VM exits for PSCI calls"* and that *"APs can't be brought
    online by the guest due to missing PSCI VM exits"*: the exit is not a
    generic SMCCC trap even when enabled. Its handler also reads the function
    ID from the `hvc` immediate rather than x0 (flagged "probably wrong" in the
    source), which suggests it has never seen real SMCCC traffic.
  - Microsoft's TLFS documents one SMCCC ID for Hyper-V (`0x46000001`,
    vendor-hypervisor function 1) and says nothing about others. Every
    Microsoft VMM that handles the arm64 hypercall intercept (OpenVMM's WHP,
    `mshv` and OpenHCL backends) treats every forwarded call as a Hyper-V
    hypercall without looking at x0, and WHP describes the exit in terms of
    Hyper-V hypercalls it delegates (`HvPostMessage`, unknown `HvSignalEvent`
    connections).
  - Without the exit enabled, Hyper-V answers unknown SMCCC IDs itself: QEMU's
    WHPX and `mshv` arm64 ports treat unexpected exits as fatal yet boot Linux,
    which probes SMCCC services Hyper-V does not implement. If Hyper-V also answers Apple's
  IDs itself with the exit enabled, secondaries get no keys from
  `SET_INITIAL_STATE`, the device tree keeps 24 MHz, and macOS 26 spins on its
  `cbnz x0, .` checks; Ventura tolerates the replies. This file needs the
  Windows SDK and has not been compiled here.
* **Host counter rate and 16K pages** are properties of the laptop, not of this
  tree: the device-tree rewrite handles any counter rate, and the CPU must
  support the 16K translation granule the guest uses.
* Everything else — the devices, the NVRAM handling, and the GPU (which renders
  through the host's Vulkan either way) — is host-side and indifferent to the
  accelerator.

So: **the known blockers are gone on paper, and nothing accelerated has booted
yet.** Do not read the above as a claim that it works.
