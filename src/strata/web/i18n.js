/* 中英文案。零依赖,和其余部分一样。
 *
 * 为什么把两种语言并排放在一个键里,而不是 {zh:{...}, en:{...}} 两份字典:
 * 分成两份的话,加一条中文忘了加英文,页面上就直接漏出中文,而代码里看不出
 * 少了什么 —— 缺失是静默的。并排放着,少一半一眼就看得见,还能写个测试断言
 * 「每个键两边都不空」(见 tests/test_i18n.py)。
 *
 * 值可以是字符串,也可以是函数。英文有单复数(1 day / 2 days),中文没有,
 * 所以需要函数的地方就写函数,不必为了统一给中文也套一层。
 */

'use strict';

const ZH = 0;
const EN = 1;

/* 英文复数。n 为 1 时用单数。
 *
 * 有这么个函数是因为散着写三元表达式漏得太容易 —— 实际上就漏过:界面上出现过
 * 「1 days measured」。中文没有这回事,所以只有英文那半用得上它。
 *
 * 不规则复数(entry/entries)传第三个参数;规则的只传单数形式。
 *
 * 这里先 Number() 一下,因为调用方经常传的是**格式化过的字符串**:界面上
 * 是 t('del.coverageEvents', { n: fmtCount(cov.events) }),送进来的是 '1' 而不是 1。
 * 原来直接 n === 1 严格比,'1' 不等于 1,于是界面上印的是「, 1 entries」——
 * 而测试给的是数字,看到的是「, 1 entry」,一路绿灯。这是这个项目最反对的那种
 * 检查:输入形状跟线上不一样,所以它只会通过。
 *
 * 已经有 5 个 key 各自写了 plural(Number(v.n), ...) 绕过这件事(age.days、
 * grid.fileCount 那几个),说明这个坑踩过了,只是没修在根上。挪到这儿来一次,
 * 那几处的 Number() 就成了冗余但无害的写法。
 *
 * '1,234' 这种带千位分隔符的会 Number() 成 NaN,而 NaN !== 1,走复数 —— 正确。 */
function plural(n, one, many) {
  return Number(n) === 1 ? one : (many !== undefined ? many : one + 's');
}

/* 千位分隔符。app.js 里有个 fmtCount 做同一件事,但这个文件必须能单独加载
 * (tests/test_i18n.py 就是拿 node 单独跑它的),不能反过来依赖 app.js。 */
function fmtInt(n) {
  return Number(n || 0).toLocaleString(LOCALES[index()]);
}

// key: [中文, English]
const STRINGS = {
  // ---- 顶栏 / 全局 ----
  'app.title': ['Strata — 空间是什么时候被吃掉的',
                'Strata — when your disk space went'],
  'brand.tagline': ['面积是大小,颜色是年龄', 'Area is size, color is age'],
  'nav.drives': ['选择磁盘', 'Select drive'],
  'nav.scan': ['扫描本盘', 'Scan this drive'],
  /* 这两条是故意反的:按钮上写的是「按下去会变成哪种语言」,所以中文界面上
   * 写 English,英文界面上写中文。tests/test_i18n.py 那条「英文里不该有汉字」
   * 的检查按 .lang 后缀放过它们。 */
  'nav.lang': ['English', '中文'],
  'nav.langTitle': ['Switch to English', '切换到中文'],
  'baseline.loading': ['读取中……', 'Loading…'],

  // ---- 首次运行 ----
  'firstrun.title': ['还没有扫描过。', 'Nothing scanned yet.'],
  'firstrun.body': [
    '点右上角「扫描本盘」跑一次,几十秒就好。第一次扫完就能看到过去几个月的' +
    '变化 —— 靠的是现存文件的创建日期倒推,不用等着攒数据。',
    'Hit Scan this drive at the top right; it takes under a minute. The very ' +
    'first scan already shows the past few months, worked back from the ' +
    'creation dates of files still on disk. You do not have to wait for ' +
    'history to pile up.',
  ],

  // ---- 时间轴 ----
  'tl.title': ['每日增减', 'Daily change'],
  'tl.sub': ['往右是今天。柱子朝上是增,朝下是减。',
             'Today is at the right. Bars up mean growth, down mean shrink.'],
  'tl.empty': ['还没有数据。扫一次就有了。', 'No data yet. Run a scan.'],
  'tl.zoomGroup': ['缩放', 'Zoom'],
  'tl.zoomOut': ['缩小(-)', 'Zoom out (−)'],
  'tl.zoomIn': ['放大(+)', 'Zoom in (+)'],
  'tl.showAll': ['看全部(0)', 'Show everything (0)'],
  'tl.panGroup': ['平移', 'Pan'],
  'tl.panLeft': ['往左(←)', 'Pan left (←)'],
  'tl.panRight': ['往右(→)', 'Pan right (→)'],
  'tl.recentGroup': ['最近', 'Recent'],
  'tl.spanDays': [(v) => `${v.n} 天`, (v) => `${v.n} ${plural(v.n, 'day')}`],
  'tl.log': ['对数纵轴', 'Log scale'],
  'tl.logTitle': [
    '纵轴取对数。大小差一百倍的日子就都看得见了(L)',
    'Log the vertical axis, so a day 100× bigger than another no longer ' +
    'flattens it to nothing (L)',
  ],
  'tl.hint': ['Ctrl + 滚轮缩放,按住拖动平移',
              'Ctrl + wheel to zoom, drag to pan'],
  'tl.shellLabel': [
    '每日空间增减时间轴,方向键平移,加号减号缩放,0 看全部',
    'Daily disk change timeline. Arrow keys pan, plus and minus zoom, ' +
    '0 shows everything.',
  ],
  'tl.svgLabel': ['每日空间增减时间轴', 'Daily disk change timeline'],
  'tl.measuredStart': ['↓ 从这里开始是实测', '↓ measured from here on'],
  'tl.allDays': [(v) => `全部 ${v.n} 天`, (v) => `all ${v.n} ${plural(v.n, 'day')}`],
  'tl.rangeSpan': [
    (v) => `${v.from} → ${v.to}  共 ${v.n} 天`,
    (v) => `${v.from} → ${v.to}  ${v.n} ${plural(v.n, 'day')}`,
  ],

  // ---- 两个层的说明 ----
  'basis.retro': [
    '回溯值:那天写入、现在还留在盘上的量。删掉的东西它看不见,所以只增不减,' +
    '不等于那天的净变化。',
    'Inferred: how much of what was written that day is still on disk now. ' +
    'It cannot see anything deleted since, so it only ever goes up — it is ' +
    'not that day’s net change.',
  ],
  'basis.measured': [
    '实测值:两次扫描的差,含删除,是真实净增减。',
    'Measured: the difference between two scans. Deletions included, so this ' +
    'is the real net change.',
  ],
  'basis.retroShort': ['回溯', 'inferred'],
  'basis.measuredShort': ['实测', 'measured'],
};

Object.assign(STRINGS, {
  // ---- 树图 ----
  'tm.title': ['地层树图', 'Strata treemap'],
  'tm.sub': [
    '块的大小是占用,颜色是这一块里最新写入的时间。点色带只看某个年龄段。',
    'Block size is disk usage, color is the newest write inside it. Click a ' +
    'band to isolate one age.',
  ],
  'tm.legendLabel': ['按年龄筛选', 'Filter by age'],
  'tm.canvasLabel': ['按大小和年龄排布的目录树图',
                     'Directory treemap laid out by size and age'],
  'tm.empty': ['这里还没有数据,先扫一次', 'Nothing here yet — run a scan'],
  'tm.onlyBand': [(v) => `只显示${v.band}写入的部分`,
                  (v) => `Showing only what was written ${v.band}`],
  'tm.shareOfLevel': [(v) => `  占本层 ${v.share}`, (v) => `  ${v.share} of this level`],
  'tm.lastWrite': ['最近写入 ', 'last write '],
  'tm.clickEnter': ['点击进入', 'click to enter'],
  // 树图上补的那一块:本层直属文件 + 扫描时被折叠的小目录。
  // 不叫「其他」是因为那听起来像凑数;这两样都是真实占用,只是没有独立格子。
  'tm.restOfLevel': ['本层文件', 'files at this level'],
  'tm.restHint': ['直接放在本目录下的文件,不属于任何子目录',
                  'files sitting directly in this folder, not in any subfolder'],
  'tm.restWithFolded': [
    (v) => `本目录下的文件,以及 ${v.n} 个太小而未单列的子目录`,
    (v) => `files in this folder, plus ${v.n} ${plural(v.n, 'subfolder')} too small to list`,
  ],
  'tm.crumbHint': ['点块进入子目录', 'click a block to go deeper'],
  'tm.thisLevel': ['本层 ', 'this level '],

  // ---- 删除记录 ----
  'del.title': ['消失了什么', 'What disappeared'],
  'del.sub': ['从 NTFS 变更日志里读出来的删除记录。时间戳看不到这些。',
              'Deletions read from the NTFS change journal. Timestamps cannot ' +
              'show you these.'],
  'del.none': ['这段时间没有记录到删除', 'No deletions recorded in this window'],
  'del.coverage': ['日志覆盖 ', 'journal covers '],
  'del.coverageSpan': [(v) => `${v.first} 起 ${v.days} 天`,
                       (v) => `${v.days} ${plural(v.days, 'day')} from ${v.first}`],
  'del.coverageEvents': [(v) => `,${v.n} 条`,
                         (v) => `, ${v.n} ${plural(v.n, 'entry', 'entries')}`],
  'del.sizeNote': [
    'USN 日志不记录文件大小,这里的字节数是从历史快照反查的,查不到就留空。',
    'The USN journal does not record file sizes. The byte counts here are looked ' +
    'up from past snapshots, and stay blank when no snapshot knew the file.',
  ],
  'del.unknownSize': [
    (v) => ` 其中 ${v.n} 条在历史快照里找不到同路径的文件,大小算不出来,` +
           '这里留空而不是猜。',
    (v) => ` ${v.n} of them have no matching path in any snapshot, so the size ` +
           'cannot be worked out. Left blank rather than guessed.',
  ],
  // 提了权但日志读不了。以前跟「真的没删过东西」走同一条路 —— 整段藏掉,
  // 用户看到的跟「什么都没删过」一模一样,而真相是「我没看成」。
  // NTFS 上 USN 日志是可以关的,不少机器默认就是关的,所以这条很常见。
  // why 是后端存下来的具体原因,原样带出来:前端猜不出是哪种,猜错比不说更糟。
  'del.unavailable': [
    (v) => `读不了这个盘的变更日志,所以这一栏是空的 —— 不是因为没删过东西,`
           + `而是没看成。原因:${v.why}`,
    (v) => 'The change journal for this drive could not be read, so this panel is '
           + 'empty. Not because nothing was deleted, but because it could not '
           + `look. Reason: ${v.why}`,
  ],
  'del.unavailableUnknown': ['没有记下具体原因', 'no reason was recorded'],
  'del.unavailableRow': ['读不了变更日志', 'Change journal unreadable'],
  // 反查不出路径的行。日志记的是文件 ID,得靠扫描时的目录表换成路径 —— 整棵
  // 目录树在扫描前就没了的话换不出来。实盘上 D: 盘 32031 条删除里只有 450 条
  // 能换出路径。这种行只有名字,而名字会重(problems-report.html 出现 5 次),
  // 看着像列表出了毛病,所以得说一句这是怎么回事。
  'del.noPathRow': [
    '只知道文件名,不知道它在哪 —— 它所在的目录在扫描之前就已经删掉了,'
    + '反查不出路径。同名的行是不同的文件。',
    'Only the name is known, not the location. The directory it lived in was '
    + 'already gone before the scan, so the path cannot be recovered. Rows '
    + 'sharing a name are different files.',
  ],
  'del.noPathNote': [
    (v) => ` 另有 ${v.n} 条只查到文件名:它们所在的目录在扫描前就没了,`
           + '反查不出路径,所以排在后面,也没法定位。',
    (v) => ` Another ${v.n} ${plural(v.n, 'entry', 'entries')} have a name but no `
           + 'path: the directories they lived in were gone before the scan. They '
           + 'sort last and cannot be located.',
  ],
  // 一个文件在窗口里被删了好多次。USN 记的是操作不是文件,一次「建-写-关-删」
  // 就是好几条记录,而反复重建的临时文件会攒出上百条。现在按路径合并成一行,
  // 但次数本身有用 —— 它把「丢了个东西」和「这是个临时文件」区分开,所以标出来。
  'del.times': [(v) => `×${v.n}`, (v) => `×${v.n}`],
  'del.timesNote': [
    (v) => `这个路径在窗口里出现 ${v.n} 次,合并成了一行。时间是最后一次。`,
    (v) => `This path appears ${v.n} ${plural(v.n, 'time')} in the window, ` +
           'collapsed into one row. The timestamp is the most recent one.',
  ],
  // 不提权时读不了 USN 日志,这一栏永远是空的。以前整段藏掉,于是这个功能
  // 对非管理员用户等于不存在 —— 现在把话说出来。
  'del.needAdmin': [
    '读变更日志需要管理员权限。现在这一栏是空的,不是因为没有删除过东西,' +
    '而是没权限去看。以管理员身份重新启动就能看到。',
    'Reading the change journal needs administrator rights. This panel is empty ' +
    'because it cannot look, not because nothing was deleted. Restart as ' +
    'administrator to see it.',
  ],
  'del.needAdminRow': ['没有权限读取', 'No permission to read'],
  // 只有一次快照时说这句。原来整段藏掉,第一次用的人不知道「再扫一次就有对比」。
  'diff.needTwo': [
    '要两次扫描才能对比。现在只有一次快照 —— 过一段时间再扫一次,' +
    '这里就会显示这期间涨了什么、缩了什么。',
    'Comparing needs two scans. There is only one snapshot so far. Scan again ' +
    'later and this will show what grew and what shrank in between.',
  ],
  // 表格空态。不能写「没有目录变大」—— 那是比较过之后的结论,而这里根本没比。
  'diff.notYet': ['还没有可比的两次扫描', 'No two scans to compare yet'],

  // ---- 两次扫描之间 ----
  'diff.title': ['最近两次扫描之间', 'Between the last two scans'],
  'diff.grew': ['长大了', 'Grew'],
  'diff.shrank': ['缩小了', 'Shrank'],
  'diff.change': ['变化', 'Change'],
  'diff.net': ['净变化 ', 'net '],
  'diff.noGrow': ['没有目录变大', 'No directory grew'],
  'diff.noShrink': ['没有目录变小', 'No directory shrank'],

  /* 对比的口径说明。代号由 analysis/diff.py 给,数字也一起给过来 ——
   * 「几层」「多少 MB」是 config 里的值,不能在文案里写死。 */
  /* size 是已经格式化好的字符串("64 MB"),不是字节数。文案函数不调
   * fmtBytes —— 那个在 app.js 里,这个文件要能单独加载(tests/test_i18n.py
   * 就是拿 node 单独跑它来检查每个键两边都不空的)。 */
  'diff.caveat.demoted': [
    (v) => `参与对比的快照中有 ${v.n} 个已降级为粗粒度,深度超过 ${v.depth} 层` +
           `且小于 ${v.size} 的目录不参与比较`,
    (v) => `${v.n} of the compared snapshots ${plural(v.n, 'has', 'have')} been ` +
           `coarsened: directories deeper than ${v.depth} ` +
           `${plural(v.depth, 'level')} and smaller than ${v.size} are left out ` +
           `of the comparison`,
  ],
  'diff.caveat.mixedMethod': [
    (v) => `两次扫描方式不同(${v.before} vs ${v.after}),硬链接与占盘大小的` +
           `算法有差异,增减值仅供参考`,
    (v) => `The two scans used different methods (${v.before} vs ${v.after}). ` +
           `Hard links and on-disk size are counted differently, so treat the ` +
           `deltas as rough`,
  ],
  'diff.caveat.rulesChanged': [
    (v) => `这两个快照之间,${v.method} 的占盘算法修过一次:以前把 Compact OS ` +
           `压缩过的系统文件按解压后的大小算了。所以这里的「减少」有一部分是` +
           `口径修正,不是硬盘上真的少了东西`,
    (v) => `Between these two snapshots the ${v.method} size accounting was ` +
           `corrected: Compact OS compressed system files used to be counted at ` +
           `their uncompressed size. Part of the drop shown here is that ` +
           `correction, not data actually leaving the disk`,
  ],
  'diff.caveat.noFilesEitherSide': [
    '两个快照都没有文件明细(已降级),只能对比目录',
    'Neither snapshot kept file-level detail (both coarsened), so only directories are compared',
  ],
  'diff.caveat.noFilesOneSide': [
    '其中一个快照的文件明细已被降级清除,跳过文件级对比',
    'One snapshot had its file-level detail dropped when it was coarsened, so file comparison is skipped',
  ],
  'diff.caveat.fileThreshold': [
    '文件明细只收录超过阈值的文件,「新出现」也可能是原本就在、只是这次才超过阈值',
    'File detail only covers files above a size threshold, so "newly appeared" may ' +
    'mean a file was already there and only now crossed the threshold',
  ],

  // ---- 空间大户 ----
  'hot.title': ['空间大户', 'Biggest consumers'],
  'hot.sub': ['排除了父子重复,同一份空间只算一次。',
              'Parent/child overlap removed, so no byte is counted twice.'],
  'hot.dir': ['目录', 'Directory'],
  'hot.usage': ['占用', 'On disk'],
  'hot.lastWrite': ['最近写入', 'Last write'],
  'hot.recent': ['最近在长的', 'Recently growing'],

  // ---- 可以清的 ----
  'clean.title': ['可以清的', 'Worth clearing'],
  'clean.sub': ['只标出来,不动手。删不删由你决定。',
                'Flagged only, never touched. Deleting is your call.'],
  'clean.safetyHead': ['安全等级', 'Safety'],
  'clean.safeWhy': ['缓存类,删了会自动重建。',
                    'Caches. They rebuild themselves.'],
  'clean.reviewWhy': ['可能有你要的东西。', 'May hold something you want.'],
  'clean.carefulWhy': ['影响系统或软件功能,别手删。',
                       'Affects the system or an app. Do not hand-delete.'],
  'clean.safe': ['可删', 'Safe'],
  'clean.review': ['先看看', 'Look first'],
  'clean.careful': ['当心', 'Careful'],
  'clean.path': ['路径', 'Path'],
  'clean.kind': ['类型', 'Kind'],
  'clean.level': ['等级', 'Level'],
  'clean.none': ['没找到明显可清的目录',
                 'Nothing obviously clearable turned up'],
  'clean.total': ['合计 ', 'total '],
  'clean.ofWhichSafe': [(v) => `,其中标「可删」 ${v.bytes}`,
                        (v) => `, ${v.bytes} of it marked Safe`],

  // ---- 设置 ----
  'set.title': ['设置', 'Settings'],
  'set.sub': ['快照存在本机,不联网。',
              'Snapshots stay on this machine. Nothing goes out.'],
  'set.dailyOn': ['每天自动拍一次快照(已开)',
                  'Daily snapshot (on)'],
  'set.daily': ['每天自动拍一次快照', 'Take a snapshot once a day'],
  'set.dailyWhy': [
    '开了以后每天自动扫一次。时间轴上的实测数据靠它攒 —— 不开就只有你手动扫的那几天。',
    'Turns on one scan a day. That is what builds the measured layer of the ' +
    'timeline — without it you only get the days you scanned by hand.',
  ],
  'set.next': ['下次 ', 'next '],
  'set.last': ['上次 ', 'last '],
  'set.result': ['结果 ', 'result '],
  'set.paused': ['已暂停。', 'Paused.'],
  'set.adminYes': ['管理员权限:有。', 'Administrator: yes.'],
  'set.adminNo': ['管理员权限:没有。', 'Administrator: no.'],
  'set.adminYesWhy': [
    ' 走 MFT + 变更日志,全盘扫描几十秒,删除记录也能看到。',
    ' Using the MFT and change journal: a whole drive in well under a minute, ' +
    'and deletions are visible.',
  ],
  'set.adminNoWhy': [
    ' 只能退回目录遍历:更慢,而且看不到删除记录。用 strata.bat 启动会自动提权。',
    ' Falling back to walking directories: slower, and deletions stay ' +
    'invisible. Launch via strata.bat to elevate automatically.',
  ],
  'set.db': [
    (v) => `数据库 ${v.path}(${v.bytes})。不联网,不上传。`,
    (v) => `Database at ${v.path} (${v.bytes}). No network, nothing uploaded.`,
  ],

  // ---- 右键菜单 ----
  'ctx.open': ['在资源管理器打开', 'Open in File Explorer'],
  'ctx.locate': ['在资源管理器中定位', 'Show in File Explorer'],
  'ctx.parent': ['打开上一级目录', 'Open parent folder'],
  'ctx.copy': ['复制完整路径', 'Copy full path'],
  'ctx.enter': ['在树图里进入这一层', 'Drill into this level'],
  'ctx.copied': ['已复制:', 'Copied: '],
  'ctx.copyManual': ['复制不了,已经选中路径,按 Ctrl+C',
                     'Could not copy. The path is selected — press Ctrl+C.'],
  'ctx.copyFailed': ['复制失败:', 'Copy failed: '],
  'ctx.close': ['关闭', 'Close'],

  // ---- 年龄 ----
  'age.today': ['今天', 'today'],
  'age.week': ['本周', 'this week'],
  'age.month': ['本月', 'this month'],
  'age.quarter': ['三个月内', 'past 3 months'],
  'age.year': ['一年内', 'past year'],
  'age.older': ['更早', 'older'],
  'age.unknown': ['未知', 'unknown'],
  'age.yesterday': ['昨天', 'yesterday'],
  /* 一律 Number() 之后再判单复数。年份那条 app.js 传的是 toFixed(1) 的字符串
   * ("1.0"),天和月传的是数字 —— 直接跟 1 比的话,得记住哪条是哪种类型,记错
   * 就印出「1 files」那种东西(grid.fileCount 真这么错过)。 */
  'age.daysAgo': [(v) => `${v.n} 天前`,
                  (v) => `${v.n} ${plural(Number(v.n), 'day')} ago`],
  'age.monthsAgo': [(v) => `${v.n} 个月前`,
                    (v) => `${v.n} ${plural(Number(v.n), 'month')} ago`],
  'age.yearsAgo': [(v) => `${v.n} 年前`,
                   (v) => `${v.n} ${plural(Number(v.n), 'year')} ago`],

  // ---- 基线 / 状态 ----
  'base.used': ['已用', 'used'],
  'base.free': ['可用', 'free'],
  'base.scanned': ['扫到', 'saw'],
  'base.walkMode': ['目录遍历', 'directory walk'],
  'base.files': ['文件', 'files'],
  'base.snapshots': ['快照', 'snapshots'],
  'base.snapshotCount': [(v) => `${v.n} 个`, (v) => v.n],
  'base.noSnapshot': ['尚无快照', 'no snapshot yet'],
  // 这条原来写死在 app.js 里,是中文界面上唯一一句漏出来的英文
  'base.asOf': ['截至 ', 'as of '],
  'base.offline': ['这个盘现在读不到,显示的是历史数据',
                   'This drive is unreadable right now — showing stored data'],

  // ---- 表格通用 ----
  'grid.noMatch': ['没有符合条件的项', 'Nothing matches'],
  'grid.noData': ['还没有扫描数据', 'No scan data yet'],
  /* 这里原来写的是 v.n === '1' —— 跟字符串比。传进来的是数字,1 === '1' 是
   * false,所以 1 个文件时印的是「1 files」。一律走 plural(),别再各写一份。 */
  'grid.fileCount': [(v) => `${v.n} 个文件`,
                     (v) => `${v.n} ${plural(Number(v.n), 'file')}`],
  'grid.itemCount': [(v) => `,${v.n} 项`,
                     (v) => `, ${v.n} ${plural(Number(v.n), 'item')}`],
  'grid.size': ['大小', 'Size'],
  'grid.time': ['时间', 'Time'],

  // ---- 时间轴提示框 ----
  'tip.stillOnDisk': ['写入还在盘上 ', 'written and still on disk '],
  'tip.added': ['新增 ', 'added '],
  'tip.removed': ['  减少 ', '  removed '],
  'tip.net': ['  净 ', '  net '],
  'tip.journalCreated': ['变更日志:建 ', 'journal: created '],
  'tip.journalDeleted': ['  删 ', '  deleted '],
  'tip.grewMost': ['长得最多', 'grew most'],
  'tip.shrankMost': ['缩得最多', 'shrank most'],
  'tip.retroWhy': [
    '这是那天写入、现在还留在盘上的量。当天建了又删的看不到,那天删掉的旧文件' +
    '也看不到 —— 不等于那天的净变化',
    'This is what was written that day and is still on disk. Files created ' +
    'and deleted the same day are invisible, and so are older files deleted ' +
    'that day — it is not that day’s net change',
  ],

  // ---- 时间轴汇总 ----
  'sum.retroSpan': [
    (v) => `近 ${v.days} 天写入还在盘上 `,
    (v) => `written in the last ${v.days} ${plural(v.days, 'day')}, still on disk `,
  ],
  'sum.measuredSpan': [
    (v) => `${v.sep}实测 ${v.days} 天净变化 `,
    (v) => `${v.sep}net change measured over ${v.days} ${plural(v.days, 'day')} `,
  ],
  'sum.addedRemoved': [
    (v) => `  增 ${v.added} · 减 ${v.removed}`,
    (v) => `  +${v.added} · −${v.removed}`,
  ],
  'sum.retroDays': [(v) => `${v.n} 天回溯`,
                    (v) => `${v.n} ${plural(v.n, 'day')} inferred`],
  'sum.measuredDays': [(v) => `${v.n} 天实测`,
                       (v) => `${v.n} ${plural(v.n, 'day')} measured`],
  'sum.busiest': [(v) => `涨得最猛是 ${v.day}`, (v) => `biggest jump ${v.day}`],
  'sum.allRetro': ['现在全是回溯值。', 'Everything here is inferred right now.'],
  'sum.allRetroWhy1': [
    '只有一次快照时,变化只能从现存文件的创建日期倒推 —— 删掉的东西看不见。',
    'With only one snapshot, change can only be worked back from the creation ' +
    'dates of files that still exist — deletions are invisible.',
  ],
  'sum.allRetroWhy2': [
    '再扫一次(或者开着每日快照)之后,新的日子就会变成实测。',
    'Scan once more (or leave the daily snapshot on) and new days become ' +
    'measured.',
  ],

  // ---- 扫描 ----
  'scan.running': ['扫描中……', 'Scanning…'],
  'scan.inProgress': ['正在扫描', 'Scanning'],
  'scan.phase': [(v) => `${v.drive} ${v.phase},大盘要几十秒`,
                 (v) => `${v.drive} ${v.phase} — a big drive takes a minute`],
  /* 数到多少了。不显示百分比:总数要等扫完才知道,拿上一次快照的数去估,
   * 会在盘上东西变多时卡在 99% —— 那比没有进度更让人以为卡死了。
   * 一个见涨的绝对数说明的是同一件事(它还在动),而且不会说谎。 */
  /* n 是原始数字,两种语言都自己格式化 —— 跟 tl.spanDays 一路。
   * 不预先在 app.js 里格式化成字符串再传进来:英文这半要拿数字判单复数,
   * 传字符串就得反过来解析,而 toLocaleString 在不同语言下会加不同的分隔符。 */
  'scan.phaseCounted': [(v) => `${v.drive} ${v.phase},已数 ${fmtInt(v.n)} 项`,
                        (v) => `${v.drive} ${v.phase} — ${fmtInt(v.n)} ` +
                               `${plural(v.n, 'item', 'items')} so far`],
  'scan.failed': ['上次扫描失败:', 'Last scan failed: '],
  'scan.doneAt': ['扫完于 ', 'finished '],
  'scan.seconds': [' 秒', 's'],

  // ---- 报错 ----
  'err.notJson': ['返回的不是 JSON', 'Response was not JSON'],
  'err.request': [(v) => `请求失败(${v.status})`,
                  (v) => `Request failed (${v.status})`],
  'err.noBackend': [(v) => `连不上后端:${v.detail}`,
                    (v) => `Cannot reach the backend: ${v.detail}`],
  'err.noDrives': ['没有找到可以扫的分区。这个工具需要 NTFS。',
                   'No scannable volume found. This tool needs NTFS.'],
  'err.notAdmin': [
    '不是管理员权限:读不了 MFT 和变更日志,只能退回目录遍历 —— 更慢,而且' +
    '看不到删除记录。用 strata.bat 启动会自动提权。',
    'Not running as administrator: the MFT and change journal are off limits, ' +
    'so this falls back to walking directories — slower, and deletions stay ' +
    'invisible. Launch via strata.bat to elevate automatically.',
  ],
  'err.whichDrive': ['是哪个盘扫的', 'which drive this came from'],
});

/* 清理规则的标签和建议。代号由后端给(analysis/hotspots.py 的 CLEANUP_RULES),
 * 措辞在这儿 —— 后端不知道人在看哪种语言,而且切语言时不会重新请求。
 * 键名固定是 clean.rule.<代号>.label / .advice,加规则时两条都要加。 */
Object.assign(STRINGS, {
  'clean.rule.winInstaller.label': ['Windows 安装缓存', 'Windows installer cache'],
  'clean.rule.winInstaller.advice': [
    '卸载和修复程序时用到,删了某些软件无法卸载',
    'Used to uninstall and repair programs; deleting it can leave software unremovable',
  ],
  'clean.rule.winSxs.label': ['组件存储', 'Component store'],
  'clean.rule.winSxs.advice': [
    '系统组件的唯一副本,不能手删,只能用 DISM 清理',
    'The only copy of system components. Never delete by hand — clean it with DISM',
  ],
  'clean.rule.winUpdate.label': ['更新下载缓存', 'Update download cache'],
  'clean.rule.winUpdate.advice': [
    '已安装的更新包,可以删',
    'Update packages already installed. Safe to delete',
  ],
  'clean.rule.winTemp.label': ['系统临时文件', 'System temp files'],
  'clean.rule.winTemp.advice': ['可以删', 'Safe to delete'],
  'clean.rule.recycleBin.label': ['回收站', 'Recycle Bin'],
  'clean.rule.recycleBin.advice': [
    '确认里面没有要恢复的东西再清空',
    'Check for anything you still want back before emptying it',
  ],
  'clean.rule.userTemp.label': ['用户临时文件', 'User temp files'],
  'clean.rule.userTemp.advice': [
    '可以删,占用往往不小',
    'Safe to delete, and often surprisingly large',
  ],
  'clean.rule.storeApps.label': ['应用商店应用数据', 'Store app data'],
  'clean.rule.storeApps.advice': [
    '含应用的缓存和存档,逐个看',
    'Holds both caches and saved data. Go through it app by app',
  ],
  'clean.rule.thumbnails.label': ['缩略图缓存', 'Thumbnail cache'],
  'clean.rule.thumbnails.advice': [
    '可以删,会自动重建',
    'Safe to delete; it rebuilds itself',
  ],
  'clean.rule.pipCache.label': ['pip 缓存', 'pip cache'],
  'clean.rule.pipCache.advice': [
    '可以删,重装包时会重新下载',
    'Safe to delete; packages re-download when reinstalled',
  ],
  'clean.rule.npmCache.label': ['npm 缓存', 'npm cache'],
  'clean.rule.npmCache.advice': ['可以删', 'Safe to delete'],
  'clean.rule.yarnCache.label': ['yarn 缓存', 'yarn cache'],
  'clean.rule.yarnCache.advice': ['可以删', 'Safe to delete'],
  'clean.rule.toolCache.label': ['工具缓存目录', 'Tool cache directory'],
  'clean.rule.toolCache.advice': [
    '多数可以删,会自动重建',
    'Mostly safe to delete; tools rebuild what they need',
  ],
  'clean.rule.gradleCache.label': ['Gradle 缓存', 'Gradle cache'],
  'clean.rule.gradleCache.advice': [
    '可以删,下次构建重新下载',
    'Safe to delete; the next build re-downloads it',
  ],
  'clean.rule.mavenRepo.label': ['Maven 本地仓库', 'Maven local repository'],
  'clean.rule.mavenRepo.advice': [
    '可以删,下次构建重新下载',
    'Safe to delete; the next build re-downloads it',
  ],
  'clean.rule.nugetCache.label': ['NuGet 缓存', 'NuGet cache'],
  'clean.rule.nugetCache.advice': ['可以删', 'Safe to delete'],
  'clean.rule.nodeModules.label': ['Node 依赖', 'Node dependencies'],
  'clean.rule.nodeModules.advice': [
    '项目可重装依赖,长期不动的项目值得清',
    'Reinstallable per project — worth clearing for projects you have not touched in a while',
  ],
  'clean.rule.pycache.label': ['Python 字节码缓存', 'Python bytecode cache'],
  'clean.rule.pycache.advice': [
    '可以删,会自动重建',
    'Safe to delete; it rebuilds itself',
  ],
  'clean.rule.rustDebug.label': ['Rust 调试产物', 'Rust debug build output'],
  'clean.rule.rustDebug.advice': [
    '可以删,重新编译即可',
    'Safe to delete; just rebuild',
  ],
  'clean.rule.rustRelease.label': ['Rust 发布产物', 'Rust release build output'],
  'clean.rule.rustRelease.advice': [
    '可以删,重新编译即可',
    'Safe to delete; just rebuild',
  ],
  'clean.rule.steamPartial.label': ['Steam 下载暂存', 'Steam partial downloads'],
  'clean.rule.steamPartial.advice': [
    '中断的下载残留,可以删',
    'Leftovers from interrupted downloads. Safe to delete',
  ],
  'clean.rule.steamShaders.label': ['Steam 着色器缓存', 'Steam shader cache'],
  'clean.rule.steamShaders.advice': [
    '可以删,会自动重建',
    'Safe to delete; it rebuilds itself',
  ],
  'clean.rule.steamWorkshop.label': ['Steam 创意工坊', 'Steam Workshop'],
  'clean.rule.steamWorkshop.advice': [
    '订阅的模组内容,按需取舍',
    'Mod content you subscribed to. Keep what you still play',
  ],
  'clean.rule.nvidiaDownload.label': ['NVIDIA 驱动下载缓存', 'NVIDIA driver download cache'],
  'clean.rule.nvidiaDownload.advice': ['可以删', 'Safe to delete'],
  'clean.rule.crashDumps.label': ['崩溃转储', 'Crash dumps'],
  'clean.rule.crashDumps.advice': [
    '排查过就可以删',
    'Safe to delete once you are done investigating',
  ],
  'clean.rule.miniDumps.label': ['蓝屏小转储', 'Blue-screen minidumps'],
  'clean.rule.miniDumps.advice': [
    '排查过就可以删',
    'Safe to delete once you are done investigating',
  ],
  'clean.rule.hiberfil.label': ['休眠文件', 'Hibernation file'],
  'clean.rule.hiberfil.advice': [
    '关掉休眠可释放,等同于内存大小',
    'Turning off hibernation frees it. Same size as your RAM',
  ],
  'clean.rule.pagefile.label': ['虚拟内存页面文件', 'Page file'],
  'clean.rule.pagefile.advice': [
    '由系统管理,不要手删',
    'Managed by Windows. Do not delete it by hand',
  ],
  'clean.rule.swapfile.label': ['交换文件', 'Swap file'],
  'clean.rule.swapfile.advice': [
    '由系统管理,不要手删',
    'Managed by Windows. Do not delete it by hand',
  ],
});

const LOCALES = ['zh-CN', 'en-US'];
const HTML_LANG = ['zh-CN', 'en'];
const STORE_KEY = 'strata.lang';

/* 默认跟浏览器走,但记住用户的选择。
 *
 * 只认 zh 前缀 —— zh-CN / zh-TW / zh-HK 都给中文。其他一切给英文:这个工具
 * 只有这两种文案,把 ja 也塞给中文没道理。 */
function detect() {
  let saved = null;
  try {
    saved = window.localStorage.getItem(STORE_KEY);
  } catch (err) {
    saved = null;            // 隐私模式下 localStorage 会抛,不能让它拦住启动
  }
  if (saved === 'zh' || saved === 'en') return saved;
  const nav = (navigator.language || '').toLowerCase();
  return nav.startsWith('zh') ? 'zh' : 'en';
}

let current = detect();
const listeners = [];

function index() { return current === 'en' ? EN : ZH; }

/* 取文案。key 不认识就把 key 本身还回去 —— 页面上会显示
 * 「tl.title」这种东西,难看,但比空白好找。 */
function t(key, vars) {
  const pair = STRINGS[key];
  if (!pair) {
    if (window.console) console.warn('[i18n] 没有这个键:' + key);
    return key;
  }
  const value = pair[index()] !== undefined && pair[index()] !== ''
    ? pair[index()] : pair[ZH];       // 英文缺了退回中文,不留空
  return typeof value === 'function' ? value(vars || {}) : value;
}

/* 把 DOM 里带 data-i18n 的地方填上。
 *
 * 属性也要翻:title 和 aria-label 是读屏软件和悬停提示看的,漏了等于这部分
 * 用户还在看中文。 */
function apply(root) {
  const scope = root || document;
  scope.querySelectorAll('[data-i18n]').forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  scope.querySelectorAll('[data-i18n-title]').forEach((node) => {
    node.setAttribute('title', t(node.dataset.i18nTitle));
  });
  scope.querySelectorAll('[data-i18n-label]').forEach((node) => {
    node.setAttribute('aria-label', t(node.dataset.i18nLabel));
  });
  if (scope === document) {
    document.documentElement.lang = HTML_LANG[index()];
    document.title = t('app.title');
  }
}

function set(next) {
  if (next !== 'zh' && next !== 'en') return;
  if (next === current) return;
  current = next;
  try {
    window.localStorage.setItem(STORE_KEY, next);
  } catch (err) {
    /* 存不下就只在本次会话生效,不影响切换 */
  }
  apply();
  listeners.forEach((fn) => fn(next));
}

function onChange(fn) { listeners.push(fn); }

window.I18N = {
  get lang() { return current; },
  get locale() { return LOCALES[index()]; },
  keys() { return Object.keys(STRINGS); },
  raw(key) { return STRINGS[key]; },
  t, apply, set, onChange,
};
window.t = t;
