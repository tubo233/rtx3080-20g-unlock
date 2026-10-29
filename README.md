# RTX 3080 20GB 算力解锁 · 非 X570 主板实战记录

> A field report: unlocking the FP32-accumulate tensor throughput of a **modded RTX 3080 20 GB**
> on an **ASUS TUF B450M-PRO GAMING (BIOS 4645, AMI)** — a board that appears in no verified list.
>
> 本仓库包含一台特定机器的完整实测记录、各失败模式的取证，以及一个直接调用系统已有 cuBLAS 的测量工具。

---

## TL;DR（英文摘要）

A stock consumer RTX 30 series card runs its **FP32-accumulate tensor instructions at half rate**.
That limit lives in the GPU's speed-selector registers (`SS0`/`SS1`), which are write-protected by
the firmware's privilege-level mask. [`nv-uefi-unlocker`](https://github.com/physicsdolphin/nv-uefi-unlocker)
runs a controlled firmware transaction in the UEFI DXE window — before the Windows driver binds — to
open the mask, program the selectors, and latch them through a PCIe Function Level Reset.

On this machine the whole chain works, and the unlock **survives the Windows driver's initialization**.
Measured on an RTX 3080 20 GB (GA102, `10de:2206`) with driver 621.01:

| precision (FP32 accumulate) | locked | unlocked | ratio |
|---|---:|---:|---:|
| BF16 | 50.9 TFLOPS | **97.4 TFLOPS** | **1.91×** |
| TF32 | 25.1 TFLOPS | **49.6 TFLOPS** | **1.97×** |
| FP16 | 58.2 TFLOPS | **98.6 TFLOPS** | 1.69× |
| **llama.cpp 27B IQ3_S `pp512`** | **1279.0 t/s** | **1282.8 t/s** | **1.003×** |

**The last row is the point of this repository.** The silicon doubles under cuBLAS BF16/TF32 GEMM,
and the quantized workload measures the same on both sides: `gguf` matmul runs on the
*FP16-accumulate* path, which was never throttled.

---

## 本机实测的三条结论

1. **这张 20 GB 改版卡与 3090 走同一条解锁链，完整跑通。** 上游对 3080 标注的 `can_unlock: false`
   在本机上得到了相反的实测结果。
2. **受限的通路是 FP32 累加，与输入精度无关。** FP16 输入 + FP32 累加实测 58.2 → 98.6 TFLOPS，
   同样翻倍。参考机 FP16 前后都是 ~104，因为它测的是 FP16 累加路径。
3. **`gguf` 量化推理在解锁前后一致**（1279.0 → 1282.8 t/s，+0.3%，噪声范围）。量化 matmul 走
   FP16 累加路径；参考机的 +21~26% 来自它自己的服务配置，对应的是 BF16 稠密 GEMM。

### 解锁成功的硬件前提

> **主板 BIOS 必须打开 `Above 4G Decoding`。**

本机在该选项关闭时测得两种结局：正常交接阶段 `ConnectController(recursive=true)` 死锁；
用 `no-reconnect.txt` 跳过交接后，Windows 因 BAR 资源冲突把显卡挂成 **Code 12**
（`STATUS_CONFLICTING_ADDRESSES`），屏幕全黑、仅 RDP 可用。打开该选项后，同一套流程一次通过。
这一条件在工具文档与本记录之外的任何资料里均未出现。

---

## 这台机器

| 项 | 值 |
|---|---|
| GPU | NVIDIA GeForce RTX 3080 **20 GB**（GA102，`10de:2206`，P/N `2206-200-A1`，VBIOS `94.02.27.00.14`） |
| 主板 | ASUS TUF B450M-PRO GAMING，BIOS **4645**（AMI） |
| CPU | Ryzen 9 3900（Zen2，无核显） |
| 系统 | Windows 10 Enterprise LTSC，build 26300（26H2），UEFI，Secure Boot 关，BitLocker 关 |
| 驱动 | 621.01 |
| 解锁工具 | `nv-uefi-unlocker` **v2.1.5**，家族 `ga102` |
| 启动介质 | 主盘上独立的 **1 GiB FAT32 ESP**（与 Windows 的 ESP 相互独立，等效虚拟启动盘） |

工具在执行前现场校验 chip id / 显存大小 / PROM 哈希，校验不过即拒绝执行（fail closed）。
通过校验代表工具认可这张卡的身份；最终结果与主板固件相关，本记录是同型号可参考的一例。

---

## 操作步骤（可复现）

### 1. 准备独立 ESP

工具要求在 Windows ESP 之外有一个 ≥1 GiB 的 FAT32 EFI 系统分区（放 EFI 程序和 7 个固件资产，约 81 MB）。
它同时充当虚拟启动盘——每次开机从它启动，物理 U 盘全程用不上。

```text
diskpart
select disk 2
create partition efi size=1024
format fs=fat32 quick label=GPUUNLOCK
assign letter=S
exit
```

⚠️ 新分区建在未分配空间上即可；Windows 自带的 300 MB ESP 与相邻数据分区保持原样。

### 2. 生成固件资产

七个 `.bin` 从使用者自己下载的驱动包现场再生，本仓库只包含文档与脚本：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\make-usbroot.ps1 -Family ga102 -Work .\inputs -Out .\usbroot
```

两个钉死 SHA-256 的公开输入（约 341 MB），脚本自己下载校验：

- `NVIDIA-kernel-module-source-610.43.03.tar.xz`（26 MB）
- `nvidia-utils-610.43.03-1-x86_64_v3.pkg.tar.zst`（315 MB）

> GitHub / NVIDIA 直连慢的时候，本地代理可快一个数量级：
> `curl -x socks5h://127.0.0.1:1080 -L -C - -o <file> <url>`

### 3. 铺到 ESP 并注册引导项

根目录要能直接看到 `EFI\` 和 7 个 `.bin`。然后用 `bcdedit` 追加一个独立条目，
`{bootmgr}` 始终保持在 `displayorder` 首位（原因见下文事故记录）。

```text
bcdedit /copy {bootmgr} /d "GPU Unlock SS0-SS1 - WRITES REGISTERS"
bcdedit /set {新条目} device partition=S:
bcdedit /set {新条目} path \EFI\BOOT\BOOTX64.EFI
bcdedit /set {fwbootmgr} displayorder {bootmgr} <原有条目...> {新条目}
```

标记文件（放在 ESP 根）：

| 文件 | 作用 |
|---|---|
| `tx-require-d0.txt` | 入口不在 D0（如休眠恢复）即零写入退出。首次尝试建议保留 |
| `i_know_what_i_am_doing.txt` | armed 模式：5 秒倒计时后自动执行，期间任意键中止。倒计时时长为二进制固定值 |
| `no-reconnect.txt` | 跳过 `ConnectController` 交接。本机实测该模式下 Windows 侧 GPU 报 Code 12 |
| `tx-select-bdf.txt` | 多卡时钉死一张，内容 `bb:dd.f` |

### 4. 开机

BIOS 里把解锁条目设为第一启动项、Windows Boot Manager 第二（后者作为兜底保留）。
选择后松开键盘——armed 模式的 5 秒窗口内任何按键都会中止执行。

事务链：

```
claim（校验 chip/显存/PROM） → SEC2 scrubber → FWSEC-FRTS 开 WPR2
→ 加载 Booter / GSP-RM → V67 四写改 PLM → SS0=0x88888888, SS1=0x8
→ PCIe FLR → 读回验证 → RECONNECT → 链回 bootmgfw
```

成功的日志（`\loader.log`）：

```
BOOTER wait_plm: PLM flip 0xffffff8f -> 0xffffffff after ~151500us - payload executed
SELECTOR RESULT: SS0=0x88888888 SS1=0x8
POST-FLR: verified (selectors survive AON)
RECONNECT: ConnectController ok (recursive=true)
PHASE: CHAIN
CHAIN: loader found on ...: \EFI\Microsoft\Boot\bootmgfw.efi
```

`PLM flip` 那一行是 payload 已执行的证据：工具要求入口 PLM 必须是闭合的，
读回值代表状态，翻转才代表执行。

---

## 验证解锁状态（`tools/gemm_probe.py`）

脚本用 `ctypes` 直调 `cublas64_*.dll` / `cudart64_*.dll`（llama.cpp 的 CUDA 版目录里就有），
安装 CUDA Toolkit 与否均可运行。

```powershell
python tools\gemm_probe.py 4096 16
```

| 状态 | BF16 | TF32 | FP16(FP32 累加) |
|---|---:|---:|---:|
| 锁定 | ~51 | ~25 | ~58 |
| 解锁 | ~97 | ~50 | ~99 |

GEMM 吞吐是解锁状态的可靠指标。`nvidia-smi` 与 AIDA64 GPGPU 的读数在锁定/解锁两种状态下相同：
前者暴露的接口不含选择器状态，后者测的是 FP32/FP64/INT/AES/SHA 等标量通路。

（AIDA64 的实际用途是确认整卡健康：FP32 实测 29.98 TFLOPS ≈ 8704 × 2 × 1.71 GHz = 29.78，吻合说明
标量通路与频率正常，解锁改动仅限张量核发射率。）

---

## 失败模式与恢复

| 现象 | 原因 | 处理 |
|---|---|---|
| 30 秒后自动回 Windows，什么都没发生 | 选择条目后 30 秒内未按键，超时即中止（零写入） | 重来，或者启用 armed 模式 |
| 黑屏卡死，`loader.log` 停在 `RECONNECT: ConnectController ... begin` | 交接阶段递归重连死锁（本机在 `Above 4G Decoding` 关闭时出现） | 断电重启 → BIOS 打开 Above 4G Decoding → 重试 |
| 进了系统但显示器黑、只有 RDP 可用 | GPU 被挂成 **Code 12**（`0xC0000018` 地址冲突），本机在跳过重连时出现 | 重启清除，重连走正常交接路径 |
| 日志绿但实测没提升 | 负载是 `gguf` 量化推理（FP16 累加路径） | 正常现象，见上文实测结论 |

整个流程只写易失寄存器，VBIOS 与固件变量保持原样。最坏情况是停在固件阶段，断电重启即回到原厂状态。

---

## 一次引导项事故与修复

手工用 `bcdedit /set {fwbootmgr} displayorder` 追加条目时，正则只匹配 `{36位GUID}`，
`{bootmgr}`（非 GUID 格式）被过滤掉，`displayorder` 一度丢失 Windows Boot Manager 前缀，
并连带删掉了 CD/DVD、Removable、Network 三个固件条目。

修复：按事先导出的 `BootOrderBefore` 显式写回，`{bootmgr}` 回到首位。

> **经验：改 `{fwbootmgr} displayorder` 一律用带校验和回滚的脚本，动手前先 `bcdedit /export` 备份。**

---

## 授权与免责

- 本仓库文档：**CC BY-SA 4.0**
- `tools/gemm_probe.py`：**MIT**
- 本仓库只包含文档与脚本；所有 `.bin` 由使用者从自己的驱动包再生。
- 修改受保护寄存器、执行 PCIe FLR 属于越权操作：**可能失去保修**，失败可能导致本机无法启动
  （需手动断电）。本项目不承担任何责任。
- 非官方。与 NVIDIA、技嘉、华硕均无关。

## 相关项目

| 项目 | 说明 |
|---|---|
| [Zagitta/gpu-unlock](https://github.com/Zagitta/gpu-unlock) | 原始 Linux 内核模块方案（initramfs，驱动加载前改选择器） |
| [physicsdolphin/nv-uefi-unlocker](https://github.com/physicsdolphin/nv-uefi-unlocker) | 本记录使用的 UEFI 实现，跨 OS，支持 20/30/40 系 |
| [rough2515-hub/rtx3080-unlock-public](https://github.com/rough2515-hub/rtx3080-unlock-public) | 首个 3080 的 Windows/UEFI 验证 |
| [wslgy/windows-gpu-unlock-x570-3080-20g-x2](https://github.com/wslgy/windows-gpu-unlock-x570-3080-20g-x2) | 另一条路线（运行时摘控制台驱动）；本机 AMI 固件拓扑与其消费者图断言不兼容，见 `docs/01` |
