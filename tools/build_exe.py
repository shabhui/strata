"""打包成单文件 exe。

    python tools/build_exe.py

产出 dist/Strata.exe —— 一个文件,拷给别人双击就能用,对方不需要装 Python。

PyInstaller 只是构建期工具,不进产物,所以「运行时零第三方依赖」这条没破:
打出来的 exe 里只有标准库和我们自己的代码。

没装 PyInstaller 的话,这个脚本会用国内镜像装(直连 PyPI 在国内经常几十 KB/s)。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "tools" / "strata.spec"

# 按顺序试。清华的同步最勤,阿里云在部分网络下更快。
MIRRORS = [
    ("清华", "https://pypi.tuna.tsinghua.edu.cn/simple", "pypi.tuna.tsinghua.edu.cn"),
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple", "mirrors.aliyun.com"),
    ("腾讯云", "https://mirrors.cloud.tencent.com/pypi/simple", "mirrors.cloud.tencent.com"),
]


def human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024 or unit == "GB":
            return f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} GB"


def have_pyinstaller() -> str | None:
    proc = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def install_pyinstaller() -> str:
    """从镜像装 PyInstaller。逐个镜像试,全挂了才报错。"""
    for name, index, host in MIRRORS:
        print(f"  试 {name} 镜像 …", flush=True)
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller",
             "-i", index, "--trusted-host", host, "--timeout", "30"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            version = have_pyinstaller()
            if version:
                print(f"  装好了:PyInstaller {version}(来自{name})")
                return version
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        print(f"  {name} 不行:{' / '.join(tail)}")
    raise SystemExit(
        "三个镜像都装不上 PyInstaller。检查网络,或者手动装:\n"
        "  pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="把 Strata 打包成单文件 exe")
    parser.add_argument("--keep-build", action="store_true",
                        help="保留 build/ 中间产物,排查打包问题时用")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("这个 exe 只能在 Windows 上打包(产物也只在 Windows 上跑)。",
              file=sys.stderr)
        return 1

    print("Strata 打包")
    print("-" * 52)

    version = have_pyinstaller()
    if version:
        print(f"PyInstaller {version} 已就绪")
    else:
        print("没找到 PyInstaller,从国内镜像装:")
        install_pyinstaller()

    # 每次从干净状态打。上一次的 dist/ 留着会让人分不清拿到的是新的还是旧的。
    for path in (ROOT / "build", ROOT / "dist"):
        if path.exists():
            shutil.rmtree(path)

    print(f"\n配置:{SPEC.relative_to(ROOT)}")
    print("开始打包,大概一两分钟 …\n", flush=True)

    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC),
         "--noconfirm", "--distpath", str(ROOT / "dist"),
         "--workpath", str(ROOT / "build")],
        cwd=str(ROOT),
    )
    elapsed = time.perf_counter() - started

    if proc.returncode != 0:
        print(f"\n打包失败(返回码 {proc.returncode})。上面有 PyInstaller 的报错。",
              file=sys.stderr)
        return proc.returncode

    exe = ROOT / "dist" / "Strata.exe"
    if not exe.exists():
        print("\n打包命令说成功了,但 dist/Strata.exe 不在。", file=sys.stderr)
        return 1

    if not args.keep_build:
        shutil.rmtree(ROOT / "build", ignore_errors=True)

    # 记下这个 exe 是从哪版代码打出来的。没有这个,verify_exe 只能验「这个文件
    # 能跑」,验不了「这个文件是不是当前代码」—— 改了 src/ 忘了重打,它照样
    # 报全部通过。
    sys.path.insert(0, str(ROOT / "tools"))
    import sources
    fp = sources.write()

    print("\n" + "-" * 52)
    print(f"好了:{exe}")
    print(f"源码指纹:{len(fp)} 个文件 → {sources.FINGERPRINT.name}")
    print(f"大小:{human_size(exe.stat().st_size)},耗时 {elapsed:.0f}s")
    print()
    print("这一个文件拷给谁都能用,对方不用装 Python。")
    print("双击会弹 UAC(要管理员才能读 MFT 和变更日志),同意之后浏览器自动打开。")
    print()
    print("建议先自己验一遍:")
    print(f'  "{exe}" doctor')
    return 0


if __name__ == "__main__":
    sys.exit(main())
