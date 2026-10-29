# 现场笔记：八次开机换来的东西

按时间顺序记录每次开机**证明了什么**，包括一条走不通的路线和它的逐门禁取证。
如果你正打算在自己的非标准主板上试，这份笔记能帮你少走几趟。

---

## 路线 A：运行时摘控制台驱动（失败）

项目：[`wslgy/windows-gpu-unlock-x570-3080-20g-x2`](https://github.com/wslgy/windows-gpu-unlock-x570-3080-20g-x2)，
针对技嘉 X570 AORUS PRO WIFI + 双 3080 20GB。它的机制不是"改寄存器后交回"，而是
**在 UEFI 里把控制台驱动和 PCI 驱动从显卡上摘下来**，改完再挂回去，然后交 Windows。

吸引人的地方：它有 3080 20GB 的实测数据（BF16 54→120、TF32 28→59），
而且**这张卡的 PROM 哈希与参考机逐字节相同**（`1a703355…`，同为 VBIOS `94.02.27.00.14`），
看起来移植量很小。

### 只读探针拿到了什么

自带的 `OBSERVE.EFI`（零写入）一次性采到 356 KB 的 DIAG，把这台机器的图结构吐了出来：

| 项 | 实测 | 对参考机 |
|---|---|---|
| BAR0 | `0xf5000000` | 不同（固件分配） |
| chip id | `0xb72000a1` | 相同 |
| 显存 | `0x5000`（20 GiB） | 相同 |
| PROM SHA-256 | `1a703355…` | **相同** |
| PCIe cap | `0x78` | 相同 |
| 控制台设备路径 | `PciRoot(0)→Pci(3,1)→Pci(0,0)→ACPI(00 01)` | root port 与 ACPI 节点不同 |
| `GU_CONSOLE_AGGREGATE` | `0`（TextOut/GOP 各自 BY_DRIVER，无 combo） | 相同 |

冷态 MMIO 九个采样点全部命中代码里写死的期望值（`0x8b8f` / `0x3ff` / `0x10` / `0` /
`0xffffff8f` / `0x02066016` / `6` / `0x5000`），说明**硅与 VBIOS 层面等价**。

### 卡在哪

按 `docs` 里的移植指南填好 profile，编出只读门禁 `PREFLIGHT.EFI`，跑出来的结果：

| 门禁 | status | 含义 |
|---|---|---|
| `preflight-displays` | `0x0` | ✅ |
| `preflight-consumers` | `0x0` | ✅ TextOut/GOP 指纹精确命中 |
| `preflight-alive` | `0x0` | ✅ |
| `preflight-release-consumers` | `0xF` `ACCESS_DENIED` | ❌ |
| `preflight-ami-runtime-code-profile` | `0x13` `CRC_ERROR` | ❌ |
| `preflight-nvidia-code-profile` | `0x3` `UNSUPPORTED` | ❌ |

给门禁加插桩（只加日志、不动判定），把运行时真实边表打出来：

```
GPU PCI 句柄:  PciIo BY_DRIVER (nvgop) + PciIo BY_CHILD        ← 干净
控制台子句柄:  TextOut BY_DRIVER / BY_CHILD                    ← 正常
               GOP     BY_DRIVER                               ← 正常
               c84ac818 私有协议 1c0c34f6-… BY_DRIVER          ← 参考机上没有
               EdidDiscovered 仅 BY_HANDLE，无 BY_DRIVER       ← 关键
```

**两个独立的、结构性的阻塞：**

1. 校验只允许控制台子句柄上存在 `TextOut` / `GOP` / `EDID` 三种边，遇到私有协议边直接拒绝。
2. 这块 AMI 固件的 EDID 运行时驱动**通过私有协议绑定**、把 EDID 协议"装"上去，
   从头到尾没有 `EdidDiscovered BY_DRIVER` 这条边。哪怕容忍第 1 条，后续 `if(edges!=1)`
   仍然拒绝。

这不是常量填错，是固件实现差异。要通过只能改校验逻辑——而那条校验正是"别把脏状态带进系统"
的保险，改它等于抽掉安全网。**因此这条路线在本板上终止，且全程零写入。**

> 结论：**同型号显卡 ≠ 可移植**。这条路线绑的不是 GPU，是主板的固件拓扑。

---

## 路线 B：UEFI 事务（成功）

项目：[`physicsdolphin/nv-uefi-unlocker`](https://github.com/physicsdolphin/nv-uefi-unlocker) v2.1.5，家族 `ga102`。
不看控制台图，只在驱动绑定前做固件事务：开 WPR2 → 跑 Booter → 改 PLM → 写选择器 → FLR。
**没有消费者图断言，所以不受上面那个问题影响。**

### 开机记录

| # | 操作 | 结果 | 学到什么 |
|---|---|---|---|
| 1 | 空格确认 | 黑屏 2.5 分钟，手按电源 | 事务其实成功了，卡在 `ConnectController(recursive=true)` |
| 2 | 加 `no-reconnect.txt` | 进了系统，但显示器黑、只剩 RDP | GPU **Code 12**（`0xC0000018` 地址冲突） |
| 3 | 正常重启 + BIOS 开 `Above 4G Decoding` | 正常 | 资源冲突清除 |
| 4 | 再进解锁项 | **全链通过**，显示器正常 | 见下 |

### 第 4 次的关键日志

```
BOOTER wait_plm: entry FEAT_PLM=0xffffff8f (cold entry must be closed; open entry cannot prove execution)
BOOTER wait_plm: PLM flip 0xffffff8f -> 0xffffffff after ~151500us - payload executed
POST-BOOT: FACTORY FEAT_READOUT_1=0x00400080 (0 = full speed; badf5040 = PLM still closed)
SELECTOR RESULT: SS0=0x88888888 SS1=0x8
PCIe FLR triggered
POST-FLR selectors: ss0=0x88888888 ss1=0x8
PM: post-FLR PMCSR=0x0008 state=D0 (entry 0x0008)
POST-FLR: verified (selectors survive AON)
RECONNECT: ConnectController ok (recursive=true)
PHASE: CHAIN
CHAIN: loader found on Handle(...): \EFI\Microsoft\Boot\bootmgfw.efi
```

### `Above 4G Decoding` 是隐藏前提

前两次失败的**根因是同一个**：显卡的 BAR 窗口在 4 GB 以下空间里挤。

- 不跳过重连 → 递归 `ConnectController` 在该固件上死锁（黑屏，只能断电）
- 跳过重连 → Windows 枚举时 `STATUS_CONFLICTING_ADDRESSES` → Code 12 → 无本地显示

打开 `Above 4G Decoding` 后，BAR 落到 4 GB 以上，同一条流程一次通过。
**这条在任何文档里都没有写**，是本机试出来的。

（顺带：`Resizable BAR` 在这张 20 GB 改版卡上没有生效，`BAR1` 始终 256 MiB。真正起作用的是
`Above 4G Decoding`，两者不是一回事。别只开 ReBAR 就以为解决了。）

---

## 数字

见 [`../README.md`](../README.md) 的结果表。一句话总结：

- 硅层面 BF16 **1.91×**、TF32 **1.97×**，且**穿过 Windows 驱动 621.01 的初始化存活**
- `llama.cpp` 27B IQ3_S `pp512`：1279 → 1283 t/s，**+0.3%，噪声**

原因：量化 matmul 走 FP16 累加，而受限的是 FP32 累加通路。**解锁买不到你的量化推理性能。**

## 什么负载会受益

- ✅ BF16 / TF32 稠密 GEMM：扩散模型（DiT/attention）、LoRA 微调、非量化推理
- ❌ `gguf` 量化推理的 prefill 与 decode、INT8 整数内核
- ❌ 任何带宽受限的负载（decode 阶段基本不动）
