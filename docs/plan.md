# Strata 实施计划

**目标:** 一个本地工具,回答「我的 C:/D: 空间被什么吃掉了,什么时候吃的」——刚装好就能看到过去几个月的增长。

**架构:** 直读 NTFS MFT 拿到全盘文件清单(秒级),读 USN 日志拿到删除/重命名事件;
按文件创建时间把历史增长「回溯」重建出来,所以首次运行立刻有时间轴;
之后每天一个真实快照,时间轴从「推测」逐步变成「实测净增减」。

**技术栈:** Python 3.12 标准库(零依赖) + SQLite + 原生 JS/Canvas(自绘树图,不引图表库)

## 全局约束

- 零第三方依赖。后端只用标准库,前端不引 CDN。装完即可跑,不受网络影响。
- Windows 专用。需要管理员权限(用户已确认),`strata.bat` 自动 UAC 提权。
- 所有字体用 Windows 自带(Bahnschrift / Segoe UI / Microsoft YaHei UI / Cascadia Mono),不下载字体。
- 数据库放 `%LOCALAPPDATA%\Strata\strata.db`,不写进项目目录。
- 服务只绑 `127.0.0.1`,不监听外部接口。
- 界面中文。

## 数据两层

| 层 | 来源 | 覆盖时间 | 精度 |
|---|---|---|---|
| 回溯层 | MFT 里每个文件的创建/修改时间 | 全盘历史(几个月~几年) | 只有「新增」,看不到删除 |
| 实测层 | 每日快照两两对比 + USN 事件 | 从安装那天起 | 精确净增减,含删除 |

界面上两者视觉区分:回溯用斜纹空心柱,实测用实心柱。不假装推测值是实测值。

## 文件结构

```
strata.bat                    双击启动(UAC 提权 → 起服务 → 开浏览器)
src/strata/
  __main__.py                    CLI: serve / scan / schedule / doctor
  config.py                      路径、端口、忽略规则
  privileges.py                  管理员检测 + 自提权
  ntfs/
    volume.py                    打开 \\.\C:、解析引导扇区、扇区对齐读
    runlist.py                   数据运行(mapping pairs)解码
    attributes.py                属性头 + $STANDARD_INFORMATION/$FILE_NAME/$DATA
    mft.py                       MFT 记录流式解析 → 文件条目
    usn.py                       USN 日志枚举(删除/重命名/新建)
  scan/
    walker.py                    scandir 后备扫描器(MFT 失败时)
    tree.py                      条目 → 目录树 + 汇总 + 按日期分桶
    snapshot.py                  扫描编排 → 写快照
  store/
    schema.sql / db.py           SQLite 层
  analysis/
    timeline.py                  每日增减(回溯 + 实测合并)
    diff.py                      两快照对比 → 增减明细
    hotspots.py                  吃空间大户 + 可清理项
  server/
    app.py / api.py              stdlib HTTP 服务 + JSON API
  web/
    index.html / app.css / app.js  界面(自绘 Canvas 树图 + SVG 时间轴)
tests/                           单元测试(含合成 MFT 记录夹具)
```

## 视觉方向

主张:**现有工具都在看「现在有多大」,这个工具看「什么时候来的」。所以时间是主视觉变量。**
面积 = 大小,颜色 = 年龄。全盘每一个字节都按到达时间着色。

调色板(地质岩芯:表层热、深层冷)

| 令牌 | 值 | 用途 |
|---|---|---|
| `--ground` | `#0B1416` | 底色,极深petrol |
| `--ground-2` | `#111E21` | 面板 |
| `--rule` | `#1E3238` | 细线 |
| `--ink` | `#D6E4E5` | 正文 |
| `--ink-dim` | `#6E8A8F` | 次要 |
| `--age-0` | `#FF5C39` | 今天 |
| `--age-1` | `#FF9E2C` | 本周 |
| `--age-2` | `#E8C46A` | 本月 |
| `--age-3` | `#8FBF9F` | 本季 |
| `--age-4` | `#4E93A8` | 今年 |
| `--age-5` | `#2F5D72` | 更早 |

字体:显示字面 Bahnschrift SemiCondensed(Win 自带,DIN 系,仪表盘气质);
正文 Segoe UI / Microsoft YaHei UI;数据与路径全部 Cascadia Mono 右对齐,单位用暗色。

结构装置:不用 01/02/03 编号(内容不是序列)。用细线 + 右对齐等宽数字 +
常驻的 `as of <时间戳>` 基准行,像仪器读数。

**签名元素:地层树图。** 方块面积是大小,颜色是子树内最新写入的年龄。
点击年龄图例色块 → 其他方块淡到 8%,只剩那个年龄段的数据,
「本周新增的东西全在这一块」一眼可见。拖时间轴则重定向整个视图。

动效克制:树图筛选时方块补间 200ms;时间轴柱子加载时从基线长出,错开 8ms。
其余没有。遵守 `prefers-reduced-motion`。

## 任务

1. **存储层** — schema + db.py,能建库、写读快照。测试:建库→写快照→读回。
2. **NTFS 引导 + runlist** — volume.py + runlist.py。测试:合成引导扇区、已知 runlist 字节串解码。
3. **MFT 解析** — attributes.py + mft.py。测试:合成 MFT 记录(含 fixup)解析出名字/大小/父引用。
4. **后备扫描器** — walker.py,scandir 递归。测试:临时目录树扫出正确大小。
5. **树构建 + 分桶** — tree.py。测试:条目列表 → 目录汇总正确、按日期分桶正确。
6. **快照编排** — snapshot.py,MFT 优先、失败退 scandir。测试:对临时目录跑通全流程。
7. **USN 日志** — usn.py + 事件入库。测试:结构解包用合成缓冲区。
8. **分析** — timeline/diff/hotspots。测试:两个人造快照 → 增减明细正确。
9. **HTTP + API** — app.py/api.py。测试:起服务、打各端点、校验 JSON 形状。
10. **界面** — index.html/app.css/app.js,自绘 squarified 树图 + SVG 时间轴。
11. **启动器 + 计划任务** — strata.bat 提权、`schedule` 子命令注册每日扫描。
12. **联调** — 提权跑真实 C:/D: 扫描,核对总量与资源管理器一致。
