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
  path: '',
  ageFilter: null,     // null = 全部
  fade: new Map(),     // path -> 当前透明度,用于补间
  anim: null,
  scanPoll: null,
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

// 年龄色阶,和 CSS 里的 --age-* 对齐
const AGE_BANDS = [
  { key: 'today',   max: 1,    label: '今天',     color: '#ff5c39' },
  { key: 'week',    max: 7,    label: '本周',     color: '#ff9e2c' },
  { key: 'month',   max: 30,   label: '本月',     color: '#e8c46a' },
  { key: 'quarter', max: 90,   label: '三个月内', color: '#8fbf9f' },
  { key: 'year',    max: 365,  label: '一年内',   color: '#4e93a8' },
  { key: 'older',   max: null, label: '更早',     color: '#2f5d72' },
];

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
  return (n === null || n === undefined) ? '—' : n.toLocaleString('zh-CN');
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtDay(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())}`;
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
  if (d === null) return '未知';
  if (d < 1) return '今天';
  if (d < 2) return '昨天';
  if (d < 30) return Math.round(d) + ' 天前';
  if (d < 365) return Math.round(d / 30) + ' 个月前';
  return (d / 365).toFixed(1) + ' 年前';
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
  const body = await res.json().catch(() => ({ error: '返回的不是 JSON' }));
  if (!res.ok) throw new Error(body.error || `请求失败(${res.status})`);
  return body;
}

async function post(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || body.message || `请求失败(${res.status})`);
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
      title: d.present ? '' : '这个盘现在读不到,显示的是历史数据',
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
    parts.push(['已用', `${fmtBytes(d.live_used_bytes)} / ${fmtBytes(d.live_total_bytes)}`, `${usedPct}%`]);
    parts.push(['可用', fmtBytes(d.live_free_bytes), null]);
  }
  if (snap) {
    parts.push(['扫到', fmtBytes(snap.scanned_bytes), snap.method === 'mft' ? 'MFT' : '目录遍历']);
    parts.push(['文件', fmtCount(snap.file_count), null]);
    parts.push(['快照', `${fmtCount(d.snapshot_count)} 个`, null]);
  }

  for (const [label, value, extra] of parts) {
    const span = tag('span');
    span.appendChild(tag('span', { class: 'dim' }, label + ' '));
    span.appendChild(tag('b', null, value));
    if (extra) span.appendChild(tag('span', { class: 'dim' }, ' ' + extra));
    box.appendChild(span);
  }

  const asOf = tag('span', { style: 'margin-left:auto' });
  asOf.appendChild(tag('span', { class: 'dim' }, snap ? 'as of ' : ''));
  asOf.appendChild(tag('b', null, snap ? fmtTime(snap.taken_at) : '尚无快照'));
  box.appendChild(asOf);
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
      title: `只显示${band.label}写入的部分`,
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
  return raw.map((c) => ({
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
    ctx.fillText('这里还没有数据,先扫一次', W / 2, H / 2);
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
    tip.appendChild(tag('div', { class: 'p' }, it.path || it.name));
    const meta = tag('div', { class: 'm' });
    meta.appendChild(tag('b', null, fmtBytes(it.bytes)));
    if (share) meta.appendChild(document.createTextNode(`  占本层 ${share}`));
    if (it.files) meta.appendChild(document.createTextNode(`  ${fmtCount(it.files)} 个文件`));
    tip.appendChild(meta);
    const age = tag('div', { class: 'm' });
    age.appendChild(document.createTextNode('最近写入 '));
    age.appendChild(tag('b', null, ageText(it.ctime)));
    if (it.ctime) age.appendChild(document.createTextNode('  ' + fmtTime(it.ctime)));
    tip.appendChild(age);
    if (it.isDir) tip.appendChild(tag('div', { class: 'm hint' }, '点击进入'));
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
    const cell = hitTest(ev.clientX - r.left, ev.clientY - r.top);
    if (cell && cell.item.isDir) enterPath(cell.item.path);
  });

  canvas.addEventListener('contextmenu', (ev) => {
    const r = canvas.getBoundingClientRect();
    const cell = hitTest(ev.clientX - r.left, ev.clientY - r.top);
    // 空白处右键当成对当前这一层操作,右键总有反应
    hideTip(el('tmTip'));
    if (cell) showCtx(cell.item.path, cell.item.isDir, ev);
    else showCtx(S.path, true, ev);
  });

  /* 用 ResizeObserver 而不是 window.resize:容器尺寸变化的原因不止窗口缩放
   * (后台标签页首次布局、字体加载后回流、横幅出现挤动布局),
   * 而首绘量到 0 之后必须有人再叫一次,不然树图就一直是空的。 */
  let pending = 0;
  const ro = new ResizeObserver(() => {
    if (pending) return;
    pending = requestAnimationFrame(() => { pending = 0; drawTreemap(false); });
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

function renderCrumbs() {
  const box = el('crumbs');
  clear(box);
  const root = S.drive + '\\';
  const mk = (label, path, active) => {
    const btn = tag('button', { class: active ? 'crumb-current' : 'crumb' }, label);
    if (!active) btn.addEventListener('click', () => enterPath(path));
    box.appendChild(btn);
  };

  const cur = S.path || '';
  mk(root, '', cur === '');
  if (!cur) {
    box.appendChild(tag('span', { class: 'crumb-sep dim' }, '点块进入子目录'));
    return;
  }

  const rel = cur.replace(/^[A-Za-z]:\\?/, '');
  const parts = rel.split('\\').filter(Boolean);
  let acc = S.drive + '\\';
  parts.forEach((p, i) => {
    acc = acc + p + (i < parts.length - 1 ? '\\' : '');
    box.appendChild(tag('span', { class: 'crumb-sep' }, '›'));
    mk(p, acc, i === parts.length - 1);
  });
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
  const W = 1000, H = 260;
  const M = { top: 18, right: 16, bottom: 26, left: 64 };
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;

  host.setAttribute('viewBox', `0 0 ${W} ${H}`);
  host.setAttribute('preserveAspectRatio', 'none');

  /* 斜纹图案,两种 basis 各一份,颜色不同。
   *
   * 回溯层用斜纹、实测层用实心,这个区别不能只写在下面的说明文字里 ——
   * 回溯值是「现存文件的创建日期」,看不到已删除的文件,是净增的下界;
   * 实测值是两次快照相减,包含删除。混在一起画会让人把推断当成测量。 */
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
      const t = svg('text', { x: x + 5, y: M.top - 8, class: 'tl-boundary-label' });
      t.textContent = '↓ 从这里开始是实测';
      g.appendChild(t);
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
   * 「08-25 08-27」会叠在一起糊成一团。 */
  const stride = Math.max(1, Math.ceil(days.length / 12));
  const last = days.length - 1;
  const MIN_LABEL_GAP = 40;                    // 约等于 "08-25" 的字宽加余量
  const crowded = (last % stride) * slot < MIN_LABEL_GAP;
  const gLab = svg('g');
  days.forEach((d, i) => {
    if (i !== last) {
      if (i % stride !== 0) return;
      // 挤到最后一格的那个刻度直接不画
      if (crowded && i + stride > last) return;
    }
    const t = svg('text', {
      x: (M.left + i * slot + slot / 2).toFixed(2),
      y: H - 8, class: 'tl-day', 'text-anchor': 'middle',
    });
    t.textContent = d.day.slice(5);
    gLab.appendChild(t);
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
      ? `全部 ${total} 天`
      : `${days[from].day} → ${days[to - 1].day}  共 ${span} 天`;
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

  // 从鼠标位置反推它落在哪一天(days 数组的下标)
  const dayAt = (clientX) => {
    const tl = S.timeline;
    if (!tl || !tl.days || !tl.days.length) return null;
    const r = plot.getBoundingClientRect();
    if (!r.width) return null;
    const [from, to] = tlWindow();
    // viewBox 是 0..1000,左右留白按同一比例换算
    const M_LEFT = 64, M_RIGHT = 16, W = 1000;
    const vx = ((clientX - r.left) / r.width) * W;
    const frac = (vx - M_LEFT) / (W - M_LEFT - M_RIGHT);
    const idx = from + frac * (to - from);
    return Math.max(from, Math.min(to - 1, Math.round(idx)));
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
    // 拖过去多少像素,换算成多少天
    const perDay = r.width * ((1000 - 64 - 16) / 1000) / drag.span;
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
}

function showDayTip(d, ev) {
  const tip = el('tlTip');
  clear(tip);
  const retro = d.basis === 'retro';

  const head = tag('div', { class: 'p' });
  head.appendChild(document.createTextNode(d.day + '  '));
  head.appendChild(tag('span', { class: 'badge ' + (retro ? 'retro' : 'measured') },
    retro ? '回溯' : '实测'));
  tip.appendChild(head);

  const m = tag('div', { class: 'm' });
  m.appendChild(document.createTextNode('新增 '));
  m.appendChild(tag('b', null, fmtBytes(d.added || 0)));
  if (d.removed) {
    m.appendChild(document.createTextNode('  减少 '));
    m.appendChild(tag('b', null, fmtBytes(d.removed)));
  }
  m.appendChild(document.createTextNode('  净 '));
  m.appendChild(tag('b', null, fmtSigned(d.net || 0)));
  tip.appendChild(m);

  if (d.files_added) {
    tip.appendChild(tag('div', { class: 'm' }, `${fmtCount(d.files_added)} 个文件`));
  }

  // USN 能补上「当天删了几个」,这是快照差看不到的
  if (d.usn && (d.usn.deleted || d.usn.created)) {
    const u = tag('div', { class: 'm' });
    u.appendChild(document.createTextNode('变更日志:建 '));
    u.appendChild(tag('b', null, fmtCount(d.usn.created)));
    u.appendChild(document.createTextNode('  删 '));
    u.appendChild(tag('b', null, fmtCount(d.usn.deleted)));
    tip.appendChild(u);
  }

  for (const [label, list] of [['增长来自', d.contributors], ['减少来自', d.shrinkers]]) {
    const top = (list || []).slice(0, 4);
    if (!top.length) continue;
    tip.appendChild(tag('div', { class: 'm sub' }, label));
    for (const c of top) {
      const row = tag('div', { class: 'm' });
      row.appendChild(tag('b', null, fmtSigned(c.bytes)));
      row.appendChild(document.createTextNode('  ' + c.path));
      tip.appendChild(row);
    }
  }

  if (retro) {
    tip.appendChild(tag('div', { class: 'm hint' },
      '按现存文件的创建日期推算,当天建了又删的文件看不到,所以这是净增的下界'));
  }
  showTip(tip, ev);
}

function renderTimelineSummary() {
  const box = el('timelineTotals');
  const sum = S.timeline && S.timeline.summary;
  if (box) {
    clear(box);
    if (sum) {
      const net = tag('b', { class: sum.net >= 0 ? 'grow' : 'shrink' }, fmtSigned(sum.net));
      box.appendChild(tag('span', { class: 'dim' }, `${sum.days} 天净变化 `));
      box.appendChild(net);
      box.appendChild(tag('span', { class: 'dim' },
        `  新增 ${fmtBytes(sum.total_added)} · 减少 ${fmtBytes(sum.total_removed)}`));
    }
  }

  // 说明只讲这份数据的实际构成,静态图例已经解释过两种柱子
  const noteBox = el('timelineNotice');
  if (!noteBox) return;
  clear(noteBox);
  if (!sum) return;

  const bits = [];
  if (sum.retro_days) bits.push(`${sum.retro_days} 天回溯`);
  if (sum.measured_days) bits.push(`${sum.measured_days} 天实测`);
  if (sum.busiest_day) bits.push(`涨得最猛是 ${sum.busiest_day}`);

  if (!sum.measured_days) {
    const note = tag('div', { class: 'notice' });
    note.appendChild(tag('b', null, '现在全是回溯值。'));
    note.appendChild(document.createTextNode(
      '只有一次快照时,变化只能从现存文件的创建日期倒推 —— 删掉的东西看不见。'
      + '再扫一次(或者开着每日快照)之后,新的日子就会变成实测。'));
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
  const empty = h.snapshot ? '没有符合条件的项' : '还没有扫描数据';

  // 列顺序跟着 index.html 的表头:目录 / 占用 / 最近写入
  fillRows('bigDirsBody', (h.dirs || []).map((d) => [
    pathCell(d.path, `${fmtCount(d.files)} 个文件`),
    bytesCell(d.bytes),
    { text: ageText(d.newest), attrs: { class: 'num dim' }, title: fmtTime(d.newest) },
  ]), empty);

  fillRows('recentBody', (h.recent || []).map((d) => [
    pathCell(d.path),
    bytesCell(d.bytes),
    { text: ageText(d.newest), attrs: { class: 'num dim' }, title: fmtTime(d.newest) },
  ]), empty);

  // 路径 / 类型 / 占用 / 等级
  const safety = { safe: '可删', review: '先看看', careful: '当心' };
  const cleanup = h.cleanup || [];
  fillRows('cleanupBody', cleanup.map((c) => [
    pathCell(c.path, c.advice),
    { text: c.label, attrs: { class: 'dim' }, title: c.advice },
    bytesCell(c.bytes),
    { node: tag('span', { class: 'tag ' + c.safety }, safety[c.safety] || c.safety) },
  ]), '没找到明显可清的目录');

  const total = el('cleanupTotal');
  if (total) {
    clear(total);
    if (cleanup.length) {
      const safeBytes = cleanup.filter((c) => c.safety === 'safe')
        .reduce((s, c) => s + (c.bytes || 0), 0);
      const allBytes = cleanup.reduce((s, c) => s + (c.bytes || 0), 0);
      total.appendChild(tag('span', { class: 'dim' }, '合计 '));
      total.appendChild(tag('b', null, fmtBytes(allBytes)));
      total.appendChild(tag('span', { class: 'dim' }, `,其中标「可删」 ${fmtBytes(safeBytes)}`));
    }
  }

  const tmTotals = el('treemapTotals');
  if (tmTotals) {
    clear(tmTotals);
    const t = treeTotal();
    if (t) {
      tmTotals.appendChild(tag('span', { class: 'dim' }, '本层 '));
      tmTotals.appendChild(tag('b', null, fmtBytes(t)));
      const n = (S.tree.children || []).length;
      tmTotals.appendChild(tag('span', { class: 'dim' }, `,${n} 项`));
    }
  }
}

function renderDiff(diff) {
  const section = el('diffSection');
  const range = el('diffRange');
  const net = el('diffNet');
  const notice = el('diffNotice');

  if (!diff || !diff.available) {
    // 只有一次快照时整段藏起来:空表格比不显示更让人困惑
    if (section) section.hidden = true;
    return;
  }
  if (section) section.hidden = false;

  if (range) {
    range.textContent = `${fmtTime(diff.before_at)} → ${fmtTime(diff.after_at)}`;
  }
  if (net) {
    clear(net);
    net.appendChild(tag('span', { class: 'dim' }, '净变化 '));
    net.appendChild(tag('b', { class: diff.net >= 0 ? 'grow' : 'shrink' }, fmtSigned(diff.net)));
    net.appendChild(tag('span', { class: 'dim' },
      `  ${fmtBytes(diff.before_bytes)} → ${fmtBytes(diff.after_bytes)}`));
  }
  if (notice) {
    clear(notice);
    for (const c of (diff.caveats || [])) {
      notice.appendChild(tag('p', { class: 'basis-note' }, c));
    }
  }

  const row = (d, cls) => [
    pathCell(d.path, `${fmtBytes(d.before)} → ${fmtBytes(d.after)}`),
    { text: fmtSigned(d.delta), attrs: { class: 'num ' + cls } },
  ];
  fillRows('grewBody', (diff.grew || []).map((d) => row(d, 'grow')), '没有目录变大');
  fillRows('shrankBody', (diff.shrank || []).map((d) => row(d, 'shrink')), '没有目录变小');
}

function renderChanges(ch) {
  const section = el('deletedSection');
  const cov = (ch && ch.coverage) || {};
  const events = (ch && ch.events) || [];
  const deletes = events.filter((e) => e.kind === 'delete');

  // 日志读不到就整段藏掉,别摆一张空表让人以为「什么都没删过」
  if (!cov.events) {
    if (section) section.hidden = true;
    return;
  }
  if (section) section.hidden = false;

  const covBox = el('usnCoverage');
  if (covBox) {
    clear(covBox);
    covBox.appendChild(tag('span', { class: 'dim' }, '日志覆盖 '));
    covBox.appendChild(tag('b', null, `${cov.first_day} 起 ${cov.days} 天`));
    covBox.appendChild(tag('span', { class: 'dim' }, `,${fmtCount(cov.events)} 条`));
  }

  const notice = el('usnNotice');
  if (notice) {
    clear(notice);
    const unknown = deletes.filter((e) => e.bytes === null || e.bytes === undefined).length;
    const note = tag('div', { class: 'notice' });
    if (ch.note) note.appendChild(document.createTextNode(ch.note));
    if (unknown) {
      note.appendChild(document.createTextNode(
        ` 其中 ${fmtCount(unknown)} 条在历史快照里找不到同路径的文件,大小算不出来,这里留空而不是猜。`));
    }
    if (note.childNodes.length) notice.appendChild(note);
  }

  fillRows('deletedBody', deletes.map((e) => [
    // 已经删掉的文件:标成文件,右键菜单里「打开上一级目录」才是能用的那一项
    pathCell(e.path || e.name, null, true),
    { text: (e.bytes === null || e.bytes === undefined) ? '未知' : fmtBytes(e.bytes),
      attrs: { class: 'num ' + ((e.bytes === null || e.bytes === undefined) ? 'dim' : 'shrink') } },
    { text: fmtTime(e.at), attrs: { class: 'num dim' } },
  ]), '这段时间没有记录到删除');
}

// ---- 扫描 ----
function setScanState(state) {
  const btn = el('scanBtn');
  const label = el('scanState');
  const running = !!(state && state.running);
  if (btn) {
    btn.disabled = running;
    btn.classList.toggle('pulse', running);
    btn.textContent = running ? '扫描中……' : '扫描本盘';
  }
  if (!label) return;
  clear(label);
  if (running) {
    const phase = state.phase || '正在扫描';
    label.appendChild(tag('span', null, `${state.drive || ''} ${phase},大盘要几十秒`));
    label.hidden = false;
  } else if (state && state.error) {
    label.appendChild(tag('span', { class: 'bad' }, '上次扫描失败:' + state.error));
    label.hidden = false;
  } else if (state && state.finished_at) {
    const r = state.result || {};
    const bits = ['扫完于 ' + fmtTime(state.finished_at)];
    if (r.method) bits.push(r.method === 'mft' ? 'MFT' : '目录遍历');
    if (r.duration_ms) bits.push((r.duration_ms / 1000).toFixed(1) + ' 秒');
    label.appendChild(tag('span', { class: 'dim' }, bits.join(' · ')));
    label.hidden = false;
    if (r.fallback_reason) {
      label.appendChild(tag('span', { class: 'bad' }, '  退回目录遍历:' + r.fallback_reason));
    }
  } else {
    label.hidden = true;
  }
}

async function pollScan() {
  try {
    const state = await api('/api/scan/state');
    setScanState(state);
    if (state.running) return true;
    if (S.scanPoll) { clearInterval(S.scanPoll); S.scanPoll = null; }
    await loadDrive(S.drive, { keepPath: true });
    return false;
  } catch (err) {
    if (S.scanPoll) { clearInterval(S.scanPoll); S.scanPoll = null; }
    setScanState({ error: err.message });
    return false;
  }
}

async function startScan() {
  try {
    setScanState({ running: true, drive: S.drive });
    await post('/api/scan', { drive: S.drive });
    if (S.scanPoll) clearInterval(S.scanPoll);
    S.scanPoll = setInterval(pollScan, 1200);
  } catch (err) {
    setScanState({ error: err.message });
  }
}

// ---- 计划任务 ----
function paintSchedule(st) {
  const toggle = el('scheduleToggle');
  const label = el('scheduleLabel');
  const detail = el('scheduleDetail');
  const on = !!(st && st.exists && st.enabled);

  if (toggle) toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
  if (label) label.textContent = on ? '每天自动拍一次快照(已开)' : '每天自动拍一次快照';
  if (!detail) return;

  clear(detail);
  if (!st || !st.exists) {
    detail.appendChild(tag('span', { class: 'dim' },
      '开了以后每天自动扫一次。时间轴上的实测数据靠它攒 —— 不开就只有你手动扫的那几天。'));
    return;
  }
  const bits = [];
  if (st.schedule) bits.push(st.schedule);
  if (st.next_run) bits.push('下次 ' + st.next_run);
  if (st.last_run) bits.push('上次 ' + st.last_run);
  if (st.last_result) bits.push('结果 ' + st.last_result);
  detail.appendChild(tag('span', { class: st.enabled ? 'dim' : 'bad' },
    (st.enabled ? '' : '已暂停。') + bits.join(' · ')));
}

function paintSettingsInfo() {
  const box = el('settingsInfo');
  if (!box || !S.status) return;
  clear(box);
  const priv = S.status.privileges || {};
  box.appendChild(tag('b', null, priv.is_admin ? '管理员权限:有。' : '管理员权限:没有。'));
  box.appendChild(document.createTextNode(priv.is_admin
    ? ' 走 MFT + 变更日志,全盘扫描几十秒,删除记录也能看到。'
    : ' 只能退回目录遍历:更慢,而且看不到删除记录。用 strata.bat 启动会自动提权。'));
  box.appendChild(tag('div', { class: 'dim' },
    `数据库 ${S.status.db_path}(${fmtBytes(S.status.db_bytes)})。不联网,不上传。`));
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
async function enterPath(path) {
  S.path = path;
  renderCrumbs();
  try {
    S.tree = await api('/api/tree', { drive: S.drive, path: path || null, depth: 1 });
    S.fade.clear();
    drawTreemap(false);
  } catch (err) {
    banner(err.message);
  }
}

async function loadDrive(drive, opts) {
  S.drive = drive;
  if (!opts || !opts.keepPath) { S.path = ''; S.fade.clear(); }
  // 换盘就复位缩放:C: 上第 40 天的窗口套到 D: 上没有任何意义
  S.tlView = null;
  S.tlAnimated = false;
  renderDriveTabs();
  renderBaseline();

  const jobs = [
    api('/api/timeline', { drive, days: 90 }).then((r) => { S.timeline = r; }),
    api('/api/tree', { drive, path: S.path || null }).then((r) => { S.tree = r; }),
    api('/api/hotspots', { drive }).then((r) => { S.hotspots = r; }),
    api('/api/diff', { drive }).then(renderDiff, () => renderDiff(null)),
    api('/api/changes', { drive, kind: 'delete', limit: 200 })
      .then(renderChanges, () => renderChanges(null)),
  ];
  const results = await Promise.allSettled(jobs);
  const failed = results.filter((r) => r.status === 'rejected');
  if (failed.length) banner(failed[0].reason.message);

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

function banner(msg) {
  const box = el('banner');
  if (!box) return;
  clear(box);
  box.hidden = false;
  box.appendChild(tag('span', null, msg));
  const close = tag('button', { class: 'banner-close', 'aria-label': '关闭' }, '×');
  close.addEventListener('click', () => { box.hidden = true; });
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
  if (open) open.textContent = ctx.isDir ? '在资源管理器打开' : '在资源管理器中定位';

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
    toast('已复制:' + text);
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
      toast('复制不了,已经选中路径,按 Ctrl+C', true);
    } else {
      toast('复制失败:' + err.message, true);
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
    // 清理表和大目录表都是目录;文件明细表标了 data-file
    showCtx(rel, cell.dataset.file !== '1', ev);
  });
}

async function boot() {
  bindTreemap();
  bindCtxMenu();
  bindTimelineZoom();
  const scanBtn = el('scanBtn');
  if (scanBtn) scanBtn.addEventListener('click', startScan);
  const toggle = el('scheduleToggle');
  if (toggle) toggle.addEventListener('change', toggleSchedule);

  try {
    S.status = await api('/api/status');
  } catch (err) {
    banner('连不上后端:' + err.message);
    return;
  }

  S.drives = S.status.drives || [];
  if (!S.drives.length) {
    banner('没有找到可以扫的分区。这个工具需要 NTFS。');
    return;
  }
  if (!(S.status.privileges || {}).is_admin) {
    banner('不是管理员权限:读不了 MFT 和变更日志,只能退回目录遍历 —— 更慢,而且看不到删除记录。用 strata.bat 启动会自动提权。');
  }
  paintSettingsInfo();

  const total = S.drives.reduce((s, d) => s + (d.snapshot_count || 0), 0);
  const firstRun = el('firstRun');
  if (firstRun) firstRun.hidden = total > 0;

  const withData = S.drives.find((d) => d.snapshot_count > 0);
  await loadDrive((withData || S.drives[0]).drive);
  await loadSchedule();
  await pollScan();
}

document.addEventListener('DOMContentLoaded', boot);
