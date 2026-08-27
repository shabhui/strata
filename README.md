# Strata

看清 C 盘和 D 盘的空间是**什么时候**、被**什么东西**吃掉的。

磁盘分析工具满地都是,但它们只回答"现在谁占得多"。真正想知道的往往是另一个问题:**这三天多出来的 40 GB 是哪来的?** Strata 按天记录每个目录的体积变化,像地层一样一层层堆起来,所以你翻的是时间轴,不是文件树。

- 纯 Python 标准库,运行时零第三方依赖
- 本地网页界面,只监听 `127.0.0.1`,数据不出本机
- 装好当天就能看到过去几天的增长(见下面的两层历史)
- 直读 NTFS MFT,全盘扫描通常几秒;拿不到权限就自动退回普通遍历

## 两层历史

刚装好的工具没有历史数据,这是这类程序的通病。Strata 用两层数据绕过去,并且**在界面上从不把两者混在一起**:

| | 回溯层 `retro` | 实测层 `measured` |
|---|---|---|
| 来源 | 按文件创建时间把现存文件分到各天 | 相邻两次快照相减 |
| 装好就有 | 是 | 否,要等第二次快照 |
| 看得见删除 | **看不见** | 看得见 |
| 含义 | 净增长的**下限** | 真实净变化 |
| 图上样式 | 斜纹填充 | 实心填充 |

回溯层的盲区是真实存在的:上周下了个 10 GB 的文件、昨天删了,它在回溯层里查无此人。所以它只是下限,界面上用斜纹提醒你这一点。跑够两天以后,实测层接手。

管理员权限下还会读 USN 变更日志,把删除事件也补进实测层。

## 用起来

**给别人用**:下载 [Releases](../../releases) 里的 `Strata.exe`,双击。会弹 UAC(要读 MFT),然后自动开浏览器。

**从源码跑**(Python 3.11+,Windows):

```bash
python -m strata
```

不带参数等于 `serve --admin`:提权、启动 `http://127.0.0.1:8731`、开浏览器。

其它子命令:

```bash
python -m strata scan --drives C: D:    # 只扫一次,不开界面
python -m strata schedule on --at 12:30 # 注册每日快照计划任务
python -m strata doctor                 # 体检:权限、盘、库、日志、计划任务
```

每天至少要有一次快照,时间轴才长得出来,所以建议把计划任务开着。`schedule on` 用的是 Windows 任务计划程序,不留后台进程。

## 界面

- **地层树图**:方块面积是体积,颜色是这段时间的增减。点进去下钻,右键在资源管理器里打开。
- **每日增减**:每天净变化的时间轴。`Ctrl + 滚轮`缩放,按住拖动平移,`0` 回到全景。碰上某天暴涨几十 GB 把其它天压成一根线时,按 `L` 切对数轴(以 1 MB 为底,0 也画得出来)。
- **热点榜**:增长最多的目录,按归属层聚合,不会出现 `Users` 套 `Users\alice` 套下去的重复条目。

## 数据放哪

一个 SQLite 库:`%LOCALAPPDATA%\Strata\strata.db`。日志同目录。程序目录和临时目录里不写任何数据,所以 exe 换个地方放不影响历史。

删掉那个目录就等于重置,不影响磁盘上的实际文件 —— Strata 只读盘,从不删你的东西。

## 开发

```bash
PYTHONPATH=src python -m unittest discover -s tests -t . -q
python tools/build_exe.py        # 打包 dist/Strata.exe
python tools/verify_exe.py       # 拉起 exe 跑一遍接口和路由
```

设计说明在 [docs/plan.md](docs/plan.md),包括为什么快照按目录而不按文件存、MFT 解析踩过的坑。

## 已知边界

- 只支持 Windows NTFS。MFT 和 USN 那两条路径依赖 NTFS 结构,别的文件系统会退回普通遍历。
- 回溯层依赖文件创建时间。有些安装器会重写这个时间戳,那部分增长的日期就是错的。
- 未做验证:MFT / USN 代码路径的单元测试用的是合成样本,尚未在多种真实硬件上跑过。

## English

Strata answers *when* your Windows disk filled up, not just *what* is on it. It snapshots per-directory sizes daily and shows net change over time. Two data layers, never mixed in the UI: a **retro** layer that buckets existing files by creation date (works on day one, blind to deletions, a lower bound) and a **measured** layer that diffs consecutive snapshots (true net change). Reads the NTFS MFT directly when elevated, falls back to a normal walk otherwise. Pure Python standard library, no runtime dependencies, local web UI bound to `127.0.0.1` only.

## 许可

GPL-3.0。见 [LICENSE](LICENSE)。
