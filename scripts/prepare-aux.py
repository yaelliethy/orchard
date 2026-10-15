#!/usr/bin/env python3
"""Set `boot-args` (or another NVRAM entry) in a tart NVRAM (aux) image.

Why it is this careful:

The aux holds two NVRAM banks, at 0xa00000 and 0xa80000, each 0x80000 long;
the one with the higher generation is live. A bank is a list of CHRP
partitions, each with a 16-byte header (signature, header checksum, length in
16-byte units LE, 12-byte name):

    +0x00     `nvram`   0x20 bytes: adler32 of [+0x14, +0x80000) LE at +0x10,
                        generation LE at +0x14, zero padding
    +0x20     `common`  entries such as the installer's IAS* variables
    +0x60000  `system`  entries such as auto-boot and boot-args

Entries are `KEY=VALUE\\0`; a partition's list ends at the first empty entry.
This edits the entry inside the partition that holds it (or appends it to
`system`), moves no other entry, leaves every partition header alone, and fixes
the bank checksum. On the tart image `boot-args=` is the last entry of
`system`, so setting it only extends that entry.

Serial output is not otherwise reachable on this path — the AVPBooter chain has
no `-append`, because boot-args come from NVRAM.

Copyright (c) 2026 Youssef Elliethy (yaelliethy)
SPDX-License-Identifier: GPL-2.0-or-later
"""

import argparse
import shutil
import sys
import zlib

BANKS = ((0xA00000, 0x80000), (0xA80000, 0x80000))
GEN_OFF = 0x14          # generation counter, LE u32, first word of the payload
CKSUM_OFF = 0x10        # adler32 of [base+0x14, base+banklen), LE u32
PART_HDR = 16           # CHRP partition header


def bank_generation(data, base):
    return int.from_bytes(data[base + GEN_OFF:base + GEN_OFF + 4], "little")


def live_bank(data):
    """The bank with the higher generation is the one iBoot reads."""
    best = None
    for base, length in BANKS:
        if base + length > len(data):
            continue
        gen = bank_generation(data, base)
        if best is None or gen > best[2]:
            best = (base, length, gen)
    return best


def partitions(data, base, length):
    """{name: (data_start, data_end)} for the CHRP partitions of one bank."""
    parts = {}
    off = base
    while off + PART_HDR <= base + length:
        blocks = int.from_bytes(data[off + 2:off + 4], "little")
        if blocks == 0:
            break
        name = bytes(data[off + 4:off + PART_HDR]).rstrip(b"\x00").decode("ascii", "replace")
        parts[name] = (off + PART_HDR, off + blocks * 16)
        off += blocks * 16
    return parts


def entries(data, start, end):
    """[(offset, bytes)] of the entries in one partition, up to the first empty one."""
    out = []
    pos = start
    while pos < end:
        nul = data.find(b"\x00", pos, end)
        if nul < 0 or nul == pos:
            break
        out.append((pos, bytes(data[pos:nul])))
        pos = nul + 1
    return out


def set_entry(data, parts, key, value):
    """Set KEY in whichever partition holds it, else append it to `system`."""
    for name in ("system", "common"):
        if name not in parts:
            continue
        start, end = parts[name]
        ents = entries(data, start, end)
        for i, (pos, raw) in enumerate(ents):
            if raw.split(b"=", 1)[0] != key:
                continue
            if i != len(ents) - 1:
                sys.exit(f"{key.decode()} is not the last entry of '{name}'; editing it "
                         "would move the entries after it, which this does not do")
            break
        else:
            continue
        old_end = pos + len(raw) + 1
        break
    else:
        if "system" not in parts:
            sys.exit("no 'system' partition in the live bank")
        name = "system"
        start, end = parts[name]
        ents = entries(data, start, end)
        pos = old_end = (ents[-1][0] + len(ents[-1][1]) + 1) if ents else start

    blob = key + b"=" + value + b"\x00"
    # Keep at least one zero byte after the list: it is the terminator.
    if pos + len(blob) >= end:
        sys.exit(f"no room left in '{name}' for this entry")
    data[pos:max(old_end, pos + len(blob))] = blob.ljust(old_end - pos, b"\x00")
    return name


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("aux", help="tart NVRAM image (modified in place unless --out)")
    ap.add_argument("--out", help="write here instead of patching in place")
    ap.add_argument("--set", action="append", default=[],
                    metavar="KEY=VALUE",
                    help="entry to set, e.g. --set 'boot-args=-v serial=3'")
    ap.add_argument("--show", action="store_true",
                    help="print the live bank's entries and exit")
    args = ap.parse_args()

    data = bytearray(open(args.aux, "rb").read())
    found = live_bank(data)
    if not found:
        sys.exit("no NVRAM bank found: is this a tart nvram image?")
    base, length, gen = found
    print(f"live bank at {base:#x}, length {length:#x}, generation {gen}")
    parts = partitions(data, base, length)
    if not {"common", "system"} <= parts.keys():
        sys.exit(f"unexpected partitions in the live bank: {sorted(parts)}")

    if args.show:
        for name in ("common", "system"):
            print(f"[{name}]")
            for _, raw in entries(data, *parts[name]):
                print("  " + raw.decode("utf-8", "replace"))
        return 0

    if not args.set:
        sys.exit("nothing to do: pass --set KEY=VALUE (or --show)")

    for item in args.set:
        if "=" not in item:
            sys.exit(f"--set needs KEY=VALUE, got {item!r}")
        key, value = item.encode().split(b"=", 1)
        name = set_entry(data, parts, key, value)
        print(f"set {item!r} in '{name}'")

    # Fix only this bank's checksum. The other bank is left untouched, and so
    # is the generation: iBoot re-reads the list, it does not diff it.
    checked = bytes(data[base + GEN_OFF:base + length])
    data[base + CKSUM_OFF:base + CKSUM_OFF + 4] = \
        (zlib.adler32(checked) & 0xFFFFFFFF).to_bytes(4, "little")

    out = args.out or args.aux
    if args.out:
        shutil.copyfile(args.aux, args.out)
    open(out, "wb").write(bytes(data))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
