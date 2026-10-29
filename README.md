# RTX 3080 20GB 算力解锁 · 非 X570 主板实战记录

> A field report: unlocking the FP32-accumulate tensor throughput of a **modded RTX 3080 20 GB**
> on an **ASUS TUF B450M-PRO GAMING (BIOS 4645, AMI)** — a board that appears in no verified list.
>
> 不是 fork、不是上游发行版。这里没有上游代码，只有**一台特定机器的实测记录、失败模式的取证、和一个不依赖 CUDA Toolkit 的测量工具**。

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

**The last row is the point of this repository.** The silicon really does double, and the workload
really does not care: `gguf` quantized matmul runs on the *FP16-accumulate* path, which was never
throttled. If your only workload is quantized LLM inference, this unlock buys you nothing.

---

## 为什么值得写下来

关于这项解锁，网上流传的三条说法在这台机器上都不成立或不完整：

1. **"上游对 3080 是 `can_unlock: false`"** —— 在这张 20 GB 改版卡上，它跟 3090 走同一条链，跑通了。
2. **"FP16 不受限，只有 BF16/TF32 受限"** —— 不准确。受限的是**FP32 累加**这条通路，与输入精度无关。
   FP16 输入 + FP32 累加实测 58.2 → 98.6，同样被裁。参考机 FP16 前后都 ~104，是因为它测的是 FP16 累加。
3. **"解锁能提升 LLM 推理"** —— 对 `gguf` 量化推理是 **0%**。参考机那个 +21~26% 来自它自己的服务配置，
   不是量化 matmul 内核。

另外一个**没有出现在任何文档里**的关键条件：

> **主板 BIOS 必须打开 `Above 4G Decoding`。**
> 关着的时候，事务能成功、选择器能穿过 FLR，但交接阶段 `ConnectController(recursive=true)` 会死锁；
> 若用 `no-reconnect.txt` 跳过它，Windows 会因 BAR 资源冲突把显卡挂成 **Code 12**（`STATUS_CONFLICTING_ADDRESSES`），
> 屏幕全黑、只剩 RDP 可用。打开 `Above 4G Decoding` 后同一套流程一次通过。

---

## 这台机器

| 项 | 值 |
|---|---|
| GPU | NVIDIA GeForce RTX 3080 **20 GB**（GA102，`10de:2206`，P/N `2206-200-A1`，VBIOS `94.02.27.00.14`） |
| 主板 | ASUS TUF B450M-PRO GAMING，BIOS **4645**（AMI） |
| CPU | Ryzen 9 3900（Zen2，**无核显**） |
| 系统 | Windows 10 Enterprise LTSC，build 26300（26H2），UEFI，Secure Boot 关，BitLocker 关 |
| 驱动 | 621.01 |
| 解锁工具 | `nv-uefi-unlocker` **v2.1.5**，家族 `ga102` |
| 启动介质 | 主盘上独立的 **1 GiB FAT32 ESP**（不是 Windows 的 ESP，不是 U 盘） |

> 你这张卡不在任何已验证清单里。工具会现场校验 chip id / 显存大小 / PROM 哈希再决定是否执行，
> 校验不过就拒绝（fail closed）。通过校验**不等于**在你的板子上一定成功——这份记录是同型号可参考的一例，不是保证。

---

## 做了什么（可复现步骤）

### 1. 准备独立 ESP

工具要求在 Windows ESP 之外有一个 ≥1 GiB 的 FAT32 EFI 系统分区（放 EFI 程序和 7 个固件资产，约 81 MB）。
它同时充当"虚拟启动盘"——不需要物理 U 盘，每次开机从它启动即可。

```text
diskpart
select disk 2
create partition efi size=1024
format fs=fat32 quick label=GPUUNLOCK
assign letter=S
exit
```

⚠️ 不要用 Windows 自带那个 300 MB 的 ESP。也不要对相邻分区点"扩展卷"——那会把空闲空间吃掉。

### 2. 生成固件资产

七个 `.bin` 从**你自己下载的**驱动包现场再生，**本仓库不附带任何 NVIDIA 固件**：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\make-usbroot.ps1 -Family ga102 -Work .\inputs -Out .\usbroot
```

两个钉死 SHA-256 的公开输入（约 341 MB），脚本自己下载校验：

- `NVIDIA-kernel-module-source-610.43.03.tar.xz`（26 MB）
- `nvidia-utils-610.43.03-1-x86_64_v3.pkg.tar.zst`（315 MB）

> 若 GitHub / NVIDIA 直连很慢，走本地代理会快一个数量级：
> `curl -x socks5h://127.0.0.1:1080 -L -C - -o <file> <url>`

### 3. 铺到 ESP 并注册引导项

根目录要能直接看到 `EFI\` 和 7 个 `.bin`。然后用 `bcdedit` 追加一个独立条目，
**`{bootmgr}` 必须保持在 `displayorder` 首位**（见下文事故记录）。

```text
bcdedit /copy {bootmgr} /d "GPU Unlock SS0-SS1 - WRITES REGISTERS"
bcdedit /set {新条目} device partition=S:
bcdedit /set {新条目} path \EFI\BOOT\BOOTX64.EFI
bcdedit /set {fwbootmgr} displayorder {bootmgr} <原有条目...> {新条目}
```

标记文件（放在 ESP 根）：

| 文件 | 作用 |
|---|---|
| `tx-require-d0.txt` | 入口不在 D0（如休眠恢复）就**零写入放弃**。首次尝试建议保留 |
| `i_know_what_i_am_doing.txt` | **armed 模式**：5 秒倒计时后自动执行，期间任意键中止。**0 秒不可配置** |
| `no-reconnect.txt` | **不要用**。跳过 `ConnectController` 会让 Windows 拿不到 BAR → Code 12 |
| `tx-select-bdf.txt` | 多卡时钉死一张，内容 `bb:dd.f` |

### 4. 开机

BIOS 里把解锁条目设为第一启动项、Windows Boot Manager 第二（后者是兜底，别删）。
选择后**松手别碰键盘**——armed 模式的 5 秒窗口里任意按键都会被当成中止。

事务链：

```
claim（校验 chip/显存/PROM） → SEC2 scrubber → FWSEC-FRTS 开 WPR2
→ 加载 Booter / GSP-RM → V67 四写改 PLM → SS0=0x88888888, SS1=0x8
→ PCIe FLR → 读回验证 → RECONNECT → 链回 bootmgfw
```

成功的日志长这样（`\loader.log`）：

```
BOOTER wait_plm: PLM flip 0xffffff8f -> 0xffffffff after ~151500us - payload executed
SELECTOR RESULT: SS0=0x88888888 SS1=0x8
POST-FLR: verified (selectors survive AON)
RECONNECT: ConnectController ok (recursive=true)
PHASE: CHAIN
CHAIN: loader found on ...: \EFI\Microsoft\Boot\bootmgfw.efi
```

`PLM flip` 那一行是"booter 真的执行了"的铁证——工具特意要求入口 PLM 必须是闭合的，
读回不代表执行，翻转才算。

---

## 自己验证（`tools/gemm_probe.py`）

不需要安装 CUDA Toolkit：脚本用 `ctypes` 直调 `cublas64_*.dll` / `cudart64_*.dll`
（llama.cpp 的 CUDA 版目录里就有）。

```powershell
python tools\gemm_probe.py 4096 16
```

| 状态 | BF16 | TF32 | FP16(FP32 累加) |
|---|---:|---:|---:|
| 锁定 | ~51 | ~25 | ~58 |
| 解锁 | ~97 | ~50 | ~99 |

**这是解锁的唯一可靠体温计。** `nvidia-smi` 看不出来，AIDA64 GPGPU 也看不出来——
后者测的是 FP32/FP64/INT/AES/SHA 等标量通路，一个张量核指令都没有，锁定/解锁数字应该完全一致。

（AIDA64 的用处是确认整卡健康：FP32 实测 29.98 TFLOPS ≈ 8704 × 2 × 1.71 GHz = 29.78，吻合即说明
标量通路与频率正常，解锁确实没动它们。）

---

## 失败模式与恢复

| 现象 | 原因 | 处理 |
|---|---|---|
| 30 秒后自动回 Windows，什么都没发生 | 没按空格/回车，超时即**中止**（零写入） | 重来，或者启用 armed 模式 |
| 黑屏卡死，`loader.log` 停在 `RECONNECT: ConnectController ... begin` | 交接阶段递归重连死锁（本机在 `Above 4G Decoding` 关闭时出现） | 断电重启 → **BIOS 打开 Above 4G Decoding** → 重试 |
| 进了系统但显示器黑、只有 RDP 可用 | GPU 被挂成 **Code 12**（`0xC0000018` 地址冲突），通常是跳过重连导致的 | 重启清除。**不要用 `no-reconnect.txt`** |
| 日志绿但实测没提升 | 负载是 `gguf` 量化推理（FP16 累加路径） | 正常现象，见上表 |

**没有变砖路径**：不刷 VBIOS、不换驱动、不写固件变量。最坏情况是停在固件里，断电重来即可。

---

## 一次真实事故（留作教训）

手工用 `bcdedit /set {fwbootmgr} displayorder` 追加条目时，正则只匹配 `{36位GUID}`，
把 `{bootmgr}`（不是 GUID 格式）过滤掉了，`displayorder` 一度丢失 Windows Boot Manager 前缀，
并连带删掉了 CD/DVD、Removable、Network 三个固件条目。

修复：按事先导出的 `BootOrderBefore` 显式写回，`{bootmgr}` 回到首位。

> **教训：改 `{fwbootmgr} displayorder` 一律用带校验和回滚的脚本，不要手工拼参数。
> 动手前先 `bcdedit /export` 备份。**

---

## 授权与免责

- 本仓库文档：**CC BY-SA 4.0**
- `tools/gemm_probe.py`：**MIT**
- **不含任何 NVIDIA 固件、VBIOS、驱动或 CUDA 二进制**。所有 `.bin` 必须由使用者从自己的驱动包再生。
- 修改受保护寄存器、执行 PCIe FLR 属于越权操作：**可能失去保修**，失败可能导致本机无法启动
  （需手动断电）。本项目不承担任何责任。
- 非官方。与 NVIDIA、技嘉、华硕均无关。

## 相关项目

| 项目 | 说明 |
|---|---|
| [Zagitta/gpu-unlock](https://github.com/Zagitta/gpu-unlock) | 原始 Linux 内核模块方案（initramfs，驱动加载前改选择器） |
| [physicsdolphin/nv-uefi-unlocker](https://github.com/physicsdolphin/nv-uefi-unlocker) | 本记录使用的 UEFI 实现，跨 OS，支持 20/30/40 系 |
| [rough2515-hub/rtx3080-unlock-public](https://github.com/rough2515-hub/rtx3080-unlock-public) | 首个 3080 的 Windows/UEFI 验证 |
| [wslgy/windows-gpu-unlock-x570-3080-20g-x2](https://github.com/wslgy/windows-gpu-unlock-x570-3080-20g-x2) | 另一条路线（运行时摘控制台驱动），板上 AMI 固件拓扑不兼容，见 `docs/02` |
