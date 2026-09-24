/*
 * Apple VM hypercall services (SMCCC over HVC #0).
 *
 * Copyright (c) 2026 Youssef Elliethy (yaelliethy)
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_VMAPPLE_HVC_H
#define HW_VMAPPLE_HVC_H

/* Install the handler; call once from machine init. */
void vmapple_hvc_init(void);

/* Forget per-boot state; call from the machine reset handler. */
void vmapple_hvc_reset(void);

#endif /* HW_VMAPPLE_HVC_H */
