/*
 * Board-level handler for SMCCC calls that are not PSCI.
 *
 * A board whose guest firmware or kernel makes vendor hypercalls (for example
 * Apple's VM PAC services) registers one handler here. TCG's HVC path offers
 * every call to it before PSCI; KVM and WHPX offer the calls their hypervisor
 * forwards to QEMU. One handler serves all three.
 *
 * Copyright (c) 2026 Youssef Elliethy (yaelliethy)
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef TARGET_ARM_SMCCC_H
#define TARGET_ARM_SMCCC_H

#include "cpu.h"

/*
 * @x is the vCPU's x0..x7. A handler that claims the call writes its results
 * to x[0..3] and returns true; one that does not returns false and leaves @x
 * untouched.
 */
typedef bool (*ARMSMCCCHandler)(ARMCPU *cpu, uint64_t x[8]);

void arm_smccc_set_handler(ARMSMCCCHandler fn);

/* Offer the call in @cpu's x0..x7 to the handler; true if it was served. */
bool arm_smccc_dispatch(ARMCPU *cpu);

#endif
