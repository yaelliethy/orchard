/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * WHPX support -- ARM specifics
 *
 * Copyright (c) 2025 Mohamed Mediouni
 *
 */

#ifndef QEMU_WHPX_ARM_H
#define QEMU_WHPX_ARM_H

#include "target/arm/cpu-qom.h"

uint32_t whpx_arm_get_ipa_bit_size(void);
void whpx_arm_set_cpu_features_from_host(ARMCPU *cpu);

/*
 * Guest-physical address of a GIC region: the machine's @prop property
 * ("gic-dist-base", "gic-redist-base", "gic-its-base") when the board
 * publishes one, otherwise @virt_default, the `virt` machine's layout.
 */
uint64_t whpx_arm_gic_addr(const char *prop, uint64_t virt_default);

#endif
