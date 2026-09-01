/* Strata 前端。零依赖:树图和时间轴都是手写的。
 *
 * 核心主张:面积表示大小,颜色表示年龄。所以每个可视化都要同时编码这两个量。
 */

'use strict';

// ---- 状态 ----
const S = {
  drive: null,
  drives: [],
  status: null,
  timeline: null,
  tree: null,
  hotspots: null,
  /* 这两块原来是拿到就画、不留底 —— 切语言时没有数据可以重画,那两段
   * 会留在旧语言上,一屏里中英混着。所以存下来。 */
  diff: null,
  changes: null,
  scan: null,          // 扫描进度,setScanState 填
  schedule: null,      // 计划任务状态,loadSchedule 填
  banner: null,        // 当前横幅,见 banner()
  path: '',
  ageFilter: null,     // null = 全部
  fade: new Map(),     // path -> 当前透明度,用于补间
  anim: null,
  scanPoll: null,
  scanPollErrors: 0,   // 连续取状态失败的次数,见 SCAN_POLL_MAX_ERRORS
  /* 时间轴的可见窗口:[起, 止) 在 days 数组里的下标。null = 全部。
   * 缩放不是把画面放大 —— 那样只会把字一起拉糊。这里是少画几天、每天
   * 画宽一点,纵轴按窗口内的峰值重算。
   *
   * 注意它只解决横向拥挤。纵向被压平要靠 tlLog:窗口缩到 14 天时,只要
   * 那个 7 GB 的日子还在窗口里,60 MB 的那天照样只有 1 px。 */
  tlView: null,
  tlLog: false,        // 纵轴取对数,见 makeYAxis
  tlAnimated: false,   // 首次渲染才播生长动画,缩放/平移时不播
};

// 时间轴最少显示几天。再少下去纵轴刻度就没有参考意义了。
const TL_MIN_DAYS = 5;

/* 对数纵轴的起压点:1 MB 以下不再细分。
 *
 * 纯 log 在 0 处发散,而「这天没变化」是最常见的一格,必须能画。所以用
 * symlog:log10(1 + |v| / K)。K 取 1 MB —— 比它小的变化本来就不值得占
 * 一格高度,压在零线附近正好。 */
const TL_LOG_K = 1024 * 1024;

/* 年龄色阶,和 CSS 里的 --age-* 对齐。
 *
 * 存的是文案的键,不是文案本身。这是模块级常量,只在加载时求值一次 ——
 * 存成文字的话,切换语言之后色带上还是旧语言,而其他地方都变了。
 * 用 band.label 这个 getter 读,每次现取。 */
const AGE_BANDS = [
  { key: 'today',   max: 1,    color: '#ff5c39' },
  { key: 'week',    max: 7,    color: '#ff9e2c' },
  { key: 'month',   max: 30,   color: '#e8c46a' },
  { key: 'quarter', max: 90,   color: '#8fbf9f' },
  { key: 'year',    max: 365,  color: '#4e93a8' },
  { key: 'older',   max: null, color: '#2f5d72' },
].map((b) => Object.defineProperty(b, 'label', {
  get() { return t('age.' + this.key); },
  enumerable: true,
}));

const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// ---- 工具 ----
function fmtBytes(n, digits) {
  if (n === null || n === undefined) return '—';
  const neg = n < 0;
  let v = Math.abs(n);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  const d = digits === undefined ? (v < 10 && i > 0 ? 1 : 0) : digits;
  return (neg ? '−' : '') + v.toFixed(d) + ' ' + units[i];
}

function fmtSigned(n) {
  if (!n) return '0';
  return (n > 0 ? '+' : '−') + fmtBytes(Math.abs(n));
}

function fmtCount(n) {
  return (n === null || n === undefined) ? '—' : n.toLocaleString(I18N.locale);
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, '0');
  // 年月日时分用 ISO 顺序,两种语言都不变:2026-08-28 19:32 没有歧义,
  // 而 08/28 还是 28/08 得看读者是哪儿的人。
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* 只有月日的短格式。这个必须分语言 ——「01-05」中文读者看是 1 月 5 日,
 * 英文读者一半会读成 5 月 1 日。英文给月份缩写,歧义就没了。 */
function fmtDay(ts) {
  if (!ts) return '—';
  return shortDay(new Date(ts * 1000));
}

/* 后端给的是 'YYYY-MM-DD' 字符串,时间轴横轴用它。
 * 按 UTC 拆字符串,不走 new Date(str) —— 那个会按本地时区偏移,
 * 东八区能把 8 月 28 日显示成 8 月 27 日。 */
function fmtDayISO(iso) {
  if (!iso) return '—';
  if (I18N.lang !== 'en') return iso.slice(5);
  const [y, m, d] = iso.split('-').map(Number);
  if (!y || !m || !d) return iso.slice(5);
  return shortDay(new Date(y, m - 1, d));
}

function shortDay(date) {
  const p = (x) => String(x).padStart(2, '0');
  if (I18N.lang === 'en') {
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  }
  return `${p(date.getMonth() + 1)}-${p(date.getDate())}`;
}

function daysAgo(ts) {
  if (!ts) return null;
  return (Date.now() / 1000 - ts) / 86400;
}

function ageBand(ts) {
  const d = daysAgo(ts);
  if (d === null) return AGE_BANDS[AGE_BANDS.length - 1];
  for (const band of AGE_BANDS) {
    if (band.max === null || d <= band.max) return band;
  }
  return AGE_BANDS[AGE_BANDS.length - 1];
}

function ageText(ts) {
  const d = daysAgo(ts);
  if (d === null) return t('age.unknown');
  if (d < 1) return t('age.today');
  if (d < 2) return t('age.yesterday');
  // 英文要区分单复数,所以把数字交给文案函数,别在这儿拼字符串
  if (d < 30) return t('age.daysAgo', { n: Math.round(d) });
  if (d < 365) return t('age.monthsAgo', { n: Math.round(d / 30) });
  return t('age.yearsAgo', { n: (d / 365).toFixed(1) });
}

function el(id) { return document.getElementById(id); }

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function tag(name, attrs, text) {
  const node = document.createElement(name);
  if (attrs) for (const k in attrs) {
    if (k === 'class') node.className = attrs[k];
    else node.setAttribute(k, attrs[k]);
  }
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

async function api(path, params) {
  const url = new URL(path, location.origin);
  if (params) for (const k in params) {
    if (params[k] !== null && params[k] !== undefined) url.searchParams.set(k, params[k]);
  }
  const res = await fetch(url, { headers: { 'Accept': 'application/json' } });
  const body = await res.json().catch(() => ({ error: t('err.notJson') }));
  if (!res.ok) throw new Error(body.error || t('err.request', { status: res.status }));
  return body;
}

async function post(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || body.message || t('err.request', { status: res.status }));
  return body;
}

// ---- 顶栏 ----
function renderDriveTabs() {
  const box = el('driveTabs');
  clear(box);
  for (const d of S.drives) {
    const btn = tag('button', {
      class: 'drive-tab',
      role: 'tab',
      'aria-selected': d.drive === S.drive ? 'true' : 'false',
      'data-absent': d.present ? '0' : '1',
      title: d.present ? '' : t('base.offline'),
    }, d.drive);
    btn.addEventListener('click', () => selectDrive(d.drive));
    box.appendChild(btn);
  }
}

function renderBaseline() {
  const box = el('baseline');
  clear(box);
  const d = S.drives.find((x) => x.drive === S.drive);
  if (!d) return;

  const snap = d.latest_snapshot;
  const parts = [];

  if (d.live_total_bytes) {
    const usedPct = (d.live_used_bytes / d.live_total_bytes * 100).toFixed(1);
    parts.push([t('base.used'), `${fmtBytes(d.live_used_bytes)} / ${fmtBytes(d.live_total_bytes)}`, `${usedPct}%`]);
    parts.push([t('base.free'), fmtBytes(d.live_free_bytes), null]);
  }
  if (snap) {
    parts.push([t('base.scanned'), fmtBytes(snap.scanned_bytes), snap.method === 'mft' ? 'MFT' : t('base.walkMode')]);
    parts.push([t('base.files'), fmtCount(snap.file_count), null]);
    parts.push([t('base.snapshots'), t('base.snapshotCount', { n: fmtCount(d.snapshot_count) }), null]);
  }

  for (const [label, value, extra] of parts) {
    const span = tag('span');
    span.appendChild(tag('span', { class: 'dim' }, label + ' '));
    span.appendChild(tag('b', null, value));
    if (extra) span.appendChild(tag('span', { class: 'dim' }, ' ' + extra));
    box.appendChild(span);
  }

  const asOf = tag('span', { style: 'margin-left:auto' });
  asOf.appendChild(tag('span', { class: 'dim' }, snap ? t('base.asOf') : ''));
  asOf.appendChild(tag('b', null, snap ? fmtTime(snap.taken_at) : t('base.noSnapshot')));
  box.appendChild(asOf);

  // 快照自带的口径说明。退回目录遍历时,上面那几个数字的含义变了(硬链接重复
  // 计数、算的是逻辑大小),这条得跟着数字一直在,而不是只在扫描那一刻闪一下。
  // 不做成可关闭的横幅:它描述的是当前这份数据本身,不是一次事件。
  const noteBox = el('baselineNote');
  if (noteBox) {
    clear(noteBox);
    if (snap && snap.note) {
      noteBox.appendChild(tag('span', null, snap.note));
      noteBox.hidden = false;
    } else {
      noteBox.hidden = true;
    }
  }
}

// ---- 年龄图例 ----
function renderLegend() {
  const box = el('ageLegend');
  clear(box);
  const profile = S.hotspots ? S.hotspots.age_profile : [];
  const byKey = {};
  for (const p of profile) byKey[p.key] = p;

  box.classList.toggle('has-filter', S.ageFilter !== null);

  for (const band of AGE_BANDS) {
    const info = byKey[band.key];
    const btn = tag('button', {
      class: 'legend-item',
      style: `--swatch:${band.color}`,
      'aria-pressed': S.ageFilter === band.key ? 'true' : 'false',
      title: t('tm.onlyBand', { band: band.label }),
    });
    btn.appendChild(tag('span', { class: 'legend-label' }, band.label));
    btn.appendChild(tag('span', { class: 'legend-value' },
      info ? fmtBytes(info.bytes) : '—'));
    btn.addEventListener('click', () => {
      S.ageFilter = S.ageFilter === band.key ? null : band.key;
      renderLegend();
      drawTreemap(true);
    });
    box.appendChild(btn);
  }
}

// ---- 树图 ----
/* squarify:Bruls/Huizing/van Wijk 的算法。目的是让每块尽量接近正方形,
 * 因为细长条的面积人眼判断不准,而面积就是这张图要传达的信息。 */
function squarify(items, x, y, w, h) {
  const out = [];
  const total = items.reduce((s, it) => s + it.value, 0);
  if (total <= 0 || w <= 0 || h <= 0) return out;

  let queue = items.slice();
  let scale = (w * h) / total;

  function worst(row, side) {
    const sum = row.reduce((s, it) => s + it.value, 0) * scale;
    if (sum <= 0) return Infinity;
    let lo = Infinity, hi = 0;
    for (const it of row) {
      const a = it.value * scale;
      if (a < lo) lo = a;
      if (a > hi) hi = a;
    }
    const s2 = sum * sum, side2 = side * side;
    return Math.max((side2 * hi) / s2, s2 / (side2 * lo));
  }

  while (queue.length) {
    const horizontal = w >= h;      // 短边决定沿哪个方向铺
    const side = horizontal ? h : w;
    const row = [queue.shift()];
    while (queue.length) {
      const next = row.concat([queue[0]]);
      if (worst(next, side) <= worst(row, side)) row.push(queue.shift());
      else break;
    }

    const rowSum = row.reduce((s, it) => s + it.value, 0) * scale;
    const thickness = side > 0 ? rowSum / side : 0;
    let off = 0;
    for (const it of row) {
      const a = it.value * scale;
      const len = thickness > 0 ? a / thickness : 0;
      if (horizontal) {
        out.push({ item: it, x: x, y: y + off, w: thickness, h: len });
      } else {
        out.push({ item: it, x: x + off, y: y, w: len, h: thickness });
      }
      off += len;
    }
    if (horizontal) { x += thickness; w -= thickness; }
    else { y += thickness; h -= thickness; }
    if (w <= 0.5 || h <= 0.5) break;
  }
  return out;
}

let tmLayout = [];      // 命中测试用
let tmHover = null;

function treeItems() {
  if (!S.tree) return [];
  const raw = (S.tree.children || []).filter((c) => c.bytes > 0);
  const items = raw.map((c) => ({
    value: c.bytes,
    name: c.name,
    path: c.path,
    bytes: c.bytes,
    ownBytes: c.own_bytes,
    files: c.files,
    dirs: c.dirs,
    ctime: c.newest_ctime,
    // dirs 有值说明是目录;文件行的 dirs 是 0 且 files 是 0
    isDir: (c.dirs || 0) > 0 || (c.files || 0) > 0,
    band: ageBand(c.newest_ctime),
  }));

  // 补一块「本层直属文件 + 被折叠的小目录」。
  //
  // 上面那些格子只是子目录,本层总量(node.bytes)还含两部分它们盖不住的:
  // 直属在本目录下的文件(own_bytes),以及扫描时被裁掉的小目录(folded_bytes,
  // 后端一直在给,前端从来没画)。不补这一块,树图铺满的面积就小于它自己写的
  // 总数 —— 盘根最明显:pagefile.sys 和 hiberfil.sys 直挂根下,真机上是个
  // 20 GB 的洞。
  //
  // 没有路径,所以不可点、不可进、右键也不给"在资源管理器中显示"。
  const n = S.tree.node;
  if (n) {
    const rest = (n.own_bytes || 0) + (n.folded_bytes || 0);
    if (rest > 0) {
      items.push({
        value: rest,
        // 哨兵而不是 null:tmHover 初值就是 null,而画的时候按
        // `tmHover === it.path` 判高亮 —— 用 null 的话这一块在鼠标还没进画布
        // 时就自带白框。真实相对路径不可能含 \x00,不会撞。
        path: '\x00rest',
        name: t('tm.restOfLevel'),
        bytes: rest,
        files: null,
        dirs: 0,
        ctime: null,
        isDir: false,
        synthetic: true,
        foldedChildren: n.folded_children || 0,
        band: ageBand(null),
      });
    }
  }
  return items;
}

function treeTotal() {
  if (!S.tree) return 0;
  if (S.tree.node && S.tree.node.bytes) return S.tree.node.bytes;
  return (S.tree.children || []).reduce((s, c) => s + (c.bytes || 0), 0);
}

function targetOpacity(it) {
  if (S.ageFilter === null) return 1;
  return it.band.key === S.ageFilter ? 1 : 0.12;
}

function drawTreemap(animate) {
  const canvas = el('treemap');
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const shell = canvas.parentElement;
  const W = Math.floor(shell.clientWidth);
  const H = Math.floor(shell.clientHeight);

  /* 量到 0 就先不画。页面在后台标签页里加载时布局可能还没算出来,
   * 这时候画一次会把 canvas 永久钉在兜底尺寸上 —— ResizeObserver
   * 会在真拿到尺寸后再叫我们一次。 */
  if (W < 2 || H < 2) return;

  // canvas 的位图尺寸按 dpr 放大,CSS 尺寸交给样式表,别写内联值
  if (canvas.width !== W * dpr || canvas.height !== H * dpr) {
    canvas.width = W * dpr;
    canvas.height = H * dpr;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const items = treeItems();
  tmLayout = squarify(items, 0, 0, W, H);

  // 补间只在筛选切换时跑,平时直接画
  const now = performance.now();
  if (animate && !REDUCED) {
    if (S.anim) cancelAnimationFrame(S.anim);
    const from = new Map(S.fade);
    const start = now;
    const step = (t) => {
      const k = Math.min(1, (t - start) / 200);
      const e = 1 - Math.pow(1 - k, 3);
      for (const cell of tmLayout) {
        const a = from.has(cell.item.path) ? from.get(cell.item.path) : 1;
        const b = targetOpacity(cell.item);
        S.fade.set(cell.item.path, a + (b - a) * e);
      }
      paintTreemap(ctx, W, H);
      if (k < 1) S.anim = requestAnimationFrame(step);
      else S.anim = null;
    };
    S.anim = requestAnimationFrame(step);
  } else {
    for (const cell of tmLayout) S.fade.set(cell.item.path, targetOpacity(cell.item));
    paintTreemap(ctx, W, H);
  }
}

function paintTreemap(ctx, W, H) {
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#0b1416';
  ctx.fillRect(0, 0, W, H);

  if (!tmLayout.length) {
    ctx.fillStyle = '#47646a';
    ctx.font = '13px "Segoe UI", "Microsoft YaHei UI", sans-serif';
    ctx.textAlign = 'center';
    // 只有一句话可说,因为树图永远画最新快照(enterPath 不传 snapshot,
    // 而 demote_previous_snapshots 从不降级最新的那个)。想给降级过的快照
    // 换一句「明细已精简」得先有办法让树图指向旧快照 —— 见 api.py 里
    // get_tree 那段。没有那个入口的话,那句话永远不会被显示。
    ctx.fillText(t('tm.empty'), W / 2, H / 2);
    ctx.textAlign = 'left';
    return;
  }

  for (const cell of tmLayout) {
    const it = cell.item;
    const alpha = S.fade.has(it.path) ? S.fade.get(it.path) : 1;
    const x = cell.x, y = cell.y;
    const w = Math.max(0, cell.w - 1), h = Math.max(0, cell.h - 1);
    if (w < 0.6 || h < 0.6) continue;

    ctx.globalAlpha = alpha;
    ctx.fillStyle = it.band.color;
    ctx.fillRect(x, y, w, h);

    // 顶部一道亮边,让相邻同色块能分开
    ctx.globalAlpha = alpha * 0.35;
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(x, y, w, 1);

    if (tmHover === it.path) {
      ctx.globalAlpha = 1;
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 2;
      ctx.strokeRect(x + 1, y + 1, w - 2, h - 2);
    }

    // 标签:块够大才写,不然是噪音
    if (w > 54 && h > 22 && alpha > 0.4) {
      ctx.globalAlpha = alpha;
      ctx.fillStyle = 'rgba(6,12,13,0.82)';
      ctx.font = '600 12px "Segoe UI", "Microsoft YaHei UI", sans-serif';
      const name = clipText(ctx, it.name, w - 10);
      ctx.fillText(name, x + 5, y + 14);
      if (h > 36) {
        ctx.globalAlpha = alpha * 0.72;
        ctx.font = '11px "Cascadia Mono", "Consolas", monospace';
        ctx.fillText(clipText(ctx, fmtBytes(it.bytes), w - 10), x + 5, y + 27);
      }
    }
  }
  ctx.globalAlpha = 1;
}

function clipText(ctx, text, maxW) {
  if (ctx.measureText(text).width <= maxW) return text;
  let s = text;
  while (s.length > 1 && ctx.measureText(s + '…').width > maxW) s = s.slice(0, -1);
  return s + '…';
}

function hitTest(px, py) {
  for (const cell of tmLayout) {
    if (px >= cell.x && px <= cell.x + cell.w && py >= cell.y && py <= cell.y + cell.h) {
      if (S.ageFilter !== null && cell.item.band.key !== S.ageFilter) return null;
      return cell;
    }
  }
  return null;
}

/* 这一格指向盘上的哪个东西。指不着就给 null。
 *
 * 「本层文件」那一块是 treeItems() 补出来的,没有对应路径 —— 点它、右键它都
 * 得当成点在空白上。不然右键菜单里的「在资源管理器中显示」会拿着哨兵路径
 * ('\x00rest')去请求后端。后端认得这是非法路径会挡掉(reveal.py 不收含 NUL
 * 的路径),但让用户点出一个必然失败的菜单项没有意义。 */
function cellTarget(cell) {
  if (!cell || cell.item.synthetic) return null;
  return cell.item;
}

function bindTreemap() {
  const canvas = el('treemap');
  const tip = el('tmTip');

  canvas.addEventListener('mousemove', (ev) => {
    const r = canvas.getBoundingClientRect();
    const cell = hitTest(ev.clientX - r.left, ev.clientY - r.top);
    const path = cell ? cell.item.path : null;
    if (path !== tmHover) {
      tmHover = path;
      const ctx = canvas.getContext('2d');
      paintTreemap(ctx, r.width, r.height);
    }
    if (!cell) { hideTip(tip); canvas.style.cursor = 'default'; return; }

    const it = cell.item;
    const total = treeTotal();
    const share = total ? (it.bytes / total * 100).toFixed(1) + '%' : '';
    clear(tip);

    // 「本层其余」那一块:没有路径,也没有年龄可言,提示只说它是什么
    if (it.synthetic) {
      tip.appendChild(tag('div', { class: 'p' }, it.name));
      const m = tag('div', { class: 'm' });
      m.appendChild(tag('b', null, fmtBytes(it.bytes)));
      if (share) m.appendChild(document.createTextNode(t('tm.shareOfLevel', { share: share })));
      tip.appendChild(m);
      tip.appendChild(tag('div', { class: 'm hint' },
        it.foldedChildren
          ? t('tm.restWithFolded', { n: fmtCount(it.foldedChildren) })
          : t('tm.restHint')));
      showTip(tip, ev);
      canvas.style.cursor = 'default';
      return;
    }

    tip.appendChild(tag('div', { class: 'p' }, it.path || it.name));
    const meta = tag('div', { class: 'm' });
    meta.appendChild(tag('b', null, fmtBytes(it.bytes)));
    if (share) meta.appendChild(document.createTextNode(t('tm.shareOfLevel', { share: share })));
    if (it.files) meta.appendChild(document.createTextNode('  ' + t('grid.fileCount', { n: fmtCount(it.files) })));
    tip.appendChild(meta);
    const age = tag('div', { class: 'm' });
    age.appendChild(document.createTextNode(t('tm.lastWrite')));
    age.appendChild(tag('b', null, ageText(it.ctime)));
    if (it.ctime) age.appendChild(document.createTextNode('  ' + fmtTime(it.ctime)));
    tip.appendChild(age);
    if (it.isDir) tip.appendChild(tag('div', { class: 'm hint' }, t('tm.clickEnter')));
    showTip(tip, ev);
    canvas.style.cursor = it.isDir ? 'pointer' : 'default';
  });

  canvas.addEventListener('mouseleave', () => {
    hideTip(el('tmTip'));
    if (tmHover !== null) {
      tmHover = null;
      const r = canvas.getBoundingClientRect();
      paintTreemap(canvas.getContext('2d'), r.width, r.height);
    }
  });

  canvas.addEventListener('click', (ev) => {
    const r = canvas.getBoundingClientRect();
    const it = cellTarget(hitTest(ev.clientX - r.left, ev.clientY - r.top));
    if (it && it.isDir) enterPath(it.path);
  });

  canvas.addEventListener('contextmenu', (ev) => {
    const r = canvas.getBoundingClientRect();
    const it = cellTarget(hitTest(ev.clientX - r.left, ev.clientY - r.top));
    // 空白处右键当成对当前这一层操作,右键总有反应
    hideTip(el('tmTip'));
    if (it) showCtx(it.path, it.isDir, ev);
    else showCtx(S.path, true, ev);
  });

  /* 用 ResizeObserver 而不是 window.resize:容器尺寸变化的原因不止窗口缩放
   * (后台标签页首次布局、字体加载后回流、横幅出现挤动布局),
   * 而首绘量到 0 之后必须有人再叫一次,不然树图就一直是空的。
   *
   * 这一次重叫不能排在 requestAnimationFrame 上:页面隐藏时 rAF 完全冻结
   * (实测 hidden 状态下 600ms 都没跑,setTimeout 跑了),而「首绘量到 0」恰好
   * 就发生在隐藏的时候。本机上实测过后果:面板在后台加载,画布位图停在默认的
   * 300,CSS 宽 1207 —— 一个像素都没画。setTimeout 合并连续 resize 一样管用,
   * 而且不冻结。 */
  let pending = 0;
  const ro = new ResizeObserver(() => {
    if (pending) return;
    pending = setTimeout(() => { pending = 0; drawTreemap(false); }, RESIZE_COALESCE_MS);
  });
  ro.observe(canvas.parentElement);
}

/* tip 是 .timeline-shell / .treemap-shell 里的绝对定位元素,
 * 所以坐标要换算成相对容器的偏移,不能直接用 clientX。 */
function placeTip(tip, ev) {
  const pad = 14;
  const host = tip.parentElement.getBoundingClientRect();
  const w = tip.offsetWidth || 240;
  const h = tip.offsetHeight || 80;
  let x = ev.clientX - host.left + pad;
  let y = ev.clientY - host.top + pad;
  if (ev.clientX + pad + w > window.innerWidth - 8) x = ev.clientX - host.left - w - pad;
  if (ev.clientY + pad + h > window.innerHeight - 8) y = ev.clientY - host.top - h - pad;
  tip.style.left = Math.max(0, x) + 'px';
  tip.style.top = Math.max(0, y) + 'px';
}

function showTip(tip, ev) {
  tip.hidden = false;
  placeTip(tip, ev);
  tip.classList.add('on');
}

function hideTip(tip) {
  tip.classList.remove('on');
  tip.hidden = true;
}

/* 把一个盘内相对路径摊成面包屑的各级。纯函数,不碰 DOM —— tests/test_crumbs.py
 * 直接在 node 里跑它。
 *
 * 每级的 path 必须是后端认的那种:反斜杠、不含盘符、根目录空串
 * (schema.sql:31)。原来这里的累加器是 `acc = S.drive + '\\'` 起头、
 * 分隔符后置,于是父级发出去的是 `C:\Users\` —— 盘符和尾部反斜杠都多了。
 * get_dir() 查不到这个 key,而 get_tree() 对查不到的路径不报 404,
 * 返回 node=null、children=[]:前端看到的是一次成功的空响应,画出来是
 * 空图,表头还留着上一层的数字。表现就是「返回上级显示未扫描,只能回到
 * 最上级才好」—— 根那一级发的是空串,是唯一合口径的一级。
 *
 * 现在只用 parts.slice(0, i+1).join('\\'),没有累加器可以带脏东西。
 * 入参也顺手归一化:S.path 的来源不止一处(点方块、右键菜单、面包屑自己),
 * 万一哪天有人喂进来一个全路径,在这里掐掉而不是发给后端。 */
function crumbTrail(cur) {
  const rel = String(cur || '').replace(/^[A-Za-z]:/, '').replace(/\//g, '\\');
  const parts = rel.split('\\').filter(Boolean);
  const trail = [{ label: null, path: '', current: parts.length === 0 }];
  parts.forEach((p, i) => {
    trail.push({
      label: p,
      path: parts.slice(0, i + 1).join('\\'),
      current: i === parts.length - 1,
    });
  });
  return trail;
}

function renderCrumbs() {
  const box = el('crumbs');
  clear(box);
  if (!S.drive) return;          // 还没选盘,别印出「null\」

  const trail = crumbTrail(S.path);
  trail.forEach((c, i) => {
    if (i) box.appendChild(tag('span', { class: 'crumb-sep' }, '›'));
    // 根那一级没有名字,显示盘符
    const label = c.label === null ? S.drive + '\\' : c.label;
    const btn = tag('button', { class: c.current ? 'crumb-current' : 'crumb' }, label);
    if (!c.current) btn.addEventListener('click', () => enterPath(c.path));
    box.appendChild(btn);
  });

  if (trail.length === 1) {
    box.appendChild(tag('span', { class: 'crumb-sep dim' }, t('tm.crumbHint')));
  }
}

// ---- 时间轴 ----
const SVG_NS = 'http://www.w3.org/2000/svg';

function svg(name, attrs) {
  const node = document.createElementNS(SVG_NS, name);
  if (attrs) for (const k in attrs) node.setAttribute(k, attrs[k]);
  return node;
}

/* 把可见窗口夹到合法范围内。
 *
 * 单独一个函数是因为缩放、平移、键盘、按钮都要用它,各自夹一遍必然有一处漏。
 * 传 null 或者窗口已经覆盖全部时返回 null,统一表示「看全部」。 */
function clampView(view, total) {
  if (!view) return null;
  let span = Math.round(view.span);
  span = Math.max(Math.min(TL_MIN_DAYS, total), Math.min(span, total));
  if (span >= total) return null;
  let start = Math.round(view.start);
  start = Math.max(0, Math.min(start, total - span));
  return { start, span };
}

/* 纵轴。线性和对数两套,统一给出 toPx 和刻度表。
 *
 * 为什么需要对数:实测数据里 90 天有一天涨了 11 GB,其他几十天是几十到
 * 几百 MB。线性轴下那 11 GB 占满半幅,60 MB 的那天只有 0.6 px —— 比一根
 * 头发还细,和「没变化」看不出区别。缩短时间窗口不解决这个问题,因为
 * 只要大的那天还在窗口里,比例就还是一百多倍。
 *
 * 所以纵轴也要能换档。对数下 60 MB 和 11 GB 差 3 个数量级,画出来差
 * 三格,两者都看得见。代价是柱子高度不再能直接目测相加,所以默认还是
 * 线性 —— 「一眼看出哪天最凶」比「小的那些也看得见」更常用。 */
function makeYAxis(peak, log, half) {
  if (!log) {
    return {
      toPx: (v) => (v / peak) * half,
      ticks: [1, 0.5, 0, -0.5, -1].map((frac) => ({
        value: frac,
        px: frac * half,
        label: frac === 0 ? '0' : (frac > 0 ? '+' : '−') + fmtBytes(peak * Math.abs(frac)),
      })),
    };
  }

  const top = Math.log10(1 + peak / TL_LOG_K);
  const toPx = (v) => {
    if (!v) return 0;
    const mag = Math.log10(1 + Math.abs(v) / TL_LOG_K) / top;
    return Math.max(0, mag) * half * Math.sign(v);
  };

  /* 刻度落在整数数量级上,而不是等分高度 —— 对数轴上等分高度会标出
   * 3.7 MB 这种数,读起来没有意义。
   *
   * 梯子写成定值而不是 K * 10^e:后者第四格是 1000 MB,而 fmtBytes 只在
   * ≥1024 时才进位,于是标成「+1000 MB」,旁边是「+11 GB」,像坏了。
   * 直接按 1024 的幂给出,每一格都能整整齐齐地进位。 */
  const M = TL_LOG_K, G = M * 1024;
  const ticks = [{ value: 0, px: 0, label: '0' }];
  /* 上下都要留白。刻度字号 10px,离零线或者峰值那行太近就会叠字。
   * 底下被挡掉的通常是 ±1 MB —— 它正好是压缩起点,本来也没什么可读的。 */
  const GAP = 11;
  for (const v of [M, 10 * M, 100 * M, G, 10 * G, 100 * G]) {
    if (v > peak) break;
    const px = toPx(v);
    if (px > half - GAP) break;
    if (px < GAP) continue;                // 贴着零线的跳过,别 break,上面还有
    for (const sign of [1, -1]) {
      ticks.push({ value: v * sign, px: px * sign,
                   label: (sign > 0 ? '+' : '−') + fmtBytes(v) });
    }
  }
  for (const sign of [1, -1]) {
    ticks.push({ value: peak * sign, px: half * sign,
                 label: (sign > 0 ? '+' : '−') + fmtBytes(peak) });
  }
  return { toPx, ticks };
}

/* ---- 时间轴的坐标系 ----------------------------------------------------------
 *
 * 一份几何,三个地方用:画图、算「鼠标落在哪一天」、算「拖过去多少天」。
 * 原来这三处各写一份,`W = 1000` 和 `{left: 64, right: 16}` 抄了三遍
 * (第三遍还是字面量 `((1000 - 64 - 16) / 1000)`)—— 改一处另两处静默错位,
 * 点击落到错的日子上、拖动速度和图对不上。
 *
 * viewBox 宽跟着宿主的实测像素宽走,也就是 1 单位 = 1 CSS 像素。
 * 原来是死写的 `0 0 1000 260` 配 `preserveAspectRatio: none`,整张图靠 CSS
 * 缩放填满容器 —— 于是图里所有东西都按 宿主宽/1000 缩,不只是字:3px 的窗口
 * 指示轨、刻度线、柱子间距,全都一起变小。浏览器实测:
 *
 *     窗口 1265px → 宿主 1209px → 日期标签 12  px 高
 *     窗口  820px → 宿主  749px → 日期标签 8.7 px 高
 *     窗口  375px → 宿主  319px → 日期标签 3.3 px 高、9.5 px 宽,整图剩 83px 高
 *
 * app.css 里有 `@media (max-width: 940px)` 把版面收成单列,窄屏是特意支持过的。
 */
const TL_M = { top: 18, right: 16, bottom: 26, left: 64 };
const TL_H = 260;
/* 宿主再窄也按这个宽度画。绘图区宽度必须是正的 —— 负宽度的 rect 在 SVG 里
 * 什么都不画,整张图会变成空白而不是「小」。 */
const TL_MIN_W = 240;

/* 合并连续 resize 的等待时间。跟一帧同量级,肉眼看不出延迟。
 * 起个名字是因为它跟留白常量长得一样(都是 16),而结构测试会盯着
 * bindTimelineZoom 里的字面量 —— 那是为了防止几何常量被再抄一遍。 */
const RESIZE_COALESCE_MS = 16;

/* 给定宿主的 CSS 像素宽,返回这一次要用的坐标系。hostW 由调用方量,
 * 这个函数本身不碰 DOM(所以能在 node 里直接测)。 */
function tlGeom(hostW) {
  const W = Math.max(TL_MIN_W, Math.floor(hostW) || TL_MIN_W);
  return {
    W, H: TL_H, M: TL_M,
    plotW: W - TL_M.left - TL_M.right,
    plotH: TL_H - TL_M.top - TL_M.bottom,
  };
}

/* 每隔几天标一个日期。
 *
 * 按「放得下几个」算,不是死写 12 个。原来是 `ceil(days / 12)`,跟宽度无关,
 * 理由是「viewBox 是死的 1000,窄窗口时字和间距一起缩,相对关系不变」——
 * 那个理由在 viewBox 跟着宽度走之后就不成立了:字不缩了,间距缩了,
 * 12 个标签会叠在一起。
 *
 * 标签宽按语言取:.tl-day 是 10px 等宽字,"08-25" 实测占 30 上下,
 * "Aug 25" 占 39 —— 英文更宽,阈值也得更大。
 *
 * 上限 12:宽窗口下把 90 个日期全标上是另一种看不清。 */
function tlLabelStride(plotW, dayCount, lang) {
  const labelW = lang === 'en' ? 44 : 38;
  const fits = Math.max(2, Math.floor(plotW / labelW));
  return Math.max(1, Math.ceil(dayCount / Math.min(12, fits)));
}

/* 屏幕上的 x(相对图左边缘的像素)落在哪一天,返回 days 里的下标。
 *
 * 和画柱子用的是同一份几何,这一点是这个函数存在的全部理由:原来这段算法
 * 抄在 bindTimelineZoom 的闭包里,自己写着 W = 1000,和 renderTimeline 各算
 * 一份 —— 两边一旦不一致,点击就落到错的日子上,而这种错没有任何报错,
 * 只是「点这根柱子弹出来的是隔壁那天」。
 *
 * hostPx 是宿主的真实像素宽,单独给:宿主窄到 TL_MIN_W 以下时 viewBox 被夹住,
 * 屏幕像素和 viewBox 单位之间就有个缩放系数,得先换算回来。
 *
 * 该满足的性质是往返一致:把某一天柱子的中心 x 喂回来,得回到那一天。
 * tests/test_timeline_geometry.py 里按这个性质测。 */
function tlDayAt(x, hostPx, from, to) {
  if (!(to > from) || !(hostPx > 0)) return null;
  const g = tlGeom(hostPx);
  const vx = (x / hostPx) * g.W;              // 屏幕像素 → viewBox 单位
  const frac = (vx - g.M.left) / g.plotW;
  /* floor 而不是 round。问的是「光标落在哪一格」,round 回答的是「离哪条格线
   * 最近」—— 每一格的右半边都会被算到下一天去。第 i 格的中心是 i + 0.5,
   * round 直接进到 i + 1,往返对不上。
   *
   * 这个 off-by-one 是原来就有的(只是没人往返验过):dayAt 只喂给滚轮缩放的
   * 锚点,光标在格子右半边时锚在隔壁那天,滚一下画面就偏一格。 */
  const idx = from + Math.floor(frac * (to - from));
  return Math.max(from, Math.min(to - 1, idx));
}

/* 第 i 天(相对 from 的偏移)那根柱子的中心,viewBox 单位。
 * renderTimeline 画柱子和 tlDayAt 反推都得用它,不然两边会飘。 */
function tlBarCenter(i, geom, dayCount) {
  const slot = geom.plotW / Math.max(1, dayCount);
  return geom.M.left + i * slot + slot / 2;
}

/* 当前可见的下标区间。没缩放时就是整个数组。 */
function tlWindow() {
  const total = (S.timeline && S.timeline.days) ? S.timeline.days.length : 0;
  const view = clampView(S.tlView, total);
  S.tlView = view;                       // 顺手把夹过的结果写回去
  return view ? [view.start, view.start + view.span] : [0, total];
}

/* 以 anchor(days 里的下标)为锚点缩放。
 *
 * 锚点很重要:滚轮缩放时鼠标底下那天要待在原地不动,不然每滚一格
 * 画面就跳一下,没法对着某一天看细节。 */
function tlZoom(factor, anchor) {
  const total = (S.timeline && S.timeline.days) ? S.timeline.days.length : 0;
  if (!total) return;
  const [from, to] = tlWindow();
  const span = to - from;
  const next = Math.max(Math.min(TL_MIN_DAYS, total),
                        Math.min(total, Math.round(span * factor)));
  if (next === span) return;

  // 锚点在窗口里的相对位置保持不变
  const at = (anchor === undefined || anchor === null)
    ? from + span / 2
    : Math.max(from, Math.min(anchor, to - 1));
  const frac = span > 0 ? (at - from) / span : 0.5;
  S.tlView = clampView({ start: Math.round(at - frac * next), span: next }, total);
  renderTimeline();
}

function tlPan(deltaDays) {
  const total = (S.timeline && S.timeline.days) ? S.timeline.days.length : 0;
  const [from, to] = tlWindow();
  const span = to - from;
  if (span >= total) return;             // 没缩放就没得平移
  S.tlView = clampView({ start: from + deltaDays, span }, total);
  renderTimeline();
}

function tlReset() {
  S.tlView = null;
  renderTimeline();
}

function renderTimeline() {
  const host = el('timeline');
  clear(host);
  const tl = S.timeline;
  const empty = el('tlEmpty');

  const shell = host.parentElement;
  if (!tl || !tl.days || !tl.days.length) {
    if (empty) empty.hidden = false;
    if (shell) shell.hidden = true;      // 空 SVG 会留一块死白,直接收起来
    renderTimelineSummary();
    renderTlZoom();
    return;
  }
  if (empty) empty.hidden = true;
  if (shell) shell.hidden = false;

  const allDays = tl.days;
  const [from, to] = tlWindow();
  const days = allDays.slice(from, to);
  // 缩放中才给抓手光标 —— 全景下拖不动,给了反而是骗人
  if (shell) shell.classList.toggle('pannable', to - from < allDays.length);
  // 生长动画只在第一次渲染时播。缩放和平移会连着触发好几次,
  // 每次都从零线长一遍的话画面一直在抖,反而看不清。
  const animate = !REDUCED && !S.tlAnimated;
  S.tlAnimated = true;

  /* 宽度量外壳,不量 svg 自己:svg 的渲染高度由 viewBox 宽高比推出来,而
   * viewBox 正是下面要设的 —— 量自己就是自己算自己。外壳是普通 div,宽度只由
   * CSS 决定。drawTreemap 量的也是 `canvas.parentElement`。
   *
   * 「量不到」和「很窄」是两件事,界不一样。TL_MIN_W(240)是给真的很窄的宿主
   * 兜底的;量到 0 说的是「布局还没算出来」,这时候按 240 画出去,就是拿一个
   * 凑合的数当真 —— 第一版就是这么错的:后台标签页里加载,量到 0 退到 240,
   * viewBox 写成 `0 0 240 260`,而外壳 CSS 宽 749px,于是整张图被放大 3.1 倍,
   * 日期标签渲染成 36px 高、91px 宽。方向和原来的 bug 相反,病根一样。
   *
   * 所以量不到就先不画,等 ResizeObserver 拿到真尺寸再来一次。摘要和缩放按钮
   * 照画:它们跟宽度无关,跳过会让按钮的禁用状态停在上一次。 */
  const hostW = shell ? shell.clientWidth : 0;
  if (hostW < 2) {
    renderTimelineSummary();
    renderTlZoom();
    return;
  }
  // 记下这次用的宽度,让 observer 能判断「宽度真变了没」
  const geom = tlGeom(hostW);
  const { W, H, M, plotW, plotH } = geom;
  S.tlW = W;

  host.setAttribute('viewBox', `0 0 ${W} ${H}`);
  /* 宽高和 viewBox 一比一,所以 none 和默认值在这儿是一回事。留着是因为
   * 万一哪天 CSS 给了个别的高度,这里假设的是拉伸而不是留黑边。 */
  host.setAttribute('preserveAspectRatio', 'none');

  /* 斜纹图案,两种 basis 各一份,颜色不同。
   *
   * 回溯层用斜纹、实测层用实心,这个区别不能只写在下面的说明文字里 ——
   * 回溯值是「那天写的、现在还在盘上」的量,删掉的东西它一概看不见,所以
   * 净减的日子它也只会画成正的;实测值是两次快照相减,含删除,是真的变化。
   * 混在一起画会让人把推断当成测量。 */
  const defs = svg('defs');
  for (const [id, color] of [['hatchGrow', 'var(--grow)'], ['hatchShrink', 'var(--shrink)']]) {
    const pat = svg('pattern', {
      id, width: 5, height: 5, patternUnits: 'userSpaceOnUse',
      patternTransform: 'rotate(45)',
    });
    pat.appendChild(svg('rect', { width: 5, height: 5, fill: color, 'fill-opacity': '0.16' }));
    pat.appendChild(svg('rect', { width: 1.6, height: 5, fill: color, 'fill-opacity': '0.9' }));
    defs.appendChild(pat);
  }
  host.appendChild(defs);

  let peak = 0;
  for (const d of days) peak = Math.max(peak, d.added || 0, d.removed || 0);
  if (peak <= 0) peak = 1;

  const yAxis = makeYAxis(peak, S.tlLog, plotH / 2);
  const scale = yAxis.toPx;
  const mid = M.top + plotH / 2;
  const slot = plotW / days.length;
  // 上限 46 而不是 22:放到只剩几天时,每格有 180 多宽,柱子还是 22
  // 的话中间全是空白,像图坏了。
  const barW = Math.max(1.5, Math.min(46, slot * 0.68));

  // 零线和刻度
  const gAxis = svg('g', { class: 'tl-axis' });
  for (const tick of yAxis.ticks) {
    const y = mid - tick.px;
    gAxis.appendChild(svg('line', {
      x1: M.left, x2: W - M.right, y1: y, y2: y,
      class: tick.value === 0 ? 'tl-zero' : 'tl-grid',
    }));
    const t = svg('text', { x: M.left - 8, y: y + 3.5, class: 'tl-tick', 'text-anchor': 'end' });
    t.textContent = tick.label;
    gAxis.appendChild(t);
  }
  host.appendChild(gAxis);

  // 实测起点:这一天之前都是回溯推断。
  // 在切片里找,不是在全量里找 —— 边界被划出窗口时就不该画这条线。
  const firstMeasured = tl.summary && tl.summary.first_measured_day;
  if (firstMeasured) {
    const idx = days.findIndex((d) => d.day === firstMeasured);
    if (idx > 0) {
      const x = M.left + idx * slot;
      const g = svg('g');
      g.appendChild(svg('line', {
        x1: x, x2: x, y1: M.top - 6, y2: M.top + plotH + 4, class: 'tl-boundary',
      }));
      // 变量别叫 t —— 全局的 t() 是取文案的,叫 t 就把它遮住了
      const label = svg('text', { x: x + 5, y: M.top - 8, class: 'tl-boundary-label' });
      label.textContent = t('tl.measuredStart');
      g.appendChild(label);
      host.appendChild(g);
    }
  }

  const gBars = svg('g');
  days.forEach((d, i) => {
    const x = M.left + i * slot + (slot - barW) / 2;
    const retro = d.basis === 'retro';

    for (const [value, dir] of [[d.added || 0, 1], [d.removed || 0, -1]]) {
      if (value <= 0) continue;
      const h = Math.max(value > 0 ? 1 : 0, scale(value));
      const y = dir > 0 ? mid - h : mid;
      const grow = dir > 0;
      const rect = svg('rect', {
        x: x.toFixed(2), width: barW.toFixed(2),
        y: (dir > 0 ? mid : mid).toFixed(2), height: '0',
        rx: Math.min(2, barW / 3),
        class: 'tl-bar' + (retro ? ' retro' : ' measured') + (grow ? ' grow' : ' shrink'),
        fill: retro ? (grow ? 'url(#hatchGrow)' : 'url(#hatchShrink)')
                    : (grow ? 'var(--grow)' : 'var(--shrink)'),
      });
      gBars.appendChild(rect);

      if (!animate) {
        rect.setAttribute('y', y.toFixed(2));
        rect.setAttribute('height', h.toFixed(2));
      } else {
        // 从零线长出来,错开 8ms,读起来像时间在推进
        const delay = Math.min(600, i * 8);
        setTimeout(() => {
          rect.style.transition = 'y 200ms cubic-bezier(.2,.7,.3,1), height 200ms cubic-bezier(.2,.7,.3,1)';
          rect.setAttribute('y', y.toFixed(2));
          rect.setAttribute('height', h.toFixed(2));
        }, delay);
      }
    }

    // 透明命中区,细条也点得到
    const hit = svg('rect', {
      x: (M.left + i * slot).toFixed(2), y: M.top,
      width: Math.max(slot, 3).toFixed(2), height: plotH,
      class: 'tl-hit',
    });
    hit.addEventListener('mouseenter', (ev) => showDayTip(d, ev));
    hit.addEventListener('mousemove', (ev) => placeTip(el('tlTip'), ev));
    hit.addEventListener('mouseleave', () => hideTip(el('tlTip')));
    gBars.appendChild(hit);
  });
  host.appendChild(gBars);

  /* 日期标签:间隔着标,不然挤成一片。最后一天一定要标(那是「今天」,
   * 是最常看的一格),所以倒数第二个刻度离它太近时让路 —— 不然
   * 「08-25 08-27」会叠在一起糊成一团。
   *
   * 间隔和让路阈值都从 tlLabelStride 那套口径来:坐标现在是 1 单位 = 1 CSS
   * 像素,所以「标签占多宽」是个真实的像素数,能直接和槽宽比。 */
  const labelW = I18N.lang === 'en' ? 44 : 38;
  const stride = tlLabelStride(plotW, days.length, I18N.lang);
  const last = days.length - 1;
  const crowded = (last % stride) * slot < labelW;
  const gLab = svg('g');
  days.forEach((d, i) => {
    if (i !== last) {
      if (i % stride !== 0) return;
      // 挤到最后一格的那个刻度直接不画
      if (crowded && i + stride > last) return;
    }
    const label = svg('text', {
      // 和 tlDayAt 反推时用的是同一个中心公式,不然标签和它对应的那天会飘
      x: tlBarCenter(i, geom, days.length).toFixed(2),
      y: H - 8, class: 'tl-day', 'text-anchor': 'middle',
    });
    label.textContent = fmtDayISO(d.day);
    gLab.appendChild(label);
  });
  host.appendChild(gLab);

  // 缩放状态下把窗口位置画出来,不然不知道自己在 90 天里的哪一段
  if (to - from < allDays.length) {
    const railY = H - 2.5;
    const gRail = svg('g');
    gRail.appendChild(svg('line', {
      x1: M.left, x2: W - M.right, y1: railY, y2: railY, class: 'tl-rail',
    }));
    gRail.appendChild(svg('line', {
      x1: (M.left + (from / allDays.length) * plotW).toFixed(2),
      x2: (M.left + (to / allDays.length) * plotW).toFixed(2),
      y1: railY, y2: railY, class: 'tl-rail-on',
    }));
    host.appendChild(gRail);
  }

  renderTimelineSummary();
  renderTlZoom();
}

/* 缩放控件的状态。范围文字和按钮可用性都跟着窗口走。 */
function renderTlZoom() {
  const box = el('tlZoom');
  if (!box) return;
  const tl = S.timeline;
  const total = (tl && tl.days) ? tl.days.length : 0;
  box.hidden = total === 0;
  if (!total) {
    // 换到没数据的盘时把范围文字清掉。控件是隐藏的,但留着上一个盘的
    // 「08-21 → 08-27」在那儿,一旦扫完重新显示就会闪一下旧日期。
    const stale = el('tlRange');
    if (stale) stale.textContent = '';
    return;
  }

  const [from, to] = tlWindow();
  const span = to - from;
  const full = span >= total;

  const label = el('tlRange');
  if (label) {
    const days = tl.days;
    label.textContent = full
      ? t('tl.allDays', { n: total })
      : t('tl.rangeSpan', { from: days[from].day, to: days[to - 1].day, n: span });
  }
  const dis = (id, off) => { const b = el(id); if (b) b.disabled = off; };
  dis('tlZoomOut', full);
  dis('tlZoomIn', span <= Math.min(TL_MIN_DAYS, total));
  dis('tlReset', full);
  dis('tlLeft', full || from <= 0);
  dis('tlRight', full || to >= total);

  const logBtn = el('tlLog');
  if (logBtn) {
    logBtn.setAttribute('aria-pressed', S.tlLog ? 'true' : 'false');
    logBtn.classList.toggle('on', !!S.tlLog);
  }
}

/* 时间轴的缩放交互:滚轮、拖动、键盘。
 *
 * 事件挂在外壳(可聚焦)上而不是 svg 上:svg 每次 renderTimeline 都会被
 * 清空重画,挂在里面的监听器要么跟着没,要么得反复重绑。外壳一直在。 */
function bindTimelineZoom() {
  const host = el('tlShell');
  const plot = el('timeline');
  if (!host || !plot) return;

  // 从鼠标位置反推它落在哪一天(days 数组的下标)。算法在 tlDayAt,这里只量。
  const dayAt = (clientX) => {
    const tl = S.timeline;
    if (!tl || !tl.days || !tl.days.length) return null;
    const r = plot.getBoundingClientRect();
    if (!r.width) return null;
    const [from, to] = tlWindow();
    return tlDayAt(clientX - r.left, r.width, from, to);
  };

  /* 滚轮缩放。这里要 preventDefault 挡掉页面滚动,所以监听器必须
   * passive: false —— Chrome 对 wheel 默认是 passive,不显式声明的话
   * preventDefault 会被忽略,页面照样滚。
   *
   * 只在按住 Ctrl(或触控板捏合,浏览器也报成 ctrlKey)时才缩放:
   * 图就在页面中间,直接吃掉滚轮的话用户想往下翻会被卡住,很烦。 */
  host.addEventListener('wheel', (ev) => {
    if (!ev.ctrlKey) return;              // 普通滚轮留给页面
    ev.preventDefault();
    tlZoom(ev.deltaY > 0 ? 1.25 : 0.8, dayAt(ev.clientX));
  }, { passive: false });

  // 拖动平移。只在缩放状态下有意义。
  let drag = null;
  host.addEventListener('pointerdown', (ev) => {
    const total = (S.timeline && S.timeline.days) ? S.timeline.days.length : 0;
    const [from, to] = tlWindow();
    if (to - from >= total) return;
    drag = { x: ev.clientX, start: from, span: to - from };
    host.setPointerCapture(ev.pointerId);
    host.classList.add('dragging');
    hideTip(el('tlTip'));
  });
  host.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    const r = plot.getBoundingClientRect();
    if (!r.width) return;
    // 拖过去多少屏幕像素,换算成多少天。绘图区占宿主的比例从 tlGeom 拿。
    const g = tlGeom(plot.clientWidth || r.width);
    const perDay = r.width * (g.plotW / g.W) / drag.span;
    const moved = Math.round((drag.x - ev.clientX) / perDay);
    const total = S.timeline.days.length;
    const next = clampView({ start: drag.start + moved, span: drag.span }, total);
    if (!next || next.start === (S.tlView && S.tlView.start)) return;
    S.tlView = next;
    renderTimeline();
  });
  const endDrag = (ev) => {
    if (!drag) return;
    drag = null;
    host.classList.remove('dragging');
    try { host.releasePointerCapture(ev.pointerId); } catch (_) { /* 已经放了 */ }
  };
  host.addEventListener('pointerup', endDrag);
  host.addEventListener('pointercancel', endDrag);

  // 键盘:图本身可聚焦,方向键平移,+/- 缩放,0 复位
  host.addEventListener('keydown', (ev) => {
    const [from, to] = tlWindow();
    const span = to - from;
    const step = Math.max(1, Math.round(span / 4));
    switch (ev.key) {
      case 'ArrowLeft':  tlPan(-step); break;
      case 'ArrowRight': tlPan(step); break;
      case '+': case '=': tlZoom(0.8); break;
      case '-': case '_': tlZoom(1.25); break;
      case '0': tlReset(); break;
      case 'l': case 'L': S.tlLog = !S.tlLog; renderTimeline(); break;
      case 'Home': tlPan(-1e9); break;
      case 'End':  tlPan(1e9); break;
      default: return;
    }
    ev.preventDefault();
  });

  // 按钮
  const on = (id, fn) => { const b = el(id); if (b) b.addEventListener('click', fn); };
  on('tlZoomIn', () => tlZoom(0.8));
  on('tlZoomOut', () => tlZoom(1.25));
  on('tlReset', tlReset);
  on('tlLog', () => { S.tlLog = !S.tlLog; renderTimeline(); });
  on('tlLeft', () => { const [f, t] = tlWindow(); tlPan(-Math.max(1, Math.round((t - f) / 2))); });
  on('tlRight', () => { const [f, t] = tlWindow(); tlPan(Math.max(1, Math.round((t - f) / 2))); });

  // 预设范围。比反复滚滚轮快,也是给不知道能滚的人一个入口。
  paintSpanButtons();
  document.querySelectorAll('#tlZoom [data-span]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const total = (S.timeline && S.timeline.days) ? S.timeline.days.length : 0;
      if (!total) return;
      const want = Number(btn.dataset.span);
      // 一律贴着最右边显示,也就是「最近 N 天」
      S.tlView = clampView({ start: total - want, span: want }, total);
      renderTimeline();
    });
  });

  /* 宽度变了要重画。以前不用:viewBox 是死的,整张图靠 CSS 缩放,窗口一变
   * 浏览器自己就按新尺寸拉一遍,一行 JS 都不需要。改成按像素画之后就得自己管了。
   *
   * 只在宽度真的变了才画。renderTimeline 改的是 viewBox 高度,而 SVG 的渲染高度
   * 由 viewBox 的宽高比推出来 —— 也就是说重画会改宿主的高度,又触发 observer。
   * 不比一下宽度就是个无限循环。
   *
   * 合并连续的 resize:拖窗口边缘一秒能触发几十次,每次都重画
   * (90 根柱子 + 命中区 + 标签,全是 DOM 节点)会卡。
   *
   * 用 setTimeout 而不是 requestAnimationFrame。页面隐藏时 rAF 完全冻结,
   * 浏览器里实测过:hidden 状态下 rAF 排的回调 600ms 都没跑,setTimeout 跑了。
   * 而首绘量到 0 就是靠这次重叫补上的 —— 排在 rAF 上,后台标签页里加载的页面
   * 时间轴会一直空着(实测:外壳已经 1029px,viewBox 还是 null)。
   *
   * 按规范 rAF 回调只是推迟、页面一显示就跑,应该能自愈。但这条路在这儿没法
   * 验证,而这张图原来根本不依赖它(viewBox 死的,CSS 缩放,零 JS)—— 是改成
   * 按像素画才引入的。合并用 setTimeout 一样管用,那就别留这个不确定性。 */
  let pendingW = 0;
  const ro = new ResizeObserver(() => {
    if (pendingW) return;
    pendingW = setTimeout(() => {
      pendingW = 0;
      if (!S.timeline) return;
      // 和 renderTimeline 量同一个东西(外壳),不然两边的判断会对不上
      const w = host.clientWidth;
      if (w < 2) return;
      if (tlGeom(w).W === S.tlW) return;
      renderTimeline();
    }, RESIZE_COALESCE_MS);
  });
  ro.observe(host);
}

/* 「7 天 / 14 天 / 30 天」。数字在 data-span 上,文案在这儿拼 ——
 * 写死在 HTML 里的话切了语言还是中文。 */
function paintSpanButtons() {
  document.querySelectorAll('#tlZoom [data-span]').forEach((btn) => {
    btn.textContent = t('tl.spanDays', { n: Number(btn.dataset.span) });
  });
}

function showDayTip(d, ev) {
  const tip = el('tlTip');
  clear(tip);
  const retro = d.basis === 'retro';

  const head = tag('div', { class: 'p' });
  head.appendChild(document.createTextNode(d.day + '  '));
  head.appendChild(tag('span', { class: 'badge ' + (retro ? 'retro' : 'measured') },
    retro ? t('basis.retroShort') : t('basis.measuredShort')));
  tip.appendChild(head);

  // 回溯日只有一个量:那天写入、现在还在盘上多少。它的 added 和 net 是同一个
  // 数(后端两处都填了它),印成「新增 60 MB 净 +60 MB」既重复又在暗示这是
  // 当天的净变化 —— 而回溯恰恰答不了那个问题。
  const m = tag('div', { class: 'm' });
  if (retro) {
    m.appendChild(document.createTextNode(t('tip.stillOnDisk')));
    m.appendChild(tag('b', null, fmtBytes(d.added || 0)));
  } else {
    m.appendChild(document.createTextNode(t('tip.added')));
    m.appendChild(tag('b', null, fmtBytes(d.added || 0)));
    if (d.removed) {
      m.appendChild(document.createTextNode(t('tip.removed')));
      m.appendChild(tag('b', null, fmtBytes(d.removed)));
    }
    m.appendChild(document.createTextNode(t('tip.net')));
    m.appendChild(tag('b', null, fmtSigned(d.net || 0)));
  }
  tip.appendChild(m);

  if (d.files_added) {
    tip.appendChild(tag('div', { class: 'm' }, t('grid.fileCount', { n: fmtCount(d.files_added) })));
  }

  // USN 能补上「当天删了几个」,这是快照差看不到的
  if (d.usn && (d.usn.deleted || d.usn.created)) {
    const u = tag('div', { class: 'm' });
    u.appendChild(document.createTextNode(t('tip.journalCreated')));
    u.appendChild(tag('b', null, fmtCount(d.usn.created)));
    u.appendChild(document.createTextNode(t('tip.journalDeleted')));
    u.appendChild(tag('b', null, fmtCount(d.usn.deleted)));
    tip.appendChild(u);
  }

  // 说「长得最多」而不是「增长来自」:上面那两个数是按顶层目录拆的净值,
  // 这个列表是各深度上单个目录的涨跌榜,两者不是同一个口径。真实数据里
  // Users 整体缩了 647 MB,里面的 .codex 却长了 87 MB —— 于是列表第一行会
  // 比「新增」还大。写成「来自」等于说这是那个数的分解,那是不成立的。
  //
  // sign:shrinkers 后端存的是减少量的绝对值(排名和折叠都按大小来,存负数
  // 要到处写 abs)。显示时必须自己带上负号,否则 fmtSigned 会给它加个加号,
  // 把「缩了 753 MB」印成「+753 MB」。
  for (const [label, list, sign] of [
    [t('tip.grewMost'), d.contributors, 1],
    [t('tip.shrankMost'), d.shrinkers, -1],
  ]) {
    const top = (list || []).slice(0, 4);
    if (!top.length) continue;
    tip.appendChild(tag('div', { class: 'm sub' }, label));
    for (const c of top) {
      const row = tag('div', { class: 'm' });
      row.appendChild(tag('b', null, fmtSigned(sign * c.bytes)));
      row.appendChild(document.createTextNode('  ' + c.path));
      tip.appendChild(row);
    }
  }

  if (retro) {
    tip.appendChild(tag('div', { class: 'm hint' },
      t('tip.retroWhy')));
  }
  showTip(tip, ev);
}

function renderTimelineSummary() {
  const box = el('timelineTotals');
  const sum = S.timeline && S.timeline.summary;
  if (box) {
    clear(box);
    if (sum) {
      // 两层分开报。以前这里是「N 天净变化 +93 GB」,那个数把回溯量和实测差
      // 加在了一起 —— 量纲不同,加出来的东西没有含义,而且系统性偏大:本机
      // 报 +93 GB,推出 91 天前只用了 85.6 GB,可盘上「91 天以上」的文件现在
      // 还有 81.3 GB,而那只是活下来的部分。
      if (sum.retro_days) {
        box.appendChild(tag('span', { class: 'dim' }, t('sum.retroSpan', { days: sum.days })));
        box.appendChild(tag('b', null, fmtBytes(sum.retro_bytes)));
      }
      if (sum.measured_days) {
        const net = tag('b', { class: sum.measured_net >= 0 ? 'grow' : 'shrink' },
          fmtSigned(sum.measured_net));
        box.appendChild(tag('span', { class: 'dim' },
          t('sum.measuredSpan', { sep: sum.retro_days ? '  ·  ' : '', days: sum.measured_days })));
        box.appendChild(net);
        box.appendChild(tag('span', { class: 'dim' },
          t('sum.addedRemoved', { added: fmtBytes(sum.measured_added), removed: fmtBytes(sum.measured_removed) })));
      }
    }
  }

  // 说明只讲这份数据的实际构成,静态图例已经解释过两种柱子
  const noteBox = el('timelineNotice');
  if (!noteBox) return;
  clear(noteBox);
  if (!sum) return;

  const bits = [];
  if (sum.retro_days) bits.push(t('sum.retroDays', { n: sum.retro_days }));
  if (sum.measured_days) bits.push(t('sum.measuredDays', { n: sum.measured_days }));
  if (sum.busiest_day) bits.push(t('sum.busiest', { day: sum.busiest_day }));

  if (!sum.measured_days) {
    const note = tag('div', { class: 'notice' });
    note.appendChild(tag('b', null, t('sum.allRetro')));
    note.appendChild(document.createTextNode(
      t('sum.allRetroWhy1')
      + t('sum.allRetroWhy2')));
    noteBox.appendChild(note);
  } else if (bits.length) {
    noteBox.appendChild(tag('p', { class: 'basis-note' }, bits.join(' · ')));
  }
}

// ---- 表格 ----
function fillRows(bodyId, rows, emptyText) {
  const body = el(bodyId);
  if (!body) return;
  clear(body);
  if (!rows.length) {
    const tr = tag('tr');
    tr.appendChild(tag('td', { colspan: '9', class: 'blank' }, emptyText));
    body.appendChild(tr);
    return;
  }
  for (const cells of rows) {
    const tr = tag('tr');
    for (const c of cells) {
      const td = tag('td', c.attrs || null);
      if (c.node) td.appendChild(c.node);
      else td.textContent = c.text === undefined ? '' : String(c.text);
      if (c.title) td.title = c.title;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
}

function pathCell(path, extraTitle, isFile) {
  const attrs = { class: 'path' };
  // 右键菜单靠这个标记决定是「打开目录」还是「定位文件」
  if (isFile) attrs['data-file'] = '1';
  return {
    text: path,
    attrs,
    title: extraTitle ? `${path}\n${extraTitle}` : path,
  };
}

function bytesCell(n, cls) {
  return { text: fmtBytes(n), attrs: { class: 'num ' + (cls || '') } };
}

function renderHotspots() {
  const h = S.hotspots || {};
  const empty = h.snapshot ? t('grid.noMatch') : t('grid.noData');

  // 列顺序跟着 index.html 的表头:目录 / 占用 / 最近写入
  fillRows('bigDirsBody', (h.dirs || []).map((d) => [
    pathCell(d.path, t('grid.fileCount', { n: fmtCount(d.files) })),
    bytesCell(d.bytes),
    { text: ageText(d.newest), attrs: { class: 'num dim' }, title: fmtTime(d.newest) },
  ]), empty);

  fillRows('recentBody', (h.recent || []).map((d) => [
    pathCell(d.path),
    bytesCell(d.bytes),
    { text: ageText(d.newest), attrs: { class: 'num dim' }, title: fmtTime(d.newest) },
  ]), empty);

  // 路径 / 类型 / 占用 / 等级
  //
  // 后端只给规则代号(c.rule),标签和建议在这边按代号取 —— 见 i18n.js 末尾。
  // 代号认不出来时退回代号本身,而不是留空:空着的话表格看起来像后端没返回
  // 数据,而实际上是少了一条文案,两种毛病要修的地方完全不同。
  const safety = { safe: t('clean.safe'), review: t('clean.review'), careful: t('clean.careful') };
  const ruleText = (code, part) => {
    if (!code) return '';
    const key = 'clean.rule.' + code + '.' + part;
    return I18N.raw(key) ? t(key) : code;
  };
  const cleanup = h.cleanup || [];
  fillRows('cleanupBody', cleanup.map((c) => {
    const advice = ruleText(c.rule, 'advice');
    return [
      pathCell(c.path, advice),
      { text: ruleText(c.rule, 'label'), attrs: { class: 'dim' }, title: advice },
      bytesCell(c.bytes),
      { node: tag('span', { class: 'tag ' + c.safety }, safety[c.safety] || c.safety) },
    ];
  }), t('clean.none'));

  const total = el('cleanupTotal');
  if (total) {
    clear(total);
    if (cleanup.length) {
      const safeBytes = cleanup.filter((c) => c.safety === 'safe')
        .reduce((s, c) => s + (c.bytes || 0), 0);
      const allBytes = cleanup.reduce((s, c) => s + (c.bytes || 0), 0);
      total.appendChild(tag('span', { class: 'dim' }, t('clean.total')));
      total.appendChild(tag('b', null, fmtBytes(allBytes)));
      total.appendChild(tag('span', { class: 'dim' }, t('clean.ofWhichSafe', { bytes: fmtBytes(safeBytes) })));
    }
  }

  renderTreemapTotals();
}

/* 树图上方那行「本层 x GB · n 项」。
 *
 * 单独一个函数,因为它讲的是 S.tree(当前这一层),而不是 S.hotspots(整盘)。
 * 原来它写在 renderHotspots 里,而进目录只调 drawTreemap —— 画布换了、
 * 这行没换,屏幕上一半是新目录的图、一半是上一层的数字。
 * 拆出来之后 enterPath 只重画这一行,不用连整个热点面板一起重画。 */
function renderTreemapTotals() {
  const box = el('treemapTotals');
  if (!box) return;
  clear(box);
  if (!S.tree) return;
  // 别叫 t,那是取文案的全局函数
  const total = treeTotal();
  if (!total) return;
  box.appendChild(tag('span', { class: 'dim' }, t('tm.thisLevel')));
  box.appendChild(tag('b', null, fmtBytes(total)));
  const n = (S.tree.children || []).length;
  box.appendChild(tag('span', { class: 'dim' }, t('grid.itemCount', { n: n })));
}

/* 后端给的口径说明:{code, vars}。文案在 i18n.js 里按 diff.caveat.<code> 取。
 *
 * vars.bytes 在这儿转成 vars.size(已格式化的 "64 MB")—— 文案函数不该
 * 自己去调 fmtBytes,那个在这个文件里,i18n.js 要能单独加载。 */
function caveatText(c) {
  if (!c) return '';
  if (typeof c === 'string') return c;      // 老格式:后端直接给的整句
  const vars = Object.assign({}, c.vars);
  if (vars.bytes !== undefined) vars.size = fmtBytes(vars.bytes);
  const key = 'diff.caveat.' + c.code;
  return I18N.raw(key) ? t(key, vars) : c.code;
}

function renderDiff(diff) {
  S.diff = diff;
  const section = el('diffSection');
  const range = el('diffRange');
  const net = el('diffNet');
  const notice = el('diffNotice');

  if (!diff || !diff.available) {
    // 原来这里整段藏起来,理由是「空表格比不显示更让人困惑」—— 对,但后端
    // 同时也送了一句 reason(api.py:270「至少要两次扫描才能对比」),
    // 藏掉整段那句话就永远到不了用户眼前。写了却到不了的文案和没写一样。
    //
    // 现在:表格还是不摆(空的两列确实没意义),但把原因留在标题下面。
    // 第一次用的人正需要这句 —— 再扫一次就有对比了,不说他不会知道。
    if (section) section.hidden = false;
    if (range) range.textContent = '';
    if (net) clear(net);
    if (notice) {
      clear(notice);
      // 不用后端送来的 diff.reason:那是生中文,英文界面下会漏出来。
      // 这里只有一种情况(快照不够两个),用本地文案就够了 —— 和
      // diff.caveat.* 那批一样,后端管判定,文案归前端。
      notice.appendChild(tag('div', { class: 'notice' }, t('diff.needTwo')));
    }
    // 空态不能用 diff.noGrow(「没有目录变大」)—— 那是在陈述一次根本没发生
    // 的比较的结论。没比过和比过没发现,是两回事。
    fillRows('grewBody', [], t('diff.notYet'));
    fillRows('shrankBody', [], t('diff.notYet'));
    return;
  }
  if (section) section.hidden = false;

  if (range) {
    range.textContent = `${fmtTime(diff.before_at)} → ${fmtTime(diff.after_at)}`;
  }
  if (net) {
    clear(net);
    net.appendChild(tag('span', { class: 'dim' }, t('diff.net')));
    net.appendChild(tag('b', { class: diff.net >= 0 ? 'grow' : 'shrink' }, fmtSigned(diff.net)));
    net.appendChild(tag('span', { class: 'dim' },
      `  ${fmtBytes(diff.before_bytes)} → ${fmtBytes(diff.after_bytes)}`));
  }
  if (notice) {
    clear(notice);
    for (const c of (diff.caveats || [])) {
      notice.appendChild(tag('p', { class: 'basis-note' }, caveatText(c)));
    }
  }

  const row = (d, cls) => [
    pathCell(d.path, `${fmtBytes(d.before)} → ${fmtBytes(d.after)}`),
    { text: fmtSigned(d.delta), attrs: { class: 'num ' + cls } },
  ];
  fillRows('grewBody', (diff.grew || []).map((d) => row(d, 'grow')), t('diff.noGrow'));
  fillRows('shrankBody', (diff.shrank || []).map((d) => row(d, 'shrink')), t('diff.noShrink'));
}

function renderChanges(ch) {
  S.changes = ch;
  const section = el('deletedSection');
  const cov = (ch && ch.coverage) || {};
  const events = (ch && ch.events) || [];
  const deletes = events.filter((e) => e.kind === 'delete');

  // 0 条事件有三种原因,以前一律藏掉 —— 意图是「别摆一张空表让人以为什么都
  // 没删过」,但顺手也把「没提权」藏了。不提权时后端整段跳过 USN
  // (app.py 里的 privileges.is_admin() 门槛),库里永远 0 条,于是这个功能
  // 对非管理员用户根本不存在,而且不给任何提示。实测:同一台机器直读日志
  // 有 8 万条该入库,库里是 0。
  //
  // 现在分三种:
  //   没权限            → 露出来说要提权
  //   有权限但读失败    → 露出来说为什么。NTFS 上 USN 日志可以关,不少机器默认
  //                       就是关的;藏起来的话跟「什么都没删过」长得一模一样
  //   有权限、读成了、真空 → 照旧藏
  if (!cov.events) {
    const isAdmin = !!((S.status && S.status.privileges || {}).is_admin);
    // 后端读失败时会把原因存进 usn_status,coverage 带出来。available 有三态:
    // true 读成了、false 读失败、null 这个盘没扫过(别把没扫过说成故障)。
    const failed = cov.available === false;
    // 没提权时先说提权:那是用户能动手解决的那一个,而且不提权时后端整段跳过
    // USN、连 usn_status 都不写,两个条件会同时成立 —— 这时候讲「日志没开」
    // 是误导。
    if (isAdmin && !failed) {
      if (section) section.hidden = true;
      return;
    }
    if (section) section.hidden = false;
    const covBox0 = el('usnCoverage');
    if (covBox0) clear(covBox0);
    const notice0 = el('usnNotice');
    if (notice0) {
      clear(notice0);
      notice0.appendChild(tag('div', { class: 'notice' }, isAdmin
        // 具体原因是后端给的(日志没开 / 读取失败 / 设备没准备好),原样带出来 ——
        // 前端猜不出是哪种,猜错了比不说更糟
        ? t('del.unavailable', { why: cov.reason || t('del.unavailableUnknown') })
        : t('del.needAdmin')));
    }
    // 表格空态要短。长的解释在上面那条 notice 里,两处都写整段就重复了。
    fillRows('deletedBody', [],
             isAdmin ? t('del.unavailableRow') : t('del.needAdminRow'));
    return;
  }
  if (section) section.hidden = false;

  const covBox = el('usnCoverage');
  if (covBox) {
    clear(covBox);
    covBox.appendChild(tag('span', { class: 'dim' }, t('del.coverage')));
    covBox.appendChild(tag('b', null, t('del.coverageSpan', { first: cov.first_day, days: cov.days })));
    covBox.appendChild(tag('span', { class: 'dim' }, t('del.coverageEvents', { n: fmtCount(cov.events) })));
  }

  const notice = el('usnNotice');
  if (notice) {
    clear(notice);
    const unknown = deletes.filter((e) => e.bytes === null || e.bytes === undefined).length;
    // 只有名字、没有路径的行。日志记的是文件 ID,要靠扫描时的目录表反查成路径 ——
    // 整棵目录树在扫描前就没了的话,反查不出来。实盘上 D: 盘 32031 条删除里
    // 只有 450 条能反查到路径,剩下的就是这种。
    const noloc = deletes.filter((e) => !e.path).length;
    const note = tag('div', { class: 'notice' });
    // 这句原来是后端送过来的,现在在这边出 —— 它没有后端才知道的参数
    note.appendChild(document.createTextNode(t('del.sizeNote')));
    if (unknown) {
      note.appendChild(document.createTextNode(
        t('del.unknownSize', { n: fmtCount(unknown) })));
    }
    if (noloc) {
      note.appendChild(document.createTextNode(
        t('del.noPathNote', { n: fmtCount(noloc) })));
    }
    if (note.childNodes.length) notice.appendChild(note);
  }

  fillRows('deletedBody', deletes.map((e) => {
    // 后端按路径把同一个文件的多条 USN 记录合成了一行,count 是折叠掉的条数。
    // >1 就标出来:反复重建的临时文件能攒到上百次,而那件事跟「丢了个东西」
    // 完全是两回事,不该让它看起来一样。老后端不送这个字段,当 1 处理。
    const n = e.count > 1 ? e.count : 0;
    // 路径反查不出来的行,拿到的只有文件名。这种行得跟真路径区分开,有两个原因:
    //
    // 一、名字会重。实盘上 problems-report.html 出现 5 次、classes 5 次 ——
    //     它们是不同目录里的不同文件,可界面上看着一模一样,像是列表出了毛病。
    //     (不能按名字合并:那 5 个真的是 5 个文件,合并等于凭空说是一个。)
    //
    // 二、右键菜单会撒谎。它把这一格的文字当盘内相对路径,拼出来是
    //     「D:\problems-report.html」并且印在菜单顶上 —— 那个位置这文件从来没待过。
    //     所以不给 data-file 标记,让 bindCtxMenu 直接跳过这种行。
    const known = !!e.path;
    const cell = pathCell(
      e.path || e.name,
      [known ? null : t('del.noPathRow'),
       n ? t('del.timesNote', { n: fmtCount(n) }) : null].filter(Boolean).join('\n') || null,
      known,
    );
    if (!known) {
      cell.attrs['data-noloc'] = '1';
      cell.attrs.class += ' dim';
    }
    return [
      cell,
      { text: (e.bytes === null || e.bytes === undefined) ? t('age.unknown') : fmtBytes(e.bytes),
        attrs: { class: 'num ' + ((e.bytes === null || e.bytes === undefined) ? 'dim' : 'shrink') } },
      // 次数跟在时间后面:它限定的正是这个时间的含义 —— 最后一次,不是唯一一次
      { text: fmtTime(e.at) + (n ? ' ' + t('del.times', { n: fmtCount(n) }) : ''),
        attrs: { class: 'num dim' } },
    ];
  }), t('del.none'));
}

// ---- 扫描 ----
function setScanState(state) {
  // 存下来,换盘的时候要按新的 S.drive 重画一遍(那个t('err.whichDrive')的前缀
  // 得跟着变)。以前不存,于是这一行只在扫描时更新,切了盘还挂着旧措辞。
  S.scan = state;
  const btn = el('scanBtn');
  const label = el('scanState');
  const running = !!(state && state.running);
  if (btn) {
    btn.disabled = running;
    // 不是 'pulse' 了。那个类带 width/height/border-radius:50%,加到按钮上会把
    // 按钮本身压成一个椭圆点,文字从里面溢出来。scanning 只改颜色,不碰尺寸。
    btn.classList.toggle('scanning', running);
    btn.textContent = running ? t('scan.running') : t('nav.scan');
  }
  if (!label) return;
  clear(label);
  // 这一行讲的是"刚才那一次扫描",而 /api/scan/state 是全局的:扫完 D: 再切到
  // C:,这里照样印 D: 那次的时间和耗时,而它挨着 C: 的盘符和 C: 的数字 ——
  // 读起来就是"C: 是 09:52 扫的、花了 33.7 秒",两个数都是假的(C: 实际 07:48
  // 扫的、花了 119 秒)。所以盘符跟当前看的不是一个时,必须写出来是哪个盘。
  // 不直接藏掉:扫描失败这种事不该因为切了个盘就没人告诉你。
  const other = state && state.drive && state.drive !== S.drive ? state.drive + ' ' : '';
  if (running) {
    const phase = state.phase || t('scan.inProgress');
    // 有条数就报条数。原来后端根本没在报(progress 没接线),这一行整次扫描
    // 一动不动 —— 一分多钟里唯一的动静是按钮上那个转圈,不知道它在干什么。
    const counted = state.counted || 0;
    const key = counted > 0 ? 'scan.phaseCounted' : 'scan.phase';
    label.appendChild(tag('span', null, t(key, {
      drive: state.drive || '', phase: phase, n: counted,
    })));
    label.hidden = false;
  } else if (state && state.error) {
    label.appendChild(tag('span', { class: 'bad' }, other + t('scan.failed') + state.error));
    label.hidden = false;
  } else if (state && state.finished_at) {
    const r = state.result || {};
    const bits = [other + t('scan.doneAt') + fmtTime(state.finished_at)];
    if (r.method) bits.push(r.method === 'mft' ? 'MFT' : t('base.walkMode'));
    if (r.duration_ms) bits.push((r.duration_ms / 1000).toFixed(1) + t('scan.seconds'));
    label.appendChild(tag('span', { class: 'dim' }, bits.join(' · ')));
    label.hidden = false;
    // 这里不再重复退化原因。它已经落进快照,常驻在基线数字下面那一行 —— 那是
    // 它该待的地方(描述的是这份数据本身,不是刚才那一次扫描)。两处都印的话,
    // 加上顶上的提权横幅,同一件事连着三段橙字,反而没人看了。
    // 这一行只留「什么时候扫的、用什么扫的、花了多久」,方法名本身就够指出退化。
  } else {
    label.hidden = true;
  }
}

/* 不变量:后端说在扫,就必须有一个定时器在盯着。
 *
 * 原来 setInterval 只写在 startScan 里,于是三种情况下界面会永久停在
 * 「扫描中」—— 刷新页面时正在扫(boot() 只 await 一次就返回了)、
 * POST 撞上 409(计划任务或第二个标签页在扫,异常落进 catch)、
 * 以及轮询自己失败清掉了定时器而扫描还在跑。
 *
 * 所以改成:凡是看到 running,就确保定时器在;建之前先查有没有,
 * 连点按钮也不会叠出第二个。 */
function ensureScanPolling() {
  if (S.scanPoll) return;
  S.scanPoll = setInterval(pollScan, 1200);
}

function stopScanPolling() {
  if (S.scanPoll) { clearInterval(S.scanPoll); S.scanPoll = null; }
}

/* 取一次状态失败不代表扫描停了 —— 后台线程照样在跑,这时候放弃轮询等于
 * 把界面永久钉在最后看到的样子。所以容忍几次,连着失败才认为服务真的没了。
 * 上限要小:1.2 秒一次,5 次就是 6 秒,足够跨过一次网络抖动,又不会对着
 * 一个已经关掉的后端一直打。 */
const SCAN_POLL_MAX_ERRORS = 5;

async function pollScan() {
  try {
    const state = await api('/api/scan/state');
    S.scanPollErrors = 0;
    setScanState(state);
    if (state.running) { ensureScanPolling(); return true; }
    /* 只有刚才真在轮询,才说明有一次扫描刚结束、需要重新取数。
     *
     * boot() 末尾也会调一次 pollScan(为了接住别处已经在跑的扫描:计划任务、
     * 命令行、另一个标签页),而它上一行刚 loadDrive 过。原来这里无条件重载,
     * 于是每次开页面整份数据都抓两遍 —— 网络面板上 status ×3,timeline/tree/
     * hotspots/diff/changes 各 ×2。C: 那几个接口要查 16 万行 dirs,白跑一遍,
     * 两轮之间界面还闪一下。
     *
     * S.scanPoll 是现成的判据,不用另加状态:boot 调进来时它是 null,
     * 定时器那一路和 startScan 那一路都非 null。 */
    const wasPolling = !!S.scanPoll;
    stopScanPolling();
    if (wasPolling) await loadDrive(S.drive, { keepPath: true });
    return false;
  } catch (err) {
    S.scanPollErrors = (S.scanPollErrors || 0) + 1;
    if (S.scanPollErrors >= SCAN_POLL_MAX_ERRORS) {
      stopScanPolling();
      S.scanPollErrors = 0;
      setScanState({ error: err.message });
      return false;
    }
    // 还没到上限:留着定时器,下一轮再试。这一次的失败不往界面上写 ——
    // 抖一下就闪一行红字,比不显示更让人以为出事了。
    ensureScanPolling();
    return true;
  }
}

async function startScan() {
  setScanState({ running: true, drive: S.drive });
  try {
    await post('/api/scan', { drive: S.drive });
  } catch (err) {
    /* 最常见的失败就是 409「已经有一次扫描在进行中」—— 那恰恰说明有东西在扫,
     * 正是最需要轮询的时候。所以不管成没成,都去查一次真实状态:
     * 真在扫就跟着刷,真没扫才把错误显示出来。 */
    ensureScanPolling();
    const running = await pollScan();
    if (!running) setScanState({ error: err.message });
    return;
  }
  ensureScanPolling();
}

// ---- 计划任务 ----
function paintSchedule(st) {
  const toggle = el('scheduleToggle');
  const label = el('scheduleLabel');
  const detail = el('scheduleDetail');
  const on = !!(st && st.exists && st.enabled);

  if (toggle) toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
  if (label) label.textContent = on ? t('set.dailyOn') : t('set.daily');
  if (!detail) return;

  clear(detail);
  if (!st || !st.exists) {
    detail.appendChild(tag('span', { class: 'dim' },
      t('set.dailyWhy')));
    return;
  }
  const bits = [];
  if (st.schedule) bits.push(st.schedule);
  if (st.next_run) bits.push(t('set.next') + st.next_run);
  if (st.last_run) bits.push(t('set.last') + st.last_run);
  if (st.last_result) bits.push(t('set.result') + st.last_result);
  detail.appendChild(tag('span', { class: st.enabled ? 'dim' : 'bad' },
    (st.enabled ? '' : t('set.paused')) + bits.join(' · ')));
}

function paintSettingsInfo() {
  const box = el('settingsInfo');
  if (!box || !S.status) return;
  clear(box);
  const priv = S.status.privileges || {};
  box.appendChild(tag('b', null, priv.is_admin ? t('set.adminYes') : t('set.adminNo')));
  box.appendChild(document.createTextNode(priv.is_admin
    ? t('set.adminYesWhy')
    : t('set.adminNoWhy')));
  box.appendChild(tag('div', { class: 'dim' },
    t('set.db', { path: S.status.db_path, bytes: fmtBytes(S.status.db_bytes) })));
}

async function loadSchedule() {
  const toggle = el('scheduleToggle');
  if (!toggle) return;
  try {
    S.schedule = await api('/api/schedule');
    toggle.disabled = false;
    paintSchedule(S.schedule);
  } catch (err) {
    toggle.disabled = true;
    const detail = el('scheduleDetail');
    if (detail) { clear(detail); detail.appendChild(tag('span', { class: 'bad' }, err.message)); }
  }
}

async function toggleSchedule() {
  const toggle = el('scheduleToggle');
  const want = toggle.getAttribute('aria-pressed') !== 'true';
  toggle.disabled = true;
  try {
    const st = await post('/api/schedule', { enabled: want });
    S.schedule = st;
    paintSchedule(st);
  } catch (err) {
    const detail = el('scheduleDetail');
    if (detail) { clear(detail); detail.appendChild(tag('span', { class: 'bad' }, err.message)); }
    await loadSchedule();
  } finally {
    toggle.disabled = false;
  }
}

// ---- 载入 ----

/* 每次载入领一个号,写回 S 之前先看自己的号还是不是最新的。
 *
 * 六个请求各自 .then 往 S 上写,谁后回来谁说了算 —— 而「谁后回来」跟
 * 「谁是用户最后要的」没有关系。pollScan 扫完会调 loadDrive,用户正好这时候
 * 切了盘或者点进一层目录,两批响应交错:S.drive 已经是 D: 了,而 C: 那批
 * 慢响应回来把树盖成 C: 的。屏幕上就是盘符、基线、树图三处各说各话,
 * 或者干脆一片空白 —— 用户说的「结果时不时就没了」。
 *
 * 号只在这两个入口领。别的地方(renderX)不写 S 的数据字段,不需要管。 */
function newLoadToken() {
  S.loadSeq = (S.loadSeq || 0) + 1;
  return S.loadSeq;
}

function isCurrentLoad(token) {
  return token === S.loadSeq;
}

async function enterPath(path) {
  const token = newLoadToken();
  S.path = path;
  renderCrumbs();
  try {
    const tree = await api('/api/tree', { drive: S.drive, path: path || null });
    if (!isCurrentLoad(token)) return;   // 用户已经去别处了,这份数据过期了
    S.tree = tree;
    S.fade.clear();
    drawTreemap(false);
    // 表头跟画布是同一份数据的两半,必须一起换。原来这儿不调,于是进目录之后
    // 画布换了、表头还是上一层的数字,读起来像「数据还在只是图没了」。
    renderTreemapTotals();
  } catch (err) {
    if (!isCurrentLoad(token)) return;
    banner({ text: err.message });     // 后端抛的原文,翻不了
  }
}

async function loadDrive(drive, opts) {
  const token = newLoadToken();
  S.drive = drive;
  if (!opts || !opts.keepPath) { S.path = ''; S.fade.clear(); }
  // 换盘就复位缩放:C: 上第 40 天的窗口套到 D: 上没有任何意义
  S.tlView = null;
  S.tlAnimated = false;
  const path = S.path || null;
  renderDriveTabs();
  renderBaseline();
  if (S.scan) setScanState(S.scan);   // 「是哪个盘扫的」那个前缀要按新的 S.drive 重算

  /* 每个 .then 都先验号。不能只在最后验一次:六个请求各写一个字段,
   * 中间任何一个漏了验,那个字段就会是旧盘的。 */
  const keep = (fn) => (r) => { if (isCurrentLoad(token)) fn(r); };

  /* 基线数字必须跟着重取。S.drives 原来只在开机时填一次,之后再没动过 ——
   * 于是扫完一个盘,上面那行还是「尚无快照」,扫到/文件/快照个数和口径说明
   * 全都不出现,非得刷新页面才对。而下面的时间轴已经是新数据了,同一屏上
   * 两半自相矛盾。换盘也一样:开着页面扫完 D: 再切过去,看到的是空的。
   *
   * 但开页面那一次是白取的:boot() 必须先自己取一遍(挑哪个盘、要不要弹
   * 「不是管理员」都靠它),下一句就调这里,于是 /api/status 请求两遍。
   * get_status 按盘数干活 —— 每个盘 latest_snapshot + volume_space(Win32,
   * 盘不在时还得等它失败)+ list_snapshots(limit=10000) 拉出来数个数 +
   * usn_coverage,外加一次 db_size_bytes。
   *
   * 所以让调用方说一声「我手上这份是刚取的」,而不是把这一路删掉:换盘和
   * 扫完刷新都必须重取,snapshot_count 刚变了。 */
  const jobs = [
    ...(opts && opts.keepStatus ? [] : [
      api('/api/status').then(keep((r) => { S.status = r; S.drives = r.drives || []; })),
    ]),
    api('/api/timeline', { drive, days: 90 }).then(keep((r) => { S.timeline = r; })),
    api('/api/tree', { drive, path }).then(keep((r) => { S.tree = r; })),
    api('/api/hotspots', { drive }).then(keep((r) => { S.hotspots = r; })),
    api('/api/diff', { drive }).then(keep(renderDiff), keep(() => renderDiff(null))),
    api('/api/changes', { drive, kind: 'delete', limit: 200 })
      .then(keep(renderChanges), keep(() => renderChanges(null))),
  ];
  const results = await Promise.allSettled(jobs);
  if (!isCurrentLoad(token)) return;

  const failed = results.filter((r) => r.status === 'rejected');
  if (failed.length) banner({ text: failed[0].reason.message });

  // 上面已经用旧数据画过一次(先出东西比等着好),status 回来之后再画准的那一版
  renderDriveTabs();
  renderBaseline();
  renderTimeline();
  renderLegend();
  renderCrumbs();
  drawTreemap(false);
  renderHotspots();
}

function selectDrive(drive) {
  if (drive === S.drive) return;
  loadDrive(drive);
}

/* 横幅。传文案的键(切语言时能重画),或者 {text} 包一层原文
 * (后端抛上来的报错,没法翻)。
 *
 * 存下最后一条:切语言时如果不重画,这行会一直停在旧语言上 —— 而它是
 * 整屏最长的一段字,停在那儿最显眼。 */
function banner(spec, vars) {
  const box = el('banner');
  if (!box) return;
  S.banner = typeof spec === 'string' ? { key: spec, vars: vars } : spec;
  paintBanner();
}

function paintBanner() {
  const box = el('banner');
  if (!box) return;
  clear(box);
  if (!S.banner) { box.hidden = true; return; }
  box.hidden = false;
  const b = S.banner;
  box.appendChild(tag('span', null, b.key ? t(b.key, b.vars) : b.text));
  const close = tag('button', { class: 'banner-close', 'aria-label': t('ctx.close') }, '×');
  // 关掉就是关掉:清空记录,别在切语言时又冒出来
  close.addEventListener('click', () => { S.banner = null; box.hidden = true; });
  box.appendChild(close);
}

/* ---- 右键菜单 ------------------------------------------------------------
 *
 * 一个菜单复用给树图、各个表格和时间轴的归因项。菜单只认「盘内相对路径」,
 * 跟数据库里存的一致;拼成完整路径是后端的事,前端只用来显示。 */

let toastTimer = 0;

function toast(msg, bad) {
  const box = el('toast');
  if (!box) return;
  box.textContent = msg;
  box.className = 'toast' + (bad ? ' bad' : '');
  box.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, bad ? 6000 : 2600);
}

function fullPath(rel) {
  const drive = S.drive || '';
  if (!rel) return drive + '\\';
  return drive + '\\' + rel;
}

function parentOf(rel) {
  if (!rel) return null;
  const i = rel.lastIndexOf('\\');
  return i < 0 ? '' : rel.slice(0, i);
}

const ctx = { rel: null, isDir: true };

function hideCtx() {
  const menu = el('ctxMenu');
  if (menu) menu.hidden = true;
  ctx.rel = null;
}

function showCtx(rel, isDir, ev) {
  const menu = el('ctxMenu');
  if (!menu) return;
  ev.preventDefault();

  ctx.rel = rel;
  ctx.isDir = isDir !== false;

  const label = el('ctxPath');
  if (label) label.textContent = fullPath(rel);

  // 文件没有「进入这一层」;已经在最外层时也没有上一级
  const enter = menu.querySelector('[data-act="enter"]');
  if (enter) enter.hidden = !ctx.isDir || rel === S.path;
  const parent = menu.querySelector('[data-act="parent"]');
  if (parent) parent.hidden = !rel;
  const open = menu.querySelector('[data-act="open"]');
  if (open) open.textContent = ctx.isDir ? t('ctx.open') : t('ctx.locate');

  // 先显示再量尺寸,否则 offsetWidth 是 0
  menu.hidden = false;
  const w = menu.offsetWidth || 220;
  const h = menu.offsetHeight || 140;
  const pad = 6;
  let x = ev.clientX + 2;
  let y = ev.clientY + 2;
  if (x + w > window.innerWidth - pad) x = Math.max(pad, ev.clientX - w - 2);
  if (y + h > window.innerHeight - pad) y = Math.max(pad, ev.clientY - h - 2);
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';

  const first = menu.querySelector('.ctx-item:not([hidden])');
  if (first) first.focus();
}

async function revealPath(rel) {
  try {
    await post('/api/reveal', { drive: S.drive, path: rel || '' });
  } catch (err) {
    toast(err.message, true);
  }
}

async function copyPath(rel) {
  const text = fullPath(rel);
  try {
    await navigator.clipboard.writeText(text);
    toast(t('ctx.copied') + text);
  } catch (err) {
    /* 剪贴板 API 要安全上下文,http://127.0.0.1 算安全上下文,
     * 但用户拒绝授权时仍会失败 —— 退回选中文本让人手动复制。 */
    const label = el('ctxPath');
    if (label && window.getSelection) {
      const range = document.createRange();
      range.selectNodeContents(label);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      toast(t('ctx.copyManual'), true);
    } else {
      toast(t('ctx.copyFailed') + err.message, true);
    }
  }
}

/* 从事件目标往上找最近的匹配元素。
 *
 * ev.target 不一定是元素 —— 可以是 document、文本节点,那些没有 closest()。
 * 直接点上去会抛异常;在收起菜单的那个处理器里抛,菜单就卡在屏幕上关不掉了。
 * 非元素一律当成「没找到」:对关闭逻辑来说,那就是点在菜单外面。 */
function closestEl(target, sel) {
  const node = target instanceof Element
    ? target
    : (target && target.parentElement) || null;      // 文本节点走这条
  return node ? node.closest(sel) : null;
}

function bindCtxMenu() {
  const menu = el('ctxMenu');
  if (!menu) return;

  menu.addEventListener('click', (ev) => {
    const btn = closestEl(ev.target, '.ctx-item');
    if (!btn) return;
    const rel = ctx.rel;
    const isDir = ctx.isDir;
    hideCtx();

    switch (btn.dataset.act) {
      case 'open':
        revealPath(rel);
        break;
      case 'parent': {
        const up = parentOf(rel);
        if (up !== null) revealPath(up);
        break;
      }
      case 'copy':
        copyPath(rel);
        break;
      case 'enter':
        if (isDir) enterPath(rel);
        break;
    }
  });

  // 点别处、滚动、按 Esc 都收起来
  document.addEventListener('mousedown', (ev) => {
    if (!menu.hidden && !closestEl(ev.target, '#ctxMenu')) hideCtx();
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') hideCtx();
  });
  window.addEventListener('blur', hideCtx);
  document.addEventListener('scroll', hideCtx, true);

  // 表格里的路径格:整行右键都算,取那一行的路径格
  document.addEventListener('contextmenu', (ev) => {
    const cell = closestEl(ev.target, 'td.path');
    if (!cell) return;
    const rel = (cell.textContent || '').trim();
    if (!rel) return;
    // 只有名字、没有路径的行不给菜单。这一格的文字是个裸文件名,当成盘内相对
    // 路径拼出来是「D:\那个名字」—— 菜单顶上就印这个,而文件从来没在那儿。
    // 报错也不对:reveal 会说「这个路径已经不在了(可能在上次扫描之后被删了)」,
    // 而真相是从来不知道它在哪。宁可不给菜单,也别给个假位置。
    if (cell.dataset.noloc === '1') return;
    // 清理表和大目录表都是目录;文件明细表标了 data-file
    showCtx(rel, cell.dataset.file !== '1', ev);
  });
}

/* 页面重新可见时把两张按尺寸画的图补一刀。
 *
 * 隐藏期间浏览器不跑渲染步骤,而 ResizeObserver 的派发就挂在那一步上 ——
 * 实测:隐藏状态下新建 observer 并 observe 一个元素,连首次观测都不派发;
 * 期间把宽度从 100 改到 400 也照样不派发(两次计数都是 0)。requestAnimationFrame
 * 同样冻结(600ms 没跑,setTimeout 跑了)。
 *
 * 于是在后台标签页里加载完的页面,两张图都是空的:外壳已经量到 1029px,
 * 时间轴的 viewBox 还是 null,树图的画布位图停在默认的 300。而它俩「量到 0 就
 * 先不画」正是靠 observer 回头再叫一次 —— 那一次在隐藏期间不会来。
 *
 * 按规范 RO 比的是「当前尺寸 vs 上次上报的尺寸」(状态比较,不是回调队列),
 * 隐藏期间的变化不该丢,页面一显示就会补派发。但这条路没法在这儿验证 ——
 * 预览面板没办法弄成可见。而这两张图原来根本不依赖它:viewBox 是死的,整张图
 * 靠 CSS 缩放,窗口一变浏览器自己就重新拉一遍,一行 JS 都不用。这个依赖是
 * 「按像素画」引入的,那就自己补上,不赌规范怎么写。
 *
 * 补多了不要紧:两个渲染函数都是幂等的,宽度没变的话画出来一模一样。 */
function bindVisibilityRepaint() {
  document.addEventListener('visibilitychange', () => {
    // 只管「变可见」这个方向。变隐藏时量不到尺寸,跑一遍是白跑。
    if (document.hidden) return;
    if (S.timeline) renderTimeline();
    // false:不播生长动画。切回标签页不是换数据,方块该待在原地。
    if (S.tree) drawTreemap(false);
  });
}

/* 切语言之后把所有「JS 画出来的东西」重画一遍。
 *
 * I18N.apply() 只管 HTML 里带 data-i18n 的静态节点,画布、时间轴、表格
 * 都是这边拼出来的,它管不着。少调一个,那一块就停在旧语言上。
 * 所以这里按渲染函数列全,而不是靠 apply() 顺带。
 *
 * drawTreemap(false):不播生长动画。切语言不是换数据,方块该待在原地。 */
function repaintAll() {
  paintBanner();
  paintSpanButtons();
  renderDriveTabs();
  renderBaseline();
  if (S.scan) setScanState(S.scan);
  if (S.timeline) { renderTimeline(); renderLegend(); }
  renderCrumbs();
  if (S.tree) { drawTreemap(false); renderTreemapTotals(); }
  if (S.hotspots) renderHotspots();
  renderDiff(S.diff);
  renderChanges(S.changes);
  if (S.schedule) paintSchedule(S.schedule);
  paintSettingsInfo();
}

async function boot() {
  I18N.apply();
  I18N.onChange(repaintAll);
  const langBtn = el('langBtn');
  if (langBtn) {
    langBtn.addEventListener('click', () => {
      I18N.set(I18N.lang === 'zh' ? 'en' : 'zh');
    });
  }

  bindTreemap();
  bindCtxMenu();
  bindTimelineZoom();
  bindVisibilityRepaint();
  const scanBtn = el('scanBtn');
  if (scanBtn) scanBtn.addEventListener('click', startScan);
  const toggle = el('scheduleToggle');
  if (toggle) toggle.addEventListener('change', toggleSchedule);

  try {
    S.status = await api('/api/status');
  } catch (err) {
    banner('err.noBackend', { detail: err.message });
    return;
  }

  S.drives = S.status.drives || [];
  if (!S.drives.length) {
    banner('err.noDrives');
    return;
  }
  if (!(S.status.privileges || {}).is_admin) {
    banner('err.notAdmin');
  }
  paintSettingsInfo();

  const total = S.drives.reduce((s, d) => s + (d.snapshot_count || 0), 0);
  const firstRun = el('firstRun');
  if (firstRun) firstRun.hidden = total > 0;

  const withData = S.drives.find((d) => d.snapshot_count > 0);
  // keepStatus:上面那句 await 刚取过,S.status/S.drives 是这一刻最新的
  await loadDrive((withData || S.drives[0]).drive, { keepStatus: true });
  await loadSchedule();
  await pollScan();
}

document.addEventListener('DOMContentLoaded', boot);
