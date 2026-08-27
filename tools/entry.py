"""打包用的入口。

不能直接把 src/strata/__main__.py 当入口脚本交给 PyInstaller:那样它会被
当成顶层脚本执行,模块名是 __main__ 而不是 strata.__main__,里面的
`from . import config` 就没有父包可言 ——

    ImportError: attempted relative import with no known parent package

exe 一起来就崩,每个子命令都崩。所以这里绕一层:以绝对导入的方式把包
import 进来,再调它的 main()。这样 strata 是个正常的包,包内的相对导入
照常能用。
"""

from __future__ import annotations

import sys

from strata.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
