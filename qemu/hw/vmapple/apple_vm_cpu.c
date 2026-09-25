/*
 * Apple Virtual Machine CPU model.
 *
 * A thin subclass of the ARM `max` core for the Orchard `apple-vm` machine.
 * `max` gives us the AArch64 feature set XNU needs (PAC, the v8.x extensions)
 * and — unlike apple-a13/apple-m2 — realizes without any SoC-specific AIC2 /
 * fiq-or wiring, so it drops straight into a GICv3-based virtual machine.
 *
 * Its one setting is real PAC under TCG; everything else vmapple needs from
 * the CPU is architected (see hw/vmapple/hvc.c).
 *
 * `wfe-monitor-ns` (target/arm) is left at 0. A bounded WFE sleep in XNU's
 * LDXR;WFE lock loops cut idle host CPU but measured no faster to the desktop
 * (238 s at 0 against 261-337 s at 20-200 us); set it with
 * -global apple-vm-arm-cpu.wfe-monitor-ns=N to trade latency for idle power.
 *
 * Copyright (c) 2026 Youssef Elliethy (yaelliethy)
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qom/object.h"
#include "system/tcg.h"
#include "hw/vmapple/apple_vm_cpu.h"

static void apple_vm_cpu_initfn(Object *obj)
{
    if (tcg_enabled()) {
        /*
         * The stock kernel verifies its own signed pointers ("JOP Hash
         * Mismatch Detected" under pauth-noop), so PAC has to be real. The
         * implementation-defined hash rather than architected QARMA5: both
         * are self-consistent, so the guest cannot tell them apart by signing
         * and authenticating, and this one is faster.
         */
        object_property_set_bool(obj, "pauth-impdef", true, NULL);
    }
}

static const TypeInfo apple_vm_cpu_type_info = {
    .name = TYPE_APPLE_VM_CPU,
    .parent = ARM_CPU_TYPE_NAME("max"),
    .instance_init = apple_vm_cpu_initfn,
};

static void apple_vm_cpu_register_types(void)
{
    type_register_static(&apple_vm_cpu_type_info);
}

type_init(apple_vm_cpu_register_types)
