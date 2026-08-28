"""路径形状的小工具。

放在这里是因为热点和时间轴都要判断「这条路径的祖先是不是已经收录了」,
两边都在做同一件事:一个目录树上的一件事,只该报一次。
"""

from __future__ import annotations

from ..store.db import SEP


def has_ancestor_in(path: str, found: set[str] | dict) -> bool:
    """path 的某个祖先是否已经收录。

    等价于 any(path.startswith(p + SEP) for p in found),但复杂度从「found 有
    多大」变成「路径有多深」 —— 前者每一对还要拼一次字符串。

    收益别指望太大:2538 条真实路径固定拿 19 个 found 比,4.80 ms 降到 2.18 ms,
    但真实循环里 found 是从空集长起来的,前面大部分行本来就没什么可比,整个接口
    只快了 0.6 ms(15.3 → 14.7 ms)。留着是因为复杂度更好、代码也更短。

    只在分隔符位置切,所以 Windows\\Temp 收录之后 Windows\\Temp2 不会被误判成
    它的后代。
    """
    i = path.find(SEP)
    while i != -1:
        if path[:i] in found:
            return True
        i = path.find(SEP, i + 1)
    return False


def is_ancestor_of(ancestor: str, path: str) -> bool:
    """ancestor 是不是 path 的严格祖先。

    要求后面紧跟分隔符,不然 Windows\\Temp 会认领 Windows\\Temp2。
    自己不算自己的祖先。
    """
    return path.startswith(ancestor + SEP)
