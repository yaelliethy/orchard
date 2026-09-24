/*
 * Emulate an A64 load/store whose data abort carried no instruction syndrome.
 *
 * Copyright (c) 2026 Youssef Elliethy (yaelliethy)
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef TARGET_ARM_EMULATE_LDST_H
#define TARGET_ARM_EMULATE_LDST_H

#include "cpu.h"

/*
 * Perform one data access of @size bytes at guest-physical @pa: store *@val,
 * or load into it. Return false if nothing at @pa accepted the access.
 */
typedef bool (*ARMLdstAccessFn)(uint64_t pa, uint64_t *val, unsigned size,
                                bool is_write);

/*
 * A data abort taken on a load/store with writeback (pre/post-index) or on a
 * register pair has ISV=0: the hardware reports the address but not the
 * register, size or direction, so a hypervisor cannot emulate the MMIO access
 * from the syndrome alone. Decode @insn, the instruction at env->pc whose
 * access faulted at @fault_pa, and perform it through @access against the
 * general-purpose registers in @env, including any base-register writeback.
 * On success the PC is advanced past the instruction and true is returned;
 * forms outside the integer pre/post-index and pair classes return false and
 * leave @env untouched.
 */
bool arm_emulate_ldst(CPUARMState *env, uint32_t insn, uint64_t fault_pa,
                      ARMLdstAccessFn access);

#endif
