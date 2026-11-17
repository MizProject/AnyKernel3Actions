# AnyKernel3Actions

Modular, zero-dependency Python GitHub Actions component for packaging compiled Linux/Android kernels using [`osm0sis/AnyKernel3`](https://github.com/osm0sis/AnyKernel3) as base.

## Features

- **Pure Python 3 standard library** (`main.py` / `ak3_actions/`) — no external Python package installation required on runners.
- **Clean AnyKernel3 base preparation (`repo.py`)** — shallow-clones `https://github.com/osm0sis/AnyKernel3.git` with `--no-tags` (or copies a local `anykernel-path`), deletes all `.git` tags, `.git`, `.github`, `.git*` metadata, and optionally `README.md`, while preserving `LICENSE` and validating AnyKernel3 structure.
- **Compiled kernel processing (`kernel.py`)** — discovers compiled kernel images in priority order (`Image.gz-dtb`, `Image.lz4-dtb`, ..., `Image.gz`, `zImage`, `Image`), verifies binary header magic and embedded Linux strings (`inspect_kernel_binary`), extracts Linux kernel version from `kernel.release` / `utsrelease.h` or compressed/raw binaries (`raw`, `gzip`, `xz`/`lzma`, `bz2`, pure-Python `lz4`), concatenates `*.dtb` trees or stages `dtb`/`dtbo`/`dt`/`recovery_dtbo`/`system_dlkm`/`vendor_dlkm`, stages `.ko` modules (with optional stripping and auto-enabling `do.modules=1`), and copies `ramdisk/`, `vendor_ramdisk/`, `patch/`, `vendor_patch/`, `banner`, and `version`.
- **[Beta] Autodetect & artifact scour (`discover.py`)** — opt-in component (`autodetect: true` or [components/discover/action.yaml](components/discover/action.yaml)) that checks the current directory, indexes the workspace for a Linux kernel source tree (`Makefile` + `arch/`), inspects `out/` and `arch/<arch>/boot/`, and scours downloaded GitHub Actions artifacts (`artifacts/`, `artifacts-dir`, `$RUNNER_TEMP`) while rejecting fake, text, ELF, or corrupt files.
- **Easy `anykernel.sh` configuration (`config.py`)** — configures `properties()` (`kernel.string`, `do.devicecheck`, `do.modules`, `do.systemless`, `do.cleanup`, `do.cleanuponabort`, dynamic `device.name1..N`, `supported.versions`, `supported.patchlevels`, `supported.vendorpatchlevels`, plus custom keys), primary boot shell variables (`BLOCK`, `IS_SLOT_DEVICE`, `RAMDISK_COMPRESSION`, `PATCH_VBMETA_FLAG`, `CUSTOMDD`, `SLOT_SELECT`, `NO_BLOCK_DISPLAY`, `NO_MAGISK_CHECK`, `NO_VBMETA_PARTITION_PATCH`), `dump_write` vs `split_flash` install flow (automatically stripping Galaxy Nexus `tuna` sample edits), optional `@Chainfire` SCSI `deepsleep-fix`, extra partition blocks (`init_boot`, `vendor_kernel_boot`, `vendor_boot`), and POSIX syntax validation (`sh -n`).
- **Flashable zip packaging (`packager.py`)** — produces clean flashable `.zip` archives excluding `.git`, `.github`, `README.md`, and `*placeholder` files, preserves Unix executable permissions (`0755`) on `update-binary` and `tools/*`, computes SHA-256/MD5 checksums, and writes a GitHub Step Summary.

## Quick Start

```yaml
- name: Package AnyKernel3 Flashable Zip
  id: ak3
  uses: MizProject/AnyKernel3Actions@main
  with:
    kernel-dir: out/arch/arm64/boot
    dtb-path: out/arch/arm64/boot/dts/qcom
    dtbo-path: out/arch/arm64/boot/dtbo.img
    modules-path: out/modules_install
    modules-dest: vendor/lib/modules
    kernel-string: "MyKernel {kernel_version} ({short_sha})"
    do-devicecheck: "1"
    device-names: "sweet,sweetin"
    supported-versions: "13 - 15"
    # Required: full boot block partition path (without _a/_b slot suffix)
    block: "/dev/block/bootdevice/by-name/boot"
    # A/B slots: "1" for boot_a/boot_b, "0" for non-A/B single boot, "auto" to detect
    is-slot-device: "1"
    zip-name: "MyKernel-{device}-{kernel_version}-{date}.zip"
    output-dir: dist
    write-checksum-file: "true"
    strict: "true"
```

See [GUIDE.md](GUIDE.md) for a step-by-step walkthrough categorized into **Quick**, **Intermediate**, and **Precisely Advanced** workflows, and [examples/workflow-example.yml](examples/workflow-example.yml) for a full caller example.

### Boot Partition (`block`) and A/B Slot (`is-slot-device`) Setup

- **`block` (required)**: Always point `block` (here or via `config-file` / `anykernel-script`) to your device's full boot partition path rather than a bare `boot` shorthand or leaving upstream's Galaxy Nexus `omap_hsmmc.0` path:
  - Qualcomm: `/dev/block/bootdevice/by-name/boot` (or `/dev/block/platform/soc/<addr>/by-name/boot`)
  - Generic / Samsung / MediaTek / Exynos: `/dev/block/by-name/boot` or `/dev/block/platform/<controller>/by-name/boot`
  - Broad non-device-specific zips only: `auto`
  - Do **not** append `_a` or `_b` to `block` when using `is-slot-device: "1"` or `"auto"`; AnyKernel3 appends the active slot suffix automatically.
- **`is-slot-device`**: Controls whether AnyKernel3 resolves `boot_a` / `boot_b`:
  - `"1"` — A/B slot devices (`boot_a` and `boot_b`). AnyKernel3 appends the active `_a`/`_b` slot suffix to `BLOCK`.
  - `"0"` — Non-A/B single-slot devices (`boot` only).
  - `"auto"` — Automatically detects whether the target device uses `boot_a`/`boot_b` slots or a single `boot` partition.

### Optional `anykernel.sh` Extras (`deepsleep-fix`)

For devices with the SCSI `write_protect` / `cache_type` deep-sleep quirk, set `deepsleep-fix: "true"` (in `action.yaml`, `components/configure`, `config-file`, or `--deepsleep-fix true` via CLI) to run `@Chainfire`'s SCSI cache fix immediately after unpacking/splitting the boot image (`dump_boot;` / `split_boot;`) and before remaining tweaks and `write_boot;` / `flash_boot;`:

```sh
# Deepsleep fix (@Chainfire)
for i in `ls /sys/class/scsi_disk/ 2>/dev/null`; do
	cat /sys/class/scsi_disk/$i/write_protect 2>/dev/null | grep 1 >/dev/null
	if [ $? -eq 0 ]; then
		echo 'temporary none' > /sys/class/scsi_disk/$i/cache_type
	fi
done;
```

## Modular Components

This repository provides both an all-in-one root [action.yaml](action.yaml) and five modular sub-actions under `components/`:

| Component | Sub-action | Python Module | Purpose |
| --- | --- | --- | --- |
| Base Preparer | [components/prepare/action.yaml](components/prepare/action.yaml) | [ak3_actions/repo.py](ak3_actions/repo.py) | Clone/copy `osm0sis/AnyKernel3`, delete `.git` tags & metadata |
| Kernel Autodetect **(Beta)** | [components/discover/action.yaml](components/discover/action.yaml) | [ak3_actions/discover.py](ak3_actions/discover.py) | Scour cwd, kernel source `out/`/`arch/`, and artifacts with string verification |
| Kernel Processor | [components/process-kernel/action.yaml](components/process-kernel/action.yaml) | [ak3_actions/kernel.py](ak3_actions/kernel.py) | Stage kernel image, DTB/DTBO/DLKM, modules, ramdisk, banner/version |
| Script Configurator | [components/configure/action.yaml](components/configure/action.yaml) | [ak3_actions/config.py](ak3_actions/config.py) | Configure & validate `anykernel.sh` properties and variables |
| Zip Packager | [components/package/action.yaml](components/package/action.yaml) | [ak3_actions/packager.py](ak3_actions/packager.py) | Create flashable zip, checksums, and step summary |

### [Beta] Autodetect & Artifacts Scour

When `kernel-image` and `kernel-dir` are omitted, set `autodetect: "true"` (or call `components/discover`) to enable the **[Beta]** scour mechanism:

1. Checks the current directory (`cwd`), `cwd/out`, and `cwd/arch/*/boot`.
2. Indexes the workspace (`$GITHUB_WORKSPACE`) for Linux kernel source roots (`Makefile` + `arch/`), then checks `out/` (`<ksrc>/out`, `<workspace>/out`) and falls back to `<ksrc>/arch/*/boot` if `out/` is absent.
3. Checks `artifacts-dir`, common artifact folders (`artifacts/`, `artifact/`, `dist/`, `output/`), `$RUNNER_TEMP`, and workspace subdirectories (excluding `.git` and AnyKernel staging directories).
4. Confirms each candidate is a genuine kernel image by checking binary header magic (`ARM64_IMAGE`, `ARM_ZIMAGE`, `RISCV_IMAGE`, `X86_BZIMAGE`, `FIT_IMAGE`) and decompressing payloads to verify embedded `Linux version ...` and `.rodata` strings—never accepting filenames blindly.
5. Falls back with detailed rejection/search diagnostics telling you to specify `kernel-image` or `kernel-dir` manually if nothing verified is found.

### Using Individual Sub-actions

```yaml
- id: prep
  uses: MizProject/AnyKernel3Actions/components/prepare@main

- id: kstage
  uses: MizProject/AnyKernel3Actions/components/process-kernel@main
  with:
    staging-dir: ${{ steps.prep.outputs.staging-dir }}
    kernel-dir: out/arch/arm64/boot

- id: cfg
  uses: MizProject/AnyKernel3Actions/components/configure@main
  with:
    staging-dir: ${{ steps.prep.outputs.staging-dir }}
    config-file: .github/anykernel.json

- id: pkg
  uses: MizProject/AnyKernel3Actions/components/package@main
  with:
    staging-dir: ${{ steps.prep.outputs.staging-dir }}
    output-dir: dist
```

## Config File Support

Pass `config-file` pointing to a `.json`, `.toml` (Python 3.11+), or `.properties` file. Action `with:` inputs override config file entries. Examples:
- [examples/anykernel.json](examples/anykernel.json)
- [examples/anykernel.properties](examples/anykernel.properties)

## Template Variables

Supported in `kernel-string`, `zip-name`, `banner`, `version`, `extra-properties`, `boot-attributes`, and `ramdisk-patch-commands`:

- `{kernel_version}` — detected Linux release string (e.g. `5.10.198-perf-g123abc`)
- `{kernel_image}` — staged kernel filename (e.g. `Image.gz-dtb`)
- `{kernel_string}` — resolved `kernel.string` (for `banner` / `version` / `zip-name`)
- `{device}` — first entry in `device-names`
- `{devices}` — hyphen-joined `device-names`
- `{date}` — UTC `YYYYMMDD`
- `{datetime}` — UTC `YYYYMMDD-HHMMSS`
- `{sha}` / `{short_sha}` — commit SHA from `GITHUB_SHA` or workspace `git rev-parse`
- `{ak_base_version}` — `AK_BASE_VERSION` from AnyKernel3 `updater-script`

## Local CLI Usage

```bash
python3 main.py build \
  --kernel-dir out/arch/arm64/boot \
  --kernel-string "LocalBuild {kernel_version}" \
  --device-names "sweet,sweetin" \
  --block /dev/block/bootdevice/by-name/boot \
  --is-slot-device auto \
  --output-dir dist
```

Individual subcommands are also available (`prepare`, `discover`, `process-kernel`, `configure`, `package`, `build`).

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## LLM Guide

See [llm.txt](llm.txt) for a structured reference for LLMs on utilizing features, repository architecture, conventions, and maintaining component/option inventories.

