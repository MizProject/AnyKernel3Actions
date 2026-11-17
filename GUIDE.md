# AnyKernel3Actions Step-by-Step Usage Guide

This guide walks through using `AnyKernel3Actions` step by step across three tiers:

- **[Quick](#tier-1-quick--flashable-zip-in-minutes)** — minimum setup to package a compiled kernel into a clean AnyKernel3 flashable zip.
- **[Intermediate](#tier-2-intermediate--dtbs-modules-constraints--config-files)** — DTBs/DTBOs, kernel modules, device/Android version bounds, dynamic banners, external config files, and `@Chainfire` `deepsleep-fix`.
- **[Precisely Advanced](#tier-3-precisely-advanced--gki-multi-partition-custom-ramdisk-sub-actions--beta-autodetect)** — GKI `init_boot` `split_flash` mode, multi-partition `extra-partitions`, custom `anykernel.sh` hooks, AVB/slot flags, modular `components/*` composition, and `[Beta]` kernel autodetect with binary verification.

---

## Prerequisites

1. **GitHub Actions runner** (e.g., `ubuntu-latest`) or local environment with **Python 3.9+** and **`git`** installed (zero external `pip` packages are needed).
2. **Compiled kernel outputs** (such as `Image.gz-dtb`, `Image.lz4-dtb`, `Image`, `zImage`, plus optional `dtb`/`dtbo.img`/`.ko` modules).
3. **Target device partition details** (full `/dev/block/.../by-name/boot` path and whether the device uses A/B `boot_a`/`boot_b` slots).

---

## Tier 1: Quick — Flashable Zip in Minutes

Use the all-in-one root [action.yaml](action.yaml) when your build job already outputs a kernel image and you want a clean flashable zip with minimal configuration.

### Step 1: Confirm Your Kernel Output and Boot Partition Path

1. Locate your compiled kernel output path (for example `out/arch/arm64/boot/Image.gz-dtb` or `out/arch/arm64/boot`).

> **Disclaimer — Non-GKI / In-Tree Kernels Without `out/` (e.g., Galaxy A12s Stock Kernel):**  
> Some older or non-GKI vendor kernels (such as the **Samsung Galaxy A12s** stock kernel) fail or do not support out-of-tree `O=out` builds and compile in-tree instead. If your kernel does not compile under `out/`, point `kernel-dir` directly to `arch/<cpu>/boot` (for example `arch/arm64/boot` or `arch/arm/boot`), or pass `kernel-image: arch/<cpu>/boot/<ImageName>` directly.

2. Find your device's **full boot block partition path** (without `_a` or `_b`):
   - Qualcomm devices usually use `/dev/block/bootdevice/by-name/boot` or `/dev/block/platform/soc/<addr>/by-name/boot`.
   - Generic / Samsung / MediaTek / Exynos devices commonly use `/dev/block/by-name/boot` or `/dev/block/platform/<controller>/by-name/boot`.
   - **Never** leave upstream's Galaxy Nexus `omap_hsmmc.0` placeholder unset, and avoid bare `boot` for target device builds.

### Step 2: Determine Your Slot Layout (`is-slot-device`)

- Set `is-slot-device: "1"` if your device uses A/B slots (`boot_a` and `boot_b`). Keep `block` without `_a`/`_b`.
- Set `is-slot-device: "0"` if your device uses a single non-A/B `boot` partition.
- Set `is-slot-device: "auto"` if you want AnyKernel3 to detect `boot_a`/`boot_b` vs single `boot` at flash time.

### Step 3: Add `AnyKernel3Actions` to Your Workflow

Add these steps after your kernel compile step in `.github/workflows/*.yml`:

```yaml
- name: Package AnyKernel3 Zip
  id: ak3
  uses: MizProject/AnyKernel3Actions@main
  with:
    kernel-dir: out/arch/arm64/boot
    kernel-string: "MyKernel {kernel_version}"
    do-devicecheck: "1"
    device-names: "a23"
    block: "/dev/block/bootdevice/by-name/boot"
    is-slot-device: "0"
    zip-name: "MyKernel-{device}-{kernel_version}-{date}.zip"
    output-dir: dist
    strict: "true"

- name: Upload Flashable Zip
  uses: actions/upload-artifact@v4
  with:
    name: ${{ steps.ak3.outputs.zip-name }}
    path: ${{ steps.ak3.outputs.zip-path }}
```

### Step 4: Verify What Happened

During that single action call:
1. `osm0sis/AnyKernel3` (`master`) is shallow-cloned with `--no-tags`, and `.git`, git tags, `.github`, `.git*`, and `README.md` are stripped while keeping `LICENSE`.
2. The highest-priority compiled kernel inside `out/arch/arm64/boot` is copied to the zip root, and `{kernel_version}` is auto-detected from `kernel.release` / `utsrelease.h` or directly from the kernel image binary.
3. Upstream's Galaxy Nexus `tuna` example edits (`init.tuna.rc`, `fstab.tuna`) and unused `device.name*` entries are removed, leaving a clean `anykernel.sh` validated by `sh -n`.
4. `dist/<zip-name>` is created with Unix `0755` permissions preserved on `update-binary` and `tools/*`, and a build table is written to the GitHub Job Summary.

> **Local Quick Test:** You can run the exact same pipeline locally before pushing to GitHub:
> ```bash
> python3 main.py build \
>   --kernel-dir out/arch/arm64/boot \
>   --kernel-string "MyKernel {kernel_version}" \
>   --device-names "a23" \
>   --block /dev/block/bootdevice/by-name/boot \
>   --is-slot-device 0 \
>   --output-dir dist \
>   --strict true
> ```

---

## Tier 2: Intermediate — DTBs, Modules, Constraints & Config Files

Move to Intermediate when your kernel ships separate device trees, `.ko` modules, Android version/security-patch guards, custom banners, or shared configuration files across branches.

### Step 1: Stage Separate DTB, DTBO, and DLKM Images

If your kernel image does **not** already embed the DTB (`Image` or `Image.gz` instead of `*-dtb`), or if your device flashes `dtbo` / `dlkm` partitions:

- **`dtb-path`**: Pass a single `dtb`/`dtb.img` file, or point to a directory/glob of `*.dtb` files (sorted and concatenated into a single `dtb` file with FDT `0xd00dfeed` checks).
- **`dtbo-path`**: Pass a prebuilt `dtbo.img` / `dtbo` file (raw `.dtbo` directories must be packed with `mkdtboimg` first).
- **`dt-path`**, **`recovery-dtbo-path`**, **`system-dlkm-path`**, **`vendor-dlkm-path`**: Pass file or containing directory paths when applicable.

```yaml
with:
  kernel-image: out/arch/arm64/boot/Image.gz
  dtb-path: out/arch/arm64/boot/dts/vendor
  dtbo-path: out/arch/arm64/boot/dtbo.img
```

### Step 2: Package Compiled `.ko` Kernel Modules

Point `modules-path` to either a `make modules_install` root (`lib/modules/<ver>/...`), an AnyKernel-structured directory (`vendor/` or `system/`), or a directory/glob of `.ko` files:

```yaml
with:
  modules-path: out/modules_install
  modules-dest: vendor/lib/modules
  preserve-module-tree: "false"
  include-module-metadata: "true"   # copies modules.load, modules.dep, modules.alias, etc.
  strip-modules: "true"             # runs llvm-strip/strip --strip-debug when available
  do-systemless: "1"                # 1 = Magisk/KernelSU ak3-helper module; 0 = direct partition push
```

- When `.ko` files are staged and `do-modules` is omitted, `do.modules=1` is enabled automatically. Set `do-modules: "0"` explicitly to override.

### Step 3: Restrict Target Devices, Android Versions, and Security Patch Levels

Guard users from flashing onto incompatible ROMs or wrong devices:

```yaml
with:
  do-devicecheck: "1"
  device-names: "sweet,sweetin"
  supported-versions: "13 - 15"              # list ("13, 14") or range ("13 - 15")
  supported-patchlevels: "2024-01 -"         # YYYY-MM closed or open range
  supported-vendorpatchlevels: "2024-01 - 2025-12"
```

### Step 4: Customize Flash-Time Display (`banner`, `version`) and Templates

Use `banner` / `banner-file` and `version` / `version-file` to display build branding during recovery flash. All template strings support:
`{kernel_version}`, `{kernel_image}`, `{kernel_string}`, `{device}`, `{devices}`, `{date}`, `{datetime}`, `{sha}`, `{short_sha}`, and `{ak_base_version}`.

```yaml
with:
  kernel-string: "Stellar {kernel_version} ({short_sha})"
  banner: |
    ==============================
    {kernel_string}
    Target: {devices}
    ==============================
  version: "{kernel_version} - {date}"
  write-checksum-file: "true"   # writes <zip-name>.sha256 beside the zip
```

### Step 5: Externalize Settings into a Config File (`config-file`)

To share one configuration across workflows or matrices without duplicating long YAML blocks, store settings in `.json`, `.properties`, or `.toml` (Python 3.11+):

1. Create `.github/anykernel.json` (see [examples/anykernel.json](examples/anykernel.json) or [examples/anykernel.properties](examples/anykernel.properties)):
   ```json
   {
     "kernel.string": "Stellar {kernel_version} ({short_sha})",
     "do.devicecheck": 1,
     "device.names": ["sweet", "sweetin"],
     "supported.versions": "13 - 15",
     "block": "/dev/block/bootdevice/by-name/boot",
     "is-slot-device": "1",
     "deepsleep-fix": true
   }
   ```
2. Reference it and override only per-build fields in YAML:
   ```yaml
   with:
     config-file: .github/anykernel.json
     device-names: ${{ matrix.device }}   # overrides device.names from config-file
   ```
   Precedence is always: `CLI flags > action inputs (INPUT_*) > config-file > upstream anykernel.sh`.

### Step 6: Enable Device Quirk Fixes (`deepsleep-fix`)

For devices suffering from the SCSI `write_protect` / `cache_type` deep-sleep bug, enable `deepsleep-fix: "true"`:

```yaml
with:
  deepsleep-fix: "true"
```

This injects `@Chainfire`'s SCSI loop directly after boot extraction (`dump_boot;` / `split_boot;`) and before remaining tweaks and `write_boot;` / `flash_boot;`:

```sh
# Deepsleep fix (@Chainfire)
for i in `ls /sys/class/scsi_disk/ 2>/dev/null`; do
	cat /sys/class/scsi_disk/$i/write_protect 2>/dev/null | grep 1 >/dev/null
	if [ $? -eq 0 ]; then
		echo 'temporary none' > /sys/class/scsi_disk/$i/cache_type
	fi
done;
```

---

## Tier 3: Precisely Advanced — GKI, Multi-Partition, Custom Ramdisk, Sub-Actions & Beta Autodetect

Use these options for modern GKI `init_boot` layouts, ramdisk patching, multi-partition flashes, custom recovery behaviors, modular pipelines, or automatic artifact scouring.

### Step 1: Configure GKI / `init_boot` Devices (`install-mode: split_flash`)

On devices with `init_boot` (Android 13+ GKI 2.0), the `boot` partition has **no ramdisk**, so AnyKernel3's default `dump_boot;` aborts with `"No ramdisk found to unpack"`. Switch `install-mode` to `"split_flash"`:

```yaml
with:
  kernel-image: out/arch/arm64/boot/Image.lz4
  dtbo-path: out/arch/arm64/boot/dtbo.img
  vendor-dlkm-path: out/arch/arm64/boot/vendor_dlkm.img
  block: "/dev/block/bootdevice/by-name/boot"
  is-slot-device: "1"
  install-mode: "split_flash"
  # Optional explicit list; vendor_dlkm, system_dlkm, and dtbo are also added automatically if staged
  flash-generic: "vendor_dlkm,dtbo"
```

This renders `split_boot;` -> optional `deepsleep-fix` / `ramdisk-patch-commands` -> `flash_boot;` -> `flash_generic vendor_dlkm;` -> `flash_generic dtbo;`.

### Step 2: Patch Ramdisks and Inject Custom `anykernel.sh` Commands

Choose one of two approaches:

- **Option A — Overlay files + inline snippets on upstream `anykernel.sh`**:
  ```yaml
  with:
    ramdisk-dir: anykernel/ramdisk
    patch-dir: anykernel/patch
    boot-attributes: |
      set_perm_recursive 0 0 755 644 $RAMDISK/*;
      set_perm 0 0 750 $RAMDISK/overlay.d/init.custom.rc;
    ramdisk-patch-commands: |
      patch_cmdline "androidboot.selinux" "androidboot.selinux=permissive";
      insert_line init.rc "init.custom.rc" after "import /init.environ.rc" "import /init.custom.rc";
  ```
- **Option B — Bring your own `anykernel-script`**:
  Pass `anykernel-script: packaging/anykernel.sh`. `install-mode` automatically defaults to `"keep"` and `strip-example-patches` defaults to `"false"`, preserving your custom install script while still updating `properties()`, `BLOCK`/`IS_SLOT_DEVICE`, `deepsleep-fix` (injected right after `dump_boot;`/`split_boot;`), and `ramdisk-patch-commands`.

### Step 3: Flash Multiple Partitions (`extra-partitions`)

To append secondary `reset_ak` partition blocks (such as `vendor_kernel_boot`, `vendor_boot`, or `init_boot`) while keeping the primary `boot` section isolated:

```yaml
with:
  block: "/dev/block/bootdevice/by-name/boot"
  is-slot-device: "1"
  install-mode: "split_flash"
  vendor-ramdisk-dir: anykernel/vendor_ramdisk
  extra-partitions: |
    [
      {
        "name": "vendor_kernel_boot",
        "block": "/dev/block/by-name/vendor_kernel_boot",
        "is-slot-device": "1",
        "ramdisk-compression": "lz4-l",
        "install-mode": "split_flash"
      },
      {
        "name": "init_boot",
        "block": "/dev/block/by-name/init_boot",
        "is-slot-device": "1",
        "install-mode": "dump_write",
        "keep_on_reset": false,
        "commands": "ui_print \"- Patched init_boot\";"
      }
    ]
```

### Step 4: Fine-Tune Compression, AVB, Slot Selection & Output Flags

| Input | Purpose | Notes |
| --- | --- | --- |
| `ramdisk-compression` | Override ramdisk repack codec | `auto`, `gz`, `lzo`, `lzma`, `xz`, `bz2`, `lz4`, `lz4-l`, `cpio`, `none` |
| `patch-vbmeta-flag` | AVBv2 vbmeta flag handling | `auto`, `0`, or `1` |
| `slot-select` | Target A/B slot | `active` or `inactive` |
| `custom-dd` | Force custom `dd` parameters | Emits `CUSTOMDD="...";` |
| `no-block-display` | Hide detected block path output | Emits `NO_BLOCK_DISPLAY=1;` when true; omitted when false |
| `no-magisk-check` | Skip Magisk kernel/dtb retention | Emits `NO_MAGISK_CHECK=1;` when true; omitted when false |
| `no-vbmeta-partition-patch` | Skip `httools` vbmeta patching on GKI | Emits `NO_VBMETA_PARTITION_PATCH=1;` when true; omitted when false |
| `extra-properties` | Custom lines inside `properties()` | Multiline `key=value` text; single quotes `'` are rejected |
| `do-cleanup` / `do-cleanuponabort` | Keep `/tmp/anykernel` in recovery | Set `"0"` temporarily when debugging recovery flashes |
| `compression-level` | Zip deflate level | `"0"` (store) to `"9"` (default) |

### Step 5: Compose Modular Sub-Actions or Pause Before Zipping

When your workflow needs custom steps between base preparation, kernel staging, script generation, and zipping:

- **Option 1 (`create-zip: "false"`)**: Run root `action.yaml` with `create-zip: "false"`, run custom scripts inside `${{ steps.ak3.outputs.staging-dir }}`, then call [components/package/action.yaml](components/package/action.yaml).
- **Option 2 (Sub-action pipeline)**:
  ```yaml
  - id: prep
    uses: MizProject/AnyKernel3Actions/components/prepare@main

  - id: kstage
    uses: MizProject/AnyKernel3Actions/components/process-kernel@main
    with:
      staging-dir: ${{ steps.prep.outputs.staging-dir }}
      kernel-dir: out/arch/arm64/boot
      verify-kernel: "true"

  # ... insert custom file checks, signing, or tools/ binaries in staging-dir ...

  - id: cfg
    uses: MizProject/AnyKernel3Actions/components/configure@main
    with:
      staging-dir: ${{ steps.prep.outputs.staging-dir }}
      block: "/dev/block/bootdevice/by-name/boot"
      is-slot-device: "1"
      deepsleep-fix: "true"

  - id: pkg
    uses: MizProject/AnyKernel3Actions/components/package@main
    with:
      staging-dir: ${{ steps.prep.outputs.staging-dir }}
      output-dir: dist
  ```

### Step 6: `[Beta]` Kernel Autodetect & Downloaded Artifacts Scour

When packaging in a downstream job (for instance after `actions/download-artifact`) without hardcoding build output paths, use `autodetect: "true"`, `artifacts-dir`, or [components/discover/action.yaml](components/discover/action.yaml):

```yaml
- uses: actions/download-artifact@v4
  with:
    path: downloaded-artifacts

- id: ak3
  uses: MizProject/AnyKernel3Actions@main
  with:
    autodetect: "true"
    artifacts-dir: downloaded-artifacts
    search-arch: "arm64"
    autodetect-extras: "true"
    kernel-string: "CIKernel {kernel_version}"
    do-devicecheck: "0"
    block: "/dev/block/bootdevice/by-name/boot"
    is-slot-device: "auto"
    strict: "true"
```

**How `[Beta]` Autodetect scours and verifies candidates:**
1. Searches `cwd` (`cwd`, `cwd/out`, `cwd/arch/*/boot`).
2. Indexes `$GITHUB_WORKSPACE` for Linux kernel source trees (`Makefile` + `arch/` + `kernel/`|`drivers/`|`Kconfig`), searches `<ksrc>/out` and `<workspace>/out`, then falls back to `<ksrc>/arch/*/boot` if `out/` is missing (covering older/non-GKI in-tree builds such as Galaxy A12s stock kernels that do not compile under `out/`).
3. Searches `artifacts-dir`, common artifact dirs (`artifacts/`, `artifact/`, `.artifacts/`, `downloads/`, `downloaded-artifacts/`, `dist/`, `output/`, `build/`), `$RUNNER_TEMP`, and workspace subdirectories (skipping `.git` and AnyKernel staging dirs).
4. **Never trusts filenames blindly**: every candidate is checked with `inspect_kernel_binary` (verifying `ARM64_IMAGE`, `ARM_ZIMAGE`, `RISCV_IMAGE`, `X86_BZIMAGE`, or `FIT_IMAGE` header magic and decompressing `gzip`, `xz`/`lzma`, `bz2`, pure-Python `lz4`, or `lzo` payloads to confirm `Linux version ...` and kernel `.rodata` strings). Text files, userland ELFs, PNGs, and zeroed headers are rejected.
5. Check outputs `discovered-from` and `kernel-confidence`, or set `verify-kernel: "true"` to enforce the same binary checks on manual `kernel-image` / `kernel-dir` inputs.

### Step 7: Use Custom AnyKernel3 Forks, Commits, or Air-Gapped Templates

```yaml
with:
  anykernel-repo: "https://github.com/your-org/AnyKernel3.git"
  anykernel-ref: "41f04bdb4ee30a44e429c2c1131d5e8bb32b15f1"  # branch, tag, or commit SHA
  # Or pass a pre-checked-out local folder instead of cloning:
  # anykernel-path: vendor/AnyKernel3
  clean-staging: "true"
  strip-readme: "true"
```

---

## Troubleshooting Validation Errors

| Error / Warning | Cause | Fix |
| --- | --- | --- |
| `BLOCK is still set to the upstream Galaxy Nexus placeholder` | `block` was omitted on upstream `anykernel.sh` | Set `block` to your device's `/dev/block/.../by-name/boot` path |
| `BLOCK='boot' is a short partition name` (`strict: true`) | Used bare `"boot"` instead of full path | Set full `/dev/block/bootdevice/by-name/boot` (or `auto` for generic zips) |
| `BLOCK=... includes slot suffix while IS_SLOT_DEVICE=1` | Passed `boot_a`/`boot_b` together with `is-slot-device` `1`/`auto` | Remove `_a`/`_b` from `block`; AnyKernel3 appends the active slot automatically |
| `No ramdisk found to unpack. Aborting...` on device | Device uses `init_boot` and `boot` has no ramdisk | Set `install-mode: "split_flash"` |
| `Property ... contains a single quote (')` | `kernel.string` or property contains `'` | Remove or rephrase `'` (breaks `properties() { ' ... '; }`) |
| `[Beta] Autodetect could not locate a verified compiled kernel image` | No verified kernel binary in `cwd`, `out/`, `arch/`, or `artifacts/` | Pass `kernel-image` or `kernel-dir` manually (check rejection logs for details) |

