# PyInstaller 打包配置。用 tools/build_exe.py 跑,别直接 pyinstaller 这个文件
# ——那样拿不到下面依赖的路径变量。
#
# 单文件(onefile):给别人用的时候只有一个 exe,不用解释「把整个文件夹拷过去」。
# 代价是每次启动要把内容解到临时目录,冷启动慢一两秒。这个工具一次开着用很久,
# 换启动速度换分发方便是值得的。
#
# 带控制台:这是个本地服务器,窗口里要显示访问地址和报错。关掉窗口就等于停服务,
# 对用户来说是能理解的模型。

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent          # noqa: F821  (SPECPATH 由 PyInstaller 注入)
SRC = ROOT / "src"

a = Analysis(
    # 入口是 tools/entry.py,不是 strata/__main__.py —— 后者会被当成顶层
    # 脚本跑,包内的相对导入就没有父包了,exe 一起来就 ImportError。
    # 详见 tools/entry.py 里的说明。
    [str(ROOT / "tools" / "entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    # 非 Python 文件 PyInstaller 不会自己发现,必须显式带上,漏一个就是运行到
    # 那条路径才崩。src 树里的非 .py 文件一共就这两处(web/ 整个目录 +
    # schema.sql),目标路径要和 config.bundle_dir() 拼出来的一致。
    #
    # web/ 是整个目录一起带,不是一个个文件列 —— 所以往里加文件(比如 i18n.js)
    # 不用改这儿。列文件的写法早晚会漏一个,而漏了要到打包出来点开页面才发现。
    datas=[
        (str(SRC / "strata" / "web"), "web"),
        (str(SRC / "strata" / "store" / "schema.sql"), "store"),
    ],
    # 这些是运行时才 import 的(__main__ 里各个 cmd_* 都是函数内 import),
    # 静态分析能顺着找到,但写出来更保险:少一个就是运行到那条命令才崩。
    #
    # 这份清单自己栽过,值得写下来:里面曾有一条 strata.analysis.treemap —— 那个
    # 模块**从来不存在**,源码里也没有任何地方 import 它。PyInstaller 找不到只
    # 打一行 `ERROR: Hidden import not found` 就继续,exe 照样打出来照样能跑,
    # 所以这条假条目安安稳稳待了很久。同时真实存在的 diff/hotspots/paths 三个
    # 反倒没列 —— 它们能进 exe 全靠静态分析,而上面那句注释说的正是「不指望静态
    # 分析」。于是这份清单当时的实际状态是:一条保护不存在的东西,三条该保护的
    # 没保护,而它看起来在保护十个。
    #
    # 这和项目里那句「只会通过的检查比没有检查更糟」是同一件事:清单不报错,不
    # 等于清单是对的。所以下面按 src/strata/analysis/ 的真实内容列全,并且
    # tests/test_packaging.py 里加了一条:清单里每个名字都必须真能 import。
    hiddenimports=[
        "strata.analysis.diff",
        "strata.analysis.hotspots",
        "strata.analysis.paths",
        "strata.analysis.timeline",
        "strata.ntfs.volume",
        "strata.reveal",
        "strata.scan.changes",
        "strata.scan.snapshot",
        "strata.schedule",
        "strata.server.api",
        "strata.server.app",
        "strata.store.db",
    ],
    hookspath=[],
    runtime_hooks=[],
    # 用不上的大件排掉。这些都不是我们的依赖,但环境里装了的话
    # PyInstaller 的静态分析有时会顺着别的包把它们拖进来,一拖就是几十 MB。
    excludes=[
        "tkinter", "unittest", "pydoc", "doctest",
        "numpy", "PIL", "pytest", "setuptools", "pip",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)                               # noqa: F821

exe = EXE(                                      # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="Strata",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                                  # UPX 常被杀软误报,不值得
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 一律要求管理员:读 MFT 和 USN 日志需要。写进 manifest 之后,
    # 双击就会弹 UAC,不用再靠 bat 自我提权那一套。
    uac_admin=True,
    manifest=str(ROOT / "tools" / "strata.manifest"),
)
