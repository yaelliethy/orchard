/*
 * Emulate an A64 load/store whose data abort carried no instruction syndrome.
 *
 * Handled encodings (Arm ARM C4.1.x, "Loads and Stores"), general-purpose
 * registers only:
 *   - LDR/STR/LDRS* (immediate), 9-bit signed offset: unscaled, post-index,
 *     pre-index, unprivileged;
 *   - LDP/STP/LDPSW: offset, post-index, pre-index, no-allocate.
 * Register 31 is SP as a base and XZR as a data register.
 *
 * Copyright (c) 2026 Youssef Elliethy (yaelliethy)
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "qemu/bitops.h"
#include "target/arm/emulate-ldst.h"

enum { ADDR_OFFSET, ADDR_PRE, ADDR_POST };

/*
 * Addressing mode by the 2-bit field both classes share (idx for single,
 * bits [24:23] for pairs): 00 offset/no-allocate, 01 post-index, 10 offset or
 * unprivileged, 11 pre-index.
 */
static const int addr_modes[4] = {
    ADDR_OFFSET, ADDR_POST, ADDR_OFFSET, ADDR_PRE,
};

typedef struct LdstOp {
    bool load;
    bool sign;          /* sign-extend loaded data */
    bool dst64;         /* X (not W) destination */
    unsigned size;      /* bytes per element */
    unsigned nregs;     /* 1, or 2 for a pair */
    int rt, rt2, rn;
    int64_t offset;
    int mode;
} LdstOp;

/* Direction and extension of a single-register access by its opc field. */
static bool single_width(LdstOp *op, unsigned opc, unsigned size_log2)
{
    switch (opc) {
    case 0:                             /* STR */
    case 1:                             /* LDR, zero-extending */
        op->sign = false;
        op->dst64 = size_log2 == 3;
        break;
    case 2:                             /* LDRS* to X; size 3 is PRFM */
        if (size_log2 == 3) {
            return false;
        }
        op->sign = true;
        op->dst64 = true;
        break;
    default:                            /* LDRS* to W; bytes and halves */
        if (size_log2 >= 2) {
            return false;
        }
        op->sign = true;
        op->dst64 = false;
        break;
    }
    op->load = opc != 0;
    return true;
}

/* LDR/STR (immediate) with a 9-bit offset: size 111 0 00 opc 0 imm9 idx. */
static bool decode_single(uint32_t insn, LdstOp *op)
{
    unsigned size_log2 = extract32(insn, 30, 2);

    if ((insn & 0x3f200000) != 0x38000000 ||
        !single_width(op, extract32(insn, 22, 2), size_log2)) {
        return false;
    }
    op->size = 1 << size_log2;
    op->nregs = 1;
    op->rt = extract32(insn, 0, 5);
    op->rt2 = -1;
    op->rn = extract32(insn, 5, 5);
    op->offset = sextract32(insn, 12, 9);
    op->mode = addr_modes[extract32(insn, 10, 2)];
    return true;
}

/* LDP/STP/LDPSW: opc 101 0 0 mode L imm7 Rt2 Rn Rt. */
static bool decode_pair(uint32_t insn, LdstOp *op)
{
    unsigned opc = extract32(insn, 30, 2);
    bool load = extract32(insn, 22, 1);

    if ((insn & 0x3e000000) != 0x28000000) {
        return false;
    }
    switch (opc) {
    case 0:                             /* 32-bit */
        op->size = 4;
        op->sign = false;
        op->dst64 = false;
        break;
    case 1:                             /* LDPSW; the store form is STGP */
        if (!load) {
            return false;
        }
        op->size = 4;
        op->sign = true;
        op->dst64 = true;
        break;
    case 2:                             /* 64-bit */
        op->size = 8;
        op->sign = false;
        op->dst64 = true;
        break;
    default:
        return false;
    }
    op->load = load;
    op->nregs = 2;
    op->rt = extract32(insn, 0, 5);
    op->rt2 = extract32(insn, 10, 5);
    op->rn = extract32(insn, 5, 5);
    op->offset = (int64_t)sextract32(insn, 15, 7) * op->size;
    op->mode = addr_modes[extract32(insn, 23, 2)];
    return true;
}

static uint64_t data_reg(CPUARMState *env, int r)
{
    return r == 31 ? 0 : env->xregs[r];
}

static void set_data_reg(CPUARMState *env, int r, uint64_t val)
{
    if (r != 31) {
        env->xregs[r] = val;
    }
}

static uint64_t extend(const LdstOp *op, uint64_t val)
{
    if (op->sign) {
        val = sextract64(val, 0, op->size * 8);
    }
    return op->dst64 ? val : (uint32_t)val;
}

/*
 * The abort reports the address of the element that faulted, which for a
 * pair need not be the first. Match it by page offset to find element 0.
 */
static bool first_element_pa(const LdstOp *op, uint64_t va, uint64_t fault_pa,
                             uint64_t *pa)
{
    for (unsigned i = 0; i < op->nregs; i++) {
        if (((va + i * op->size) ^ fault_pa) & 0xfff) {
            continue;
        }
        *pa = fault_pa - i * op->size;
        return true;
    }
    return false;
}

static bool do_access(CPUARMState *env, const LdstOp *op, uint64_t pa,
                      ARMLdstAccessFn access)
{
    const int regs[2] = { op->rt, op->rt2 };
    uint64_t vals[2] = { 0, 0 };

    for (unsigned i = 0; i < op->nregs; i++) {
        if (!op->load) {
            vals[i] = data_reg(env, regs[i]);
        }
        if (!access(pa + i * op->size, &vals[i], op->size, !op->load)) {
            return false;
        }
    }
    if (op->load) {
        for (unsigned i = 0; i < op->nregs; i++) {
            set_data_reg(env, regs[i], extend(op, vals[i]));
        }
    }
    return true;
}

bool arm_emulate_ldst(CPUARMState *env, uint32_t insn, uint64_t fault_pa,
                      ARMLdstAccessFn access)
{
    LdstOp op;
    uint64_t base, va, pa;

    if (!decode_single(insn, &op) && !decode_pair(insn, &op)) {
        return false;
    }
    base = env->xregs[op.rn];           /* xregs[31] holds the current SP */
    va = op.mode == ADDR_POST ? base : base + op.offset;
    if (!first_element_pa(&op, va, fault_pa, &pa) ||
        !do_access(env, &op, pa, access)) {
        return false;
    }
    if (op.mode != ADDR_OFFSET) {
        env->xregs[op.rn] = base + op.offset;
    }
    env->pc += 4;
    return true;
}
