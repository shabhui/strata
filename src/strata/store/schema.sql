-- Strata 数据库结构
-- 每次扫描写入一个 snapshot,其下挂目录汇总、大文件明细、按日期的归因桶。

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 一次扫描 = 一个盘的一个时间点
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    drive         TEXT    NOT NULL,          -- 'C:'
    taken_at      REAL    NOT NULL,          -- Unix 秒
    method        TEXT    NOT NULL,          -- 'mft' | 'scandir'
    total_bytes   INTEGER NOT NULL,          -- 卷容量
    free_bytes    INTEGER NOT NULL,
    used_bytes    INTEGER NOT NULL,          -- 卷已用(来自系统,权威值)
    scanned_bytes INTEGER NOT NULL,          -- 扫描累计到的占用
    file_count    INTEGER NOT NULL,
    dir_count     INTEGER NOT NULL,
    duration_ms   INTEGER NOT NULL,
    complete      INTEGER NOT NULL DEFAULT 1, -- 扫描是否完整跑完
    note          TEXT
);

CREATE INDEX IF NOT EXISTS idx_snap_drive_time ON snapshots(drive, taken_at);

-- 目录汇总。path 用反斜杠、不含盘符、根目录为空串。
CREATE TABLE IF NOT EXISTS dirs (
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    path            TEXT    NOT NULL,
    depth           INTEGER NOT NULL,
    bytes           INTEGER NOT NULL,   -- 含子目录的总占用
    own_bytes       INTEGER NOT NULL,   -- 仅本目录直属文件
    files           INTEGER NOT NULL,   -- 含子目录
    dirs            INTEGER NOT NULL,   -- 含子目录
    newest_mtime    REAL,               -- 子树内最新修改时间
    newest_ctime    REAL,               -- 子树内最新创建时间
    folded_children INTEGER NOT NULL DEFAULT 0, -- 被折叠掉的子目录数
    folded_bytes    INTEGER NOT NULL DEFAULT 0, -- 被折叠掉的字节
    PRIMARY KEY (snapshot_id, path)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_dirs_snap_bytes ON dirs(snapshot_id, bytes DESC);
CREATE INDEX IF NOT EXISTS idx_dirs_snap_depth ON dirs(snapshot_id, depth);

-- 大文件与近期文件明细
CREATE TABLE IF NOT EXISTS files (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    path        TEXT    NOT NULL,
    bytes       INTEGER NOT NULL,
    mtime       REAL,
    ctime       REAL,
    PRIMARY KEY (snapshot_id, path)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_files_snap_bytes ON files(snapshot_id, bytes DESC);
CREATE INDEX IF NOT EXISTS idx_files_snap_ctime ON files(snapshot_id, ctime);

-- 回溯层:按「文件创建日」把现存字节分桶,归因到路径前 N 段。
-- 只对每个盘最新的快照保留(旧快照的年龄分布没有独立价值)。
CREATE TABLE IF NOT EXISTS age_buckets (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    day         TEXT    NOT NULL,   -- 'YYYY-MM-DD',按本地时区
    attribution TEXT    NOT NULL,   -- 'Program Files\Steam\steamapps','' 表示其他
    bytes       INTEGER NOT NULL,
    files       INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, day, attribution)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_buckets_snap_day ON age_buckets(snapshot_id, day);

-- 实测层:USN 日志事件(删除/新建/重命名),补上时间戳看不到的删除。
CREATE TABLE IF NOT EXISTS usn_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    drive     TEXT    NOT NULL,
    usn       INTEGER NOT NULL,
    timestamp REAL    NOT NULL,
    reason    INTEGER NOT NULL,      -- USN reason 位掩码
    kind      TEXT    NOT NULL,      -- 'create'|'delete'|'rename_old'|'rename_new'|'write'|'other'
    is_dir    INTEGER NOT NULL,
    name      TEXT    NOT NULL,
    path      TEXT,                  -- 能解析到父目录时填完整路径
    bytes     INTEGER,               -- 删除事件通常拿不到大小,可为 NULL
    UNIQUE (drive, usn)
);

CREATE INDEX IF NOT EXISTS idx_usn_drive_time ON usn_events(drive, timestamp);

-- USN 读取游标,记录每个盘读到哪了
CREATE TABLE IF NOT EXISTS usn_cursor (
    drive      TEXT PRIMARY KEY,
    journal_id INTEGER NOT NULL,
    next_usn   INTEGER NOT NULL,
    updated_at REAL    NOT NULL
);
