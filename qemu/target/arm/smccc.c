/*
 * Board-level handler for SMCCC calls that are not PSCI.
 *
 * Copyright (c) 2026 Youssef Elliethy (yaelliethy)
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "target/arm/smccc.h"

static ARMSMCCCHandler smccc_handler;

void arm_smccc_set_handler(ARMSMCCCHandler fn)
{
    smccc_handler = fn;
}

bool arm_smccc_dispatch(ARMCPU *cpu)
{
    return smccc_handler && smccc_handler(cpu, cpu->env.xregs);
}
