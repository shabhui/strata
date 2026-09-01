# tools/

打包脚本、验证脚本,以及一堆性能调查用的一次性脚本。**都不是库的一部分** ——
`src/strata/` 不 import 这里的任何东西,删掉任何一个都不影响程序运行。

放在仓库里是因为源码注释里那些「实测 X 秒」的数字得有来路。数字会过时,
但重跑的方法留着,下次怀疑某个结论时不用从头搭测量环境。

## 打包与验证

| 文件 | 干什么 |
|---|---|
| `build_exe.py` | 打包 `dist/Strata.exe`。**用这个,别直接 pyinstaller strata.spec** —— 后者拿不到 spec 里依赖的路径变量 |
| `strata.spec` | PyInstaller 配置。`hiddenimports` 和 `datas` 两份清单由 `tests/test_packaging.py` 钉着 |
| `strata.manifest` | 请求管理员权限的 manifest,写进 exe 之后双击就弹 UAC |
| `entry.py` | exe 的入口。不用 `strata/__main__.py`:那样会被当顶层脚本跑,包内相对导入就没父包了 |
| `sources.py` | 算「打进 exe 的源码」指纹。`build_exe` 写下,`verify_exe` 比对 |
| `verify_exe.py` | 拉起打好的 exe,跑一遍接口和路由 |
| `verify_wof_fix.py` | WOF 压缩文件的大小口径改完之后,在真盘上核对 |
| `verify_usn_paths.py` | USN 路径还原能否在**非管理员**下跑通(这条链路刻意不需要提权) |
| `verify_task_command.py` | 计划任务注册的那条命令,真的跑得起来吗 |
| `verify_elevated_gaps.py` | 把两件只有管理员能验的事一次验完,省一次 UAC |

## 提权运行(Windows)

带 UAC manifest 的程序在 Git Bash 里起不来,而 PowerShell 的 `-Verb RunAs`
和 `-RedirectStandardOutput` 互斥。这几个 bat 是绕过去的办法,里面的
`chcp 65001` + `PYTHONIOENCODING` + `cd /d` 三件套都是必需的 —— 少哪个都会
得到一个空结果文件(理由写在各自的文件头)。

| 文件 | 干什么 |
|---|---|
| `run_elevated.bat` | 提权跑 `tools/` 下任意脚本,输出存 `tools/*_result.txt` |
| `run_exe_elevated.bat` | 提权跑打好的 `dist/Strata.exe`,输出存 `dist/` |
| `run_scan_elevated.bat` | 提权跑一次完整扫描 |
| `run_bench_elevated.bat` | 提权跑基准脚本 |

## 性能调查(`bench_` / `prof_` / `probe_`)

一次性脚本,回答一个具体问题就不再动了。**结论已经写进源码注释**,这里是
重跑的入口。`bench_` 比较两种做法,`prof_` 拆一段时间花在哪,`probe_` 查真盘
上的事实。

分桶那个 O(n²)(D: 盘 289 秒 → 20 秒)是从 `prof_scan_stages.py` 顺着
往下查出来的,MFT 缓冲区复用是 `prof_mft_buffer.py` 定位的 —— 这两个是
这批脚本最值回票价的地方。

| 文件 | 回答的问题 |
|---|---|
| `prof_scan_stages.py` | 一次扫描的各阶段各花多少秒(**查性能问题从这个开始**) |
| `prof_collect_stages.py` | 收集条目那一段内部再拆 |
| `prof_pipeline.py` | 收集之后那五遍,各自值多少秒 |
| `prof_dbwrite.py` | 写库那一段值多少秒 |
| `prof_mft_buffer.py` | 每块新分配 8 MiB 缓冲区的代价(合成数据上 72.6s → 15.5s) |
| `prof_mft_perchunk.py` | 196 块里每块各花多少秒 —— 定位 N² 从哪来 |
| `prof_mft_convert.py` | MFT 条目 → ScanEntry 那一段 |
| `prof_mft_fresh.py` | 每个规模换新进程量,避免同进程互相污染 |
| `prof_mft_gc.py` | 解析变慢是不是 GC 的锅(不是,只占 5%) |
| `prof_parse_hot.py` | 解析一条 1 KB 记录的 42 微秒花在哪 |
| `prof_cpu_sustained.py` | 这台机器满载会不会掉频 —— 排除硬件因素 |
| `bench_paths_head2head.py` | mft 和 scandir 两条路背靠背比(比值稳在 1.43x) |
| `bench_two_drives.py` | 同进程连扫两个盘,第二个慢多少 |
| `bench_parse_record.py` | 单条记录解析的上限(合成记录) |
| `bench_mft.py` | 对真实卷跑一次 MFT 解析并计时 |
| `bench_mft_read.py` | 读盘占多少、解析占多少(读盘只占 1.3 秒) |
| `bench_nobuffering.py` | `FILE_FLAG_NO_BUFFERING` 让顺序读慢多少(不慢) |
| `bench_dbwrite_order.py` | 页缓存大小对写库的影响(2 MB → 64 MiB 快 3.66x) |
| `bench_dir_paths.py` | 目录路径还原的几种写法 |
| `bench_inode.py` | 遍历时顺手取 `st_ino` 的代价(整盘 68s → 165s,所以没这么做) |
| `bench_reparse_pass.py` | 联接点那一遍的代价 |
| `bench_walk.py` | scandir 遍历本身 |
| `stress_walk.py` | 遍历在异常目录结构下会不会崩 |
| `probe_wof.py` | WOF 压缩文件在 MFT 里长什么样 |
| `probe_wof_shapes.py` | WOF 的几种形态各占多少(78,587 个文件差 38.54G) |
| `probe_wof_db.py` | 库里对 WOF 记的是逻辑大小还是占盘大小 |
| `probe_overcount.py` | 多报的字节从哪来 |
| `probe_runlist_truth.py` | 运行列表里非稀疏段的真实占用 |
| `probe_measured.py` | 在真库副本上造更早的快照,验证实测层对比 |

## 注意

- 大部分 `bench_`/`prof_`/`probe_` 要管理员权限(直读 MFT)。
- 真盘上单次测量方差很大(同样代码量到 45.52 / 47.89 / 71.37 / 89.61 秒)。
  **报比值不报绝对值**,或者同进程连跑取多次最小值 —— 跨进程、不配对的计时
  在有负载的机器上不能用。这一处栽过,细节在 `tests/test_bucket_fast_path.py`
  的文件头。
- 输出文件(`*_result.txt`、`*.out`)已在 `.gitignore` 里。
