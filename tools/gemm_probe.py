#!/usr/bin/env python3
"""BF16 / TF32 / FP16 tensor-core GEMM probe via cuBLAS, without a CUDA Toolkit install.

Why this exists: the RTX 30 series unlock (SS0/SS1 speed selectors) doubles the
FP32-accumulate tensor instruction issue rate. Nothing in nvidia-smi exposes that,
and AIDA64 GPGPU cannot see it either -- AIDA64 measures scalar FP32/FP64/INT/AES/SHA
paths, which never touch a tensor core. This probe measures the only thing that moves:

    locked   : BF16 ~51, TF32 ~25, FP16(FP32 acc) ~58   TFLOPS
    unlocked : BF16 ~97, TF32 ~50, FP16(FP32 acc) ~99   TFLOPS
    (RTX 3080, 320 W, 1710 MHz, n=4096)

It calls cublas64_*.dll / cudart64_*.dll through ctypes, so any directory that already
holds a CUDA build works -- e.g. the folder next to `ggml-cuda.dll` from a llama.cpp
CUDA release.

Usage:
    python gemm_probe.py                     # 4096 x 4096, 16 iterations
    python gemm_probe.py 8192 10             # bigger matrix, fewer iterations
    python gemm_probe.py --dll-dir "C:\\Program Files\\NVIDIA GPU Computing Toolkit\\CUDA\\v13.4\\bin"

MIT License.
"""
import ctypes as C
import glob
import os
import sys
import time

# cudaDataType_t
R_32F, R_16F, R_16BF = 0, 2, 14
# cublasComputeType_t
COMP_32F, COMP_32F_FAST_TF32 = 68, 77
OP_N = 0


def find_dll_dir(explicit=None):
    """Locate a directory holding both cublas64_*.dll and cudart64_*.dll."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("CUBLAS_DIR"):
        candidates.append(os.environ["CUBLAS_DIR"])
    # CUDA Toolkit installs
    candidates += sorted(glob.glob(os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        "NVIDIA GPU Computing Toolkit", "CUDA", "*", "bin")), reverse=True)
    # wherever python is running from / cwd
    candidates += [os.getcwd(), os.path.dirname(os.path.abspath(__file__))]
    # anything on PATH
    candidates += os.environ.get("PATH", "").split(os.pathsep)

    for d in candidates:
        if not d or not os.path.isdir(d):
            continue
        if glob.glob(os.path.join(d, "cublas64_*.dll")) and glob.glob(os.path.join(d, "cudart64_*.dll")):
            return d
    return None


def main():
    argv = [a for a in sys.argv[1:]]
    dll_dir = None
    if "--dll-dir" in argv:
        i = argv.index("--dll-dir")
        dll_dir = argv[i + 1]
        del argv[i:i + 2]
    n = int(argv[0]) if len(argv) > 0 else 4096
    iters = int(argv[1]) if len(argv) > 1 else 16

    dll_dir = find_dll_dir(dll_dir)
    if not dll_dir:
        sys.exit("找不到同时含 cublas64_*.dll 与 cudart64_*.dll 的目录。\n"
                 "请用 --dll-dir 指定（llama.cpp CUDA 版目录或 CUDA Toolkit\\bin 均可）。")
    cublas_dll = sorted(glob.glob(os.path.join(dll_dir, "cublas64_*.dll")))[-1]
    cudart_dll = sorted(glob.glob(os.path.join(dll_dir, "cudart64_*.dll")))[-1]
    print(f"cublas : {os.path.basename(cublas_dll)}")
    print(f"cudart : {os.path.basename(cudart_dll)}")

    os.add_dll_directory(dll_dir)
    cudart = C.CDLL(cudart_dll)
    cublas = C.CDLL(cublas_dll)

    cudart.cudaMalloc.argtypes = [C.POINTER(C.c_void_p), C.c_size_t]
    cudart.cudaFree.argtypes = [C.c_void_p]
    cudart.cudaMemcpy.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t, C.c_int]
    cudart.cudaDeviceSynchronize.restype = C.c_int
    cudart.cudaGetErrorString.argtypes = [C.c_int]
    cudart.cudaGetErrorString.restype = C.c_char_p

    cublas.cublasCreate_v2.argtypes = [C.POINTER(C.c_void_p)]
    cublas.cublasDestroy_v2.argtypes = [C.c_void_p]
    cublas.cublasGemmEx.argtypes = [
        C.c_void_p, C.c_int, C.c_int,                 # handle, transa, transb
        C.c_int, C.c_int, C.c_int,                    # m, n, k
        C.c_void_p, C.c_void_p, C.c_int,              # alpha, A, Atype
        C.c_int, C.c_void_p, C.c_int,                 # lda, B, Btype
        C.c_int, C.c_void_p, C.c_void_p,              # ldb, beta, C
        C.c_int, C.c_int, C.c_int,                    # Ctype, ldc, computeType
        C.c_int,                                      # algo
    ]

    def check(code, what):
        if code != 0:
            msg = cudart.cudaGetErrorString(code).decode(errors="replace") if what.startswith("cuda") else str(code)
            raise RuntimeError(f"{what} failed: {code} {msg}")

    def run(dtype, compute, elem, one, label):
        handle = C.c_void_p()
        check(cublas.cublasCreate_v2(C.byref(handle)), "cublasCreate")
        nbytes = n * n * elem
        ptrs = []
        for _ in range(3):
            p = C.c_void_p()
            check(cudart.cudaMalloc(C.byref(p), nbytes), "cudaMalloc")
            ptrs.append(p)
        A, B, Cm = ptrs
        buf = C.create_string_buffer((one * (n * n))[:nbytes], nbytes)
        for p in ptrs:
            check(cudart.cudaMemcpy(p, buf, nbytes, 1), "cudaMemcpy")
        ab = C.create_string_buffer((one * 8)[:8], 8)
        for _ in range(3):  # warmup
            cublas.cublasGemmEx(handle, OP_N, OP_N, n, n, n, ab, A, dtype, n,
                                B, dtype, n, ab, Cm, dtype, n, compute, -1)
        check(cudart.cudaDeviceSynchronize(), "cudaDeviceSynchronize")
        t0 = time.perf_counter()
        for _ in range(iters):
            cublas.cublasGemmEx(handle, OP_N, OP_N, n, n, n, ab, A, dtype, n,
                                B, dtype, n, ab, Cm, dtype, n, compute, -1)
        check(cudart.cudaDeviceSynchronize(), "cudaDeviceSynchronize")
        dt = time.perf_counter() - t0
        tf = 2.0 * n ** 3 * iters / dt / 1e12
        print(f"{label:5s} n={n} iters={iters}  {dt / iters * 1e3:8.2f} ms/iter  {tf:7.2f} TFLOPS")
        for p in ptrs:
            cudart.cudaFree(p)
        cublas.cublasDestroy_v2(handle)
        return tf

    print(f"probe n={n} iters={iters}")
    bf = run(R_16BF, COMP_32F, 2, b"\x80\x3f", "BF16")        # bf16 1.0
    tf = run(R_32F, COMP_32F_FAST_TF32, 4, b"\x00\x00\x80\x3f", "TF32")  # fp32 1.0
    fp = run(R_16F, COMP_32F, 2, b"\x00\x3c", "FP16")         # fp16 1.0
    print()
    print(f"BF16 {bf:.1f}  TF32 {tf:.1f}  FP16 {fp:.1f}  (TFLOPS)")
    print("=> BF16 已解锁 (>=90)" if bf >= 90 else "=> BF16 锁定态 (约 51 为正常锁定值)")


if __name__ == "__main__":
    main()
