-- RoundTable Lite 数据库结构（需求文档第 2 节，v2.1）

CREATE TABLE IF NOT EXISTS meeting (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  description   TEXT DEFAULT '',
  status        TEXT DEFAULT 'running',   -- 'running' | 'paused' | 'stopped'
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participant (
  id            TEXT PRIMARY KEY,
  meeting_id    TEXT NOT NULL,
  kind          TEXT NOT NULL,            -- 'human' | 'agent'
  transport     TEXT NOT NULL,            -- 'human' | 'cli' | 'mcp'
  display_name  TEXT NOT NULL,
  cli_cmd       TEXT DEFAULT '',
  cli_args      TEXT DEFAULT '',          -- JSON 数组字符串
  model         TEXT DEFAULT '',
  system_prompt TEXT DEFAULT '',
  trigger_mode  TEXT DEFAULT 'mention',   -- 'mention' | 'always'
  status        TEXT DEFAULT 'idle',      -- 'idle' | 'busy' | 'offline' | 'failed'
  enabled       INTEGER DEFAULT 1,
  joined_at     TEXT NOT NULL,
  last_seen     TEXT,
  UNIQUE(meeting_id, display_name)
);

CREATE TABLE IF NOT EXISTS message (
  id            TEXT PRIMARY KEY,
  meeting_id    TEXT NOT NULL,
  seq           INTEGER NOT NULL,         -- 会议内全局递增，从 1 开始
  author_kind   TEXT NOT NULL,            -- 'human' | 'agent' | 'system'
  author_id     TEXT,
  author_name   TEXT NOT NULL,
  content       TEXT NOT NULL,
  status        TEXT DEFAULT 'done',      -- 'done' | 'streaming' | 'failed'
  error         TEXT DEFAULT '',
  delivery      TEXT DEFAULT 'delivered', -- 'delivered' | 'no_recipients'
  created_at    TEXT NOT NULL
);

-- v2.1：seq 唯一索引——REST / MCP say / 后台接力三路并发写下的顺序基石
CREATE UNIQUE INDEX IF NOT EXISTS uq_msg_meeting_seq ON message(meeting_id, seq);

CREATE TABLE IF NOT EXISTS mention (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id     TEXT NOT NULL,
  participant_id TEXT NOT NULL,
  start_offset   INTEGER NOT NULL,
  length         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mention_message ON mention(message_id);

CREATE TABLE IF NOT EXISTS form (
  id          TEXT PRIMARY KEY,
  meeting_id  TEXT NOT NULL,
  author_id   TEXT,
  author_name TEXT NOT NULL,
  title       TEXT NOT NULL,
  fields      TEXT NOT NULL,   -- JSON 数组：[{key,type,label,required,options}]
  to_target   TEXT DEFAULT 'all',
  status      TEXT DEFAULT 'pending',   -- 'pending' | 'answered' | 'cancelled'
  answer      TEXT DEFAULT '',          -- JSON 对象 {key: value}
  created_at  TEXT NOT NULL,
  answered_at TEXT
);

CREATE TABLE IF NOT EXISTS app_state (
  k TEXT PRIMARY KEY,
  v TEXT
);
