# scripts

| Script | What it does |
|---|---|
| `build.sh` | Configures and builds QEMU; the Rust GPU device builds with it. |
| `fetch-tart-image.py` | Pulls the macOS disk, the NVRAM (aux) and the VM config from a tart OCI image. Resumable; prints the ECID. |
| `extract-avpbooter.py` | Takes Apple's VM firmware out of the macOS image (it ships inside `Virtualization.framework`), so no Mac is needed. Needs `apfs-fuse`. |
| `patch-avpbooter.py` | Turns that firmware into one that runs here. Checks the bytes before writing. |
| `prepare-aux.py` | Sets a variable (e.g. `boot-args`) in the live NVRAM bank, inside the partition that holds it, and fixes the bank checksum. `--show` lists what is there. |
| `run-vm.sh` | Boots the guest in the foreground. Every flag is commented with the measurement behind it. |
| `boot-robust.sh` | Boots in the background, resets nothing, and refuses to start a second VM against the same images. Once macOS is up it sets the guest clock and retires Cirrus's tart-guest-agent through `guest.py`. |
| `guest.py` | Talks to macOS's own guest agent over `qga.sock`: `wait`, `exec`, `settime`, `prepare`, `shutdown`. No ssh or password needed. |

## Order for a fresh machine

```sh
scripts/build.sh
scripts/fetch-tart-image.py --out-dir images
scripts/extract-avpbooter.py images/disk.raw -o images/AVPBooter.vmapple2.bin
scripts/patch-avpbooter.py images/AVPBooter.vmapple2.bin images/AVPBooter.patched.bin
scripts/prepare-aux.py images/aux.img --set 'boot-args=-v serial=3'   # optional
scripts/boot-robust.sh
```
