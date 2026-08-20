const fs = require("node:fs");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");

let database;
let openedPath;

function assertRecordId(id) {
  if (typeof id !== "string" || !/^[a-zA-Z0-9_-]{8,80}$/.test(id)) {
    throw new Error("会议记录 ID 无效");
  }
}

function getDatabase(databasePath) {
  if (database && openedPath === databasePath) return database;
  if (database) database.close();
  fs.mkdirSync(path.dirname(databasePath), { recursive: true });
  database = new DatabaseSync(databasePath);
  openedPath = databasePath;
  database.exec(`
    PRAGMA journal_mode = WAL;
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS meetings (
      id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      started_at INTEGER NOT NULL,
      ended_at INTEGER,
       status TEXT NOT NULL,
       scene TEXT,
       runtime_config_json TEXT,
       review_status TEXT,
       review_generated_at INTEGER,
       review_enhanced_at INTEGER,
       review_message TEXT,
       meeting_mode TEXT,
       transcript_mode TEXT,
       transcript_versions_json TEXT,
       audio_path TEXT,
       audio_seconds REAL,
       mic_audio_path TEXT,
       mic_audio_seconds REAL,
       system_audio_path TEXT,
       system_audio_seconds REAL,
       hotwords_status TEXT,
       hotwords_count INTEGER,
       hotwords_vocabulary_id TEXT,
       hotwords_reason TEXT,
       last_error_json TEXT,
       minutes_text TEXT,
      minutes_generated_at INTEGER,
      minutes_source TEXT,
      updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS transcripts (
      meeting_id TEXT NOT NULL,
      id TEXT NOT NULL,
      speaker TEXT NOT NULL,
      speaker_id TEXT,
      text TEXT NOT NULL,
      is_final INTEGER NOT NULL,
      at INTEGER NOT NULL,
      audio_start_ms REAL,
      audio_end_ms REAL,
      PRIMARY KEY (meeting_id, id),
      FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS suggestion_batches (
      meeting_id TEXT NOT NULL,
      id TEXT NOT NULL,
      elapsed REAL NOT NULL,
      at INTEGER NOT NULL,
      hits_json TEXT NOT NULL,
      -- 本批生成失败的原因与模型原始输出（会后要能查清那次为什么没出建议）
      error_json TEXT,
      -- 生成本批建议时实际使用的转写上下文范围；旧批次允许为空
      context_json TEXT,
      -- 实际模型、触发方式、超时阶段等诊断快照；旧批次允许为空
      runtime_json TEXT,
      PRIMARY KEY (meeting_id, id),
      FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS suggestions (
      meeting_id TEXT NOT NULL,
      batch_id TEXT NOT NULL,
      position INTEGER NOT NULL,
      intent TEXT NOT NULL,
      script TEXT NOT NULL,
      grounded INTEGER,
      level TEXT,
      references_json TEXT NOT NULL,
      evidence_json TEXT NOT NULL DEFAULT '[]',
      adopted INTEGER,                -- 用户当时采纳了这条话术
      -- 程序化校验的改判理由与敏感内容提醒（见 poc/suggest.py _validate）。
      -- 会后复盘要能看出"这条当时为什么被降级"，只存最终 level 是不够的。
      notice TEXT,
      sensitive TEXT,
      category TEXT,
      PRIMARY KEY (meeting_id, batch_id, position),
      FOREIGN KEY (meeting_id, batch_id)
        REFERENCES suggestion_batches(meeting_id, id) ON DELETE CASCADE
    );

    -- 会议的说话人档案（改名/标记我）。此前只落在转写文本里（焊死、无法再改）；
    -- 独立成表后，历史回看可重新编辑说话人名称并作用于全部发言。
    CREATE TABLE IF NOT EXISTS meeting_speakers (
      meeting_id TEXT NOT NULL,
      speaker_id TEXT NOT NULL,
      name TEXT NOT NULL,
      is_me INTEGER,
      PRIMARY KEY (meeting_id, speaker_id),
      FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS projects (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      note TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );

    -- 文档采用【路径引用】而非拷贝：用户在自己编辑器里改完即刻生效。
    -- 代价是原文件被移走/重命名即失效，因此读取时必须做存在性检测并明示。
    CREATE TABLE IF NOT EXISTS documents (
      id TEXT PRIMARY KEY,
      project_id TEXT,                -- NULL = 公共资料，可被任何项目引用
      name TEXT NOT NULL,
      path TEXT NOT NULL,
      added_at INTEGER NOT NULL,
      FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
    );

    -- 项目↔文档 多对多关联（共享库 + 项目挑选）。
    -- documents 是全局文档库；项目从库里挑选"可用资料"，同一文档可被多个项目引用。
    CREATE TABLE IF NOT EXISTS project_documents (
      project_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      PRIMARY KEY (project_id, document_id),
      FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
      FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
    );

    -- 单场会议最终使用的知识范围【快照】。
    -- 存 path 而非仅 document_id：文档日后被删除或改归属时，
    -- 历史会议仍能说清当时用的是哪些资料，保证建议可回溯。
    CREATE TABLE IF NOT EXISTS meeting_documents (
      meeting_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      name TEXT NOT NULL,
      path TEXT NOT NULL,
      PRIMARY KEY (meeting_id, document_id),
      FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_meetings_started_at
      ON meetings(started_at DESC);
    CREATE INDEX IF NOT EXISTS idx_transcripts_meeting_at
      ON transcripts(meeting_id, at);
    CREATE INDEX IF NOT EXISTS idx_batches_meeting_at
      ON suggestion_batches(meeting_id, at DESC);
    CREATE INDEX IF NOT EXISTS idx_documents_project
      ON documents(project_id);

    -- 专有名词 / ASR 热词。project_id IS NULL = 通用词库；否则绑定到项目。
    -- 开会时合并「通用 + 本场项目」后传给阿里 vocabulary。
    CREATE TABLE IF NOT EXISTS glossary_terms (
      id TEXT PRIMARY KEY,
      term TEXT NOT NULL,
      weight INTEGER NOT NULL DEFAULT 4,
      project_id TEXT,
      note TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_glossary_scope_term
      ON glossary_terms(COALESCE(project_id, ''), term);
    CREATE INDEX IF NOT EXISTS idx_glossary_project
      ON glossary_terms(project_id);

    CREATE TABLE IF NOT EXISTS meeting_memory_items (
      meeting_id TEXT NOT NULL,
      id TEXT NOT NULL,
      kind TEXT NOT NULL,
      status TEXT NOT NULL,
      content TEXT NOT NULL,
      owner TEXT,
      due_at TEXT,
      evidence_transcript_id TEXT,
      evidence_text TEXT,
      source TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (meeting_id, id),
      FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS meeting_glossary_candidates (
      meeting_id TEXT NOT NULL,
      id TEXT NOT NULL,
      term TEXT NOT NULL,
      frequency INTEGER NOT NULL DEFAULT 1,
      weight INTEGER NOT NULL DEFAULT 3,
      sample_context TEXT NOT NULL DEFAULT '',
      reason TEXT NOT NULL DEFAULT '',
      source TEXT NOT NULL DEFAULT 'frequency',
      selected INTEGER,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (meeting_id, id),
      UNIQUE (meeting_id, term),
      FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_memory_meeting_status
      ON meeting_memory_items(meeting_id, status, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_glossary_candidates_meeting
      ON meeting_glossary_candidates(meeting_id, updated_at DESC);
  `);

  // meetings 表在项目功能之前就已存在，补列而非重建，避免丢历史数据
  const columns = database.prepare("PRAGMA table_info(meetings)").all();
  if (!columns.some((c) => c.name === "project_id")) {
    database.exec("ALTER TABLE meetings ADD COLUMN project_id TEXT");
  }
  if (!columns.some((c) => c.name === "scene")) {
    database.exec("ALTER TABLE meetings ADD COLUMN scene TEXT");
  }
  if (!columns.some((c) => c.name === "runtime_config_json")) {
    database.exec("ALTER TABLE meetings ADD COLUMN runtime_config_json TEXT");
  }
  if (!columns.some((c) => c.name === "review_status")) {
    database.exec("ALTER TABLE meetings ADD COLUMN review_status TEXT");
  }
  if (!columns.some((c) => c.name === "review_generated_at")) {
    database.exec("ALTER TABLE meetings ADD COLUMN review_generated_at INTEGER");
  }
  if (!columns.some((c) => c.name === "review_enhanced_at")) {
    database.exec("ALTER TABLE meetings ADD COLUMN review_enhanced_at INTEGER");
  }
  if (!columns.some((c) => c.name === "review_message")) {
    database.exec("ALTER TABLE meetings ADD COLUMN review_message TEXT");
  }
  if (!columns.some((c) => c.name === "meeting_mode")) {
    database.exec("ALTER TABLE meetings ADD COLUMN meeting_mode TEXT");
  }
  if (!columns.some((c) => c.name === "audio_path")) {
    database.exec("ALTER TABLE meetings ADD COLUMN audio_path TEXT");
  }
  if (!columns.some((c) => c.name === "audio_seconds")) {
    database.exec("ALTER TABLE meetings ADD COLUMN audio_seconds REAL");
  }
  if (!columns.some((c) => c.name === "mic_audio_path")) {
    database.exec("ALTER TABLE meetings ADD COLUMN mic_audio_path TEXT");
  }
  if (!columns.some((c) => c.name === "mic_audio_seconds")) {
    database.exec("ALTER TABLE meetings ADD COLUMN mic_audio_seconds REAL");
  }
  if (!columns.some((c) => c.name === "system_audio_path")) {
    database.exec("ALTER TABLE meetings ADD COLUMN system_audio_path TEXT");
  }
  if (!columns.some((c) => c.name === "system_audio_seconds")) {
    database.exec("ALTER TABLE meetings ADD COLUMN system_audio_seconds REAL");
  }
  if (!columns.some((c) => c.name === "hotwords_status")) {
    database.exec("ALTER TABLE meetings ADD COLUMN hotwords_status TEXT");
  }
  if (!columns.some((c) => c.name === "hotwords_count")) {
    database.exec("ALTER TABLE meetings ADD COLUMN hotwords_count INTEGER");
  }
  if (!columns.some((c) => c.name === "hotwords_vocabulary_id")) {
    database.exec("ALTER TABLE meetings ADD COLUMN hotwords_vocabulary_id TEXT");
  }
  if (!columns.some((c) => c.name === "hotwords_reason")) {
    database.exec("ALTER TABLE meetings ADD COLUMN hotwords_reason TEXT");
  }
  if (!columns.some((c) => c.name === "last_error_json")) {
    database.exec("ALTER TABLE meetings ADD COLUMN last_error_json TEXT");
  }
  if (!columns.some((c) => c.name === "transcript_mode")) {
    database.exec("ALTER TABLE meetings ADD COLUMN transcript_mode TEXT");
  }
  if (!columns.some((c) => c.name === "transcript_versions_json")) {
    database.exec("ALTER TABLE meetings ADD COLUMN transcript_versions_json TEXT");
  }
  if (!columns.some((c) => c.name === "minutes_text")) {
    database.exec("ALTER TABLE meetings ADD COLUMN minutes_text TEXT");
  }
  if (!columns.some((c) => c.name === "minutes_generated_at")) {
    database.exec("ALTER TABLE meetings ADD COLUMN minutes_generated_at INTEGER");
  }
  if (!columns.some((c) => c.name === "minutes_source")) {
    database.exec("ALTER TABLE meetings ADD COLUMN minutes_source TEXT");
  }

  // 转写的录音轴时间：用于历史播放时逐段高亮和点击跳转。
  // 老数据库补列即可；旧记录在渲染层用 at 做近似，重新跑会后分离后会得到准确边界。
  const transcriptCols = database.prepare("PRAGMA table_info(transcripts)").all();
  if (!transcriptCols.some((c) => c.name === "audio_start_ms")) {
    database.exec("ALTER TABLE transcripts ADD COLUMN audio_start_ms REAL");
    database.exec("ALTER TABLE transcripts ADD COLUMN audio_end_ms REAL");
  }

  // suggestions.adopted 补列（表在采纳功能之前已存在）
  const sugCols = database.prepare("PRAGMA table_info(suggestions)").all();
  if (!sugCols.some((c) => c.name === "adopted")) {
    database.exec("ALTER TABLE suggestions ADD COLUMN adopted INTEGER");
  }
  const batchCols = database.prepare("PRAGMA table_info(suggestion_batches)").all();
  if (!batchCols.some((c) => c.name === "error_json")) {
    database.exec("ALTER TABLE suggestion_batches ADD COLUMN error_json TEXT");
  }
  if (!batchCols.some((c) => c.name === "context_json")) {
    database.exec("ALTER TABLE suggestion_batches ADD COLUMN context_json TEXT");
  }
  if (!batchCols.some((c) => c.name === "runtime_json")) {
    database.exec("ALTER TABLE suggestion_batches ADD COLUMN runtime_json TEXT");
  }

  if (!sugCols.some((c) => c.name === "notice")) {
    database.exec("ALTER TABLE suggestions ADD COLUMN notice TEXT");
  }
  if (!sugCols.some((c) => c.name === "sensitive")) {
    database.exec("ALTER TABLE suggestions ADD COLUMN sensitive TEXT");
  }
  if (!sugCols.some((c) => c.name === "category")) {
    database.exec("ALTER TABLE suggestions ADD COLUMN category TEXT");
  }
  if (!sugCols.some((c) => c.name === "evidence_json")) {
    database.exec(
      "ALTER TABLE suggestions ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '[]'",
    );
  }

  // 迁移：把旧的"文档硬绑定项目"(documents.project_id) 转成多对多关联，
  // 使既有项目的文档在新模型下仍是该项目的"可用资料"。幂等。
  database.exec(`
    INSERT OR IGNORE INTO project_documents (project_id, document_id)
    SELECT project_id, id FROM documents WHERE project_id IS NOT NULL
  `);

  return database;
}

function nowId(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random()
    .toString(36)
    .slice(2, 8)}`;
}

function positiveAudioSeconds(value) {
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
}

function isBenignAsrShutdownError(error, meetingStatus) {
  if (meetingStatus !== "completed") return false;
  const stage = String(error?.stage || "");
  const message = String(error?.message || "")
    .replace(/\s+/g, " ")
    .trim();
  return (
    (stage === "bridge" || stage === "asr_stop") &&
    /^\[(?:阿里云|讯飞|火山引擎|腾讯云)\]\s*连接已关闭[。.]?$/.test(message)
  );
}

// ── 项目 ──────────────────────────────────────────────
function listProjects(databasePath) {
  const db = getDatabase(databasePath);
  return db
    .prepare(`
      SELECT p.id, p.name, p.note, p.created_at, p.updated_at,
             (SELECT COUNT(*) FROM project_documents pd WHERE pd.project_id = p.id)
               AS document_count,
             (SELECT COUNT(*) FROM meetings m WHERE m.project_id = p.id)
               AS meeting_count
      FROM projects p ORDER BY p.updated_at DESC
    `)
    .all()
    .map((row) => ({
      id: row.id,
      name: row.name,
      note: row.note || "",
      createdAt: Number(row.created_at),
      updatedAt: Number(row.updated_at),
      documentCount: Number(row.document_count),
      meetingCount: Number(row.meeting_count),
    }));
}

function saveProject(databasePath, project) {
  const db = getDatabase(databasePath);
  const name = String(project?.name || "").trim();
  if (!name) throw new Error("项目名称不能为空");
  const id = project?.id || nowId("proj");
  const now = Date.now();
  db.prepare(`
    INSERT INTO projects (id, name, note, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
      name = excluded.name, note = excluded.note, updated_at = excluded.updated_at
  `).run(id, name, String(project?.note || ""), now, now);
  return listProjects(databasePath).find((p) => p.id === id);
}

function deleteProject(databasePath, id) {
  const db = getDatabase(databasePath);
  db.exec("BEGIN IMMEDIATE");
  try {
    // 会议不级联删除：历史记录必须留存，只解除项目归属
    db.prepare("UPDATE meetings SET project_id = NULL WHERE project_id = ?").run(id);
    // 文档不级联删除：文档属于全局知识库，项目删除只解除归属，保留在知识库中
    db.prepare("UPDATE documents SET project_id = NULL WHERE project_id = ?").run(id);
    // 项目与文档多对多关联解除
    db.prepare("DELETE FROM project_documents WHERE project_id = ?").run(id);
    // 项目专有名词随项目删除（表上有 CASCADE；显式删更稳妥）
    db.prepare("DELETE FROM glossary_terms WHERE project_id = ?").run(id);
    db.prepare("DELETE FROM projects WHERE id = ?").run(id);
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
  return { ok: true };
}

// ── 专有名词 / ASR 热词 ─────────────────────────────────
const GLOSSARY_MAX_TERM_CHARS = 15;
const GLOSSARY_MAX_TERMS_PER_MEETING = 500; // 与阿里 Paraformer 热词上限对齐
const GLOSSARY_WEIGHT_MIN = 1;
const GLOSSARY_WEIGHT_MAX = 5;

function _normalizeGlossaryTerm(raw) {
  return String(raw || "")
    .replace(/\s+/g, " ")
    .trim();
}

function _clampGlossaryWeight(weight) {
  const n = Number(weight);
  if (!Number.isFinite(n)) return 4;
  return Math.max(GLOSSARY_WEIGHT_MIN, Math.min(GLOSSARY_WEIGHT_MAX, Math.round(n)));
}

function _mapGlossaryRow(row) {
  return {
    id: row.id,
    term: row.term,
    weight: Number(row.weight) || 4,
    projectId: row.project_id || null,
    note: row.note || "",
    createdAt: Number(row.created_at),
    updatedAt: Number(row.updated_at),
  };
}

/**
 * 列专有名词。
 *   scope === "general" | null | undefined → 仅通用（project_id IS NULL）
 *   scope === projectId 字符串 → 仅该项目
 *   scope === "all" → 全部
 */
function listGlossaryTerms(databasePath, scope = "general") {
  const db = getDatabase(databasePath);
  let rows;
  if (scope === "all") {
    rows = db
      .prepare(
        `SELECT id, term, weight, project_id, note, created_at, updated_at
         FROM glossary_terms
         ORDER BY CASE WHEN project_id IS NULL THEN 0 ELSE 1 END,
                  updated_at DESC, term COLLATE NOCASE ASC`,
      )
      .all();
  } else if (scope && scope !== "general") {
    rows = db
      .prepare(
        `SELECT id, term, weight, project_id, note, created_at, updated_at
         FROM glossary_terms
         WHERE project_id = ?
         ORDER BY updated_at DESC, term COLLATE NOCASE ASC`,
      )
      .all(String(scope));
  } else {
    rows = db
      .prepare(
        `SELECT id, term, weight, project_id, note, created_at, updated_at
         FROM glossary_terms
         WHERE project_id IS NULL
         ORDER BY updated_at DESC, term COLLATE NOCASE ASC`,
      )
      .all();
  }
  return rows.map(_mapGlossaryRow);
}

function saveGlossaryTerm(databasePath, input = {}) {
  const db = getDatabase(databasePath);
  const term = _normalizeGlossaryTerm(input.term);
  if (!term) throw new Error("专有名词不能为空");
  if ([...term].length > GLOSSARY_MAX_TERM_CHARS) {
    throw new Error(`专有名词最多 ${GLOSSARY_MAX_TERM_CHARS} 个字符`);
  }
  const weight = _clampGlossaryWeight(input.weight);
  const projectId = input.projectId ? String(input.projectId) : null;
  if (projectId) {
    const project = db.prepare("SELECT id FROM projects WHERE id = ?").get(projectId);
    if (!project) throw new Error("项目不存在");
  }
  const note = String(input.note || "").trim().slice(0, 200);
  const now = Date.now();
  const id = input.id ? String(input.id) : nowId("gloss");

  // 同范围去重：通用与项目各自独立；同范围 term 唯一
  const existing = db
    .prepare(
      `SELECT id FROM glossary_terms
       WHERE term = ?
         AND (
           (? IS NULL AND project_id IS NULL)
           OR project_id = ?
         )
       LIMIT 1`,
    )
    .get(term, projectId, projectId);
  if (existing && existing.id !== id) {
    throw new Error(
      projectId ? "该项目下已有相同专有名词" : "通用词库中已有相同专有名词",
    );
  }

  db.prepare(`
    INSERT INTO glossary_terms
      (id, term, weight, project_id, note, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
      term = excluded.term,
      weight = excluded.weight,
      project_id = excluded.project_id,
      note = excluded.note,
      updated_at = excluded.updated_at
  `).run(id, term, weight, projectId, note, now, now);

  return _mapGlossaryRow(
    db
      .prepare(
        `SELECT id, term, weight, project_id, note, created_at, updated_at
         FROM glossary_terms WHERE id = ?`,
      )
      .get(id),
  );
}

function deleteGlossaryTerm(databasePath, id) {
  const db = getDatabase(databasePath);
  db.prepare("DELETE FROM glossary_terms WHERE id = ?").run(String(id || ""));
  return { ok: true };
}

/**
 * 开会用词表：通用 + 指定项目。同名时【项目词优先】（覆盖权重）。
 * 截断到 ASR 上限，优先保留高权重、后更新的。
 */
function listGlossaryTermsForMeeting(databasePath, projectId = null) {
  const general = listGlossaryTerms(databasePath, "general");
  const project = projectId
    ? listGlossaryTerms(databasePath, String(projectId))
    : [];
  const byTerm = new Map();
  for (const item of general) {
    byTerm.set(item.term, { ...item, scope: "general" });
  }
  for (const item of project) {
    byTerm.set(item.term, { ...item, scope: "project" });
  }
  const merged = Array.from(byTerm.values()).sort((a, b) => {
    if (b.weight !== a.weight) return b.weight - a.weight;
    return b.updatedAt - a.updatedAt;
  });
  return merged.slice(0, GLOSSARY_MAX_TERMS_PER_MEETING);
}

// ── 会后复盘：决策 / 待办 / 领域词候选 ─────────────────────
const MEMORY_KINDS = new Set(["decision", "action_item"]);
const MEMORY_STATUSES = new Set(["candidate", "confirmed", "rejected"]);
const MEMORY_SOURCES = new Set(["rule", "model", "user"]);
const CANDIDATE_SOURCES = new Set(["frequency", "title", "document", "asr", "model"]);

function _shortHash(value) {
  let hash = 2166136261;
  for (const char of String(value || "")) {
    hash ^= char.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function _normalizeMemoryContent(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .replace(/[。！？!?；;]+$/u, "")
    .trim()
    .slice(0, 500);
}

function _mapMemoryRow(row) {
  return {
    id: row.id,
    kind: MEMORY_KINDS.has(row.kind) ? row.kind : "action_item",
    status: MEMORY_STATUSES.has(row.status) ? row.status : "candidate",
    content: row.content || "",
    owner: row.owner || null,
    dueAt: row.due_at || null,
    evidenceTranscriptId: row.evidence_transcript_id || null,
    evidenceText: row.evidence_text || null,
    source: MEMORY_SOURCES.has(row.source) ? row.source : "rule",
    createdAt: Number(row.created_at || 0),
    updatedAt: Number(row.updated_at || 0),
  };
}

function listMeetingMemoryItems(databasePath, meetingId) {
  assertRecordId(meetingId);
  return getDatabase(databasePath)
    .prepare(`
      SELECT id, kind, status, content, owner, due_at,
             evidence_transcript_id, evidence_text, source, created_at, updated_at
      FROM meeting_memory_items
      WHERE meeting_id = ?
      ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
               updated_at DESC
    `)
    .all(meetingId)
    .map(_mapMemoryRow);
}

function saveMeetingMemoryItem(databasePath, meetingId, input = {}) {
  assertRecordId(meetingId);
  const db = getDatabase(databasePath);
  const content = _normalizeMemoryContent(input.content);
  if (!content) throw new Error("记忆内容不能为空");
  const kind = MEMORY_KINDS.has(input.kind) ? input.kind : "action_item";
  const status = MEMORY_STATUSES.has(input.status) ? input.status : "candidate";
  const source = MEMORY_SOURCES.has(input.source) ? input.source : "user";
  const now = Date.now();
  const id = String(input.id || `memory-${_shortHash(`${kind}:${content}`)}`);
  const previous = db
    .prepare("SELECT created_at FROM meeting_memory_items WHERE meeting_id = ? AND id = ?")
    .get(meetingId, id);
  db.prepare(`
    INSERT INTO meeting_memory_items
      (meeting_id, id, kind, status, content, owner, due_at,
       evidence_transcript_id, evidence_text, source, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(meeting_id, id) DO UPDATE SET
      kind = excluded.kind,
      status = excluded.status,
      content = excluded.content,
      owner = excluded.owner,
      due_at = excluded.due_at,
      evidence_transcript_id = excluded.evidence_transcript_id,
      evidence_text = excluded.evidence_text,
      source = excluded.source,
      updated_at = excluded.updated_at
  `).run(
    meetingId,
    id,
    kind,
    status,
    content,
    input.owner ? String(input.owner).trim().slice(0, 80) : null,
    input.dueAt ? String(input.dueAt).trim().slice(0, 80) : null,
    input.evidenceTranscriptId ? String(input.evidenceTranscriptId) : null,
    input.evidenceText ? String(input.evidenceText).trim().slice(0, 500) : null,
    source,
    Number(previous?.created_at || input.createdAt || now),
    now,
  );
  return listMeetingMemoryItems(databasePath, meetingId).find((item) => item.id === id);
}

function _mapCandidateRow(row) {
  return {
    id: row.id,
    term: row.term,
    frequency: Math.max(1, Number(row.frequency || 1)),
    weight: _clampGlossaryWeight(row.weight),
    sampleContext: row.sample_context || "",
    reason: row.reason || "",
    source: CANDIDATE_SOURCES.has(row.source) ? row.source : "frequency",
    selected: Boolean(row.selected),
    createdAt: Number(row.created_at || 0),
    updatedAt: Number(row.updated_at || 0),
  };
}

function listGlossaryCandidates(databasePath, meetingId) {
  assertRecordId(meetingId);
  return getDatabase(databasePath)
    .prepare(`
      SELECT id, term, frequency, weight, sample_context, reason, source,
             selected, created_at, updated_at
      FROM meeting_glossary_candidates
      WHERE meeting_id = ?
      ORDER BY selected DESC, frequency DESC, updated_at DESC, term COLLATE NOCASE ASC
    `)
    .all(meetingId)
    .map(_mapCandidateRow);
}

function saveGlossaryCandidates(databasePath, meetingId, candidates = []) {
  assertRecordId(meetingId);
  const db = getDatabase(databasePath);
  const now = Date.now();
  const insert = db.prepare(`
    INSERT INTO meeting_glossary_candidates
      (meeting_id, id, term, frequency, weight, sample_context, reason,
       source, selected, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(meeting_id, id) DO UPDATE SET
      term = excluded.term,
      frequency = excluded.frequency,
      weight = excluded.weight,
      sample_context = excluded.sample_context,
      reason = excluded.reason,
      source = excluded.source,
      selected = excluded.selected,
      updated_at = excluded.updated_at
  `);
  for (const candidate of Array.isArray(candidates) ? candidates : []) {
    const term = _normalizeGlossaryTerm(candidate.term);
    if (!term || [...term].length > GLOSSARY_MAX_TERM_CHARS) continue;
    const id = String(candidate.id || `glossary-candidate-${_shortHash(term)}`);
    insert.run(
      meetingId,
      id,
      term,
      Math.max(1, Math.round(Number(candidate.frequency) || 1)),
      _clampGlossaryWeight(candidate.weight),
      String(candidate.sampleContext || "").trim().slice(0, 300),
      String(candidate.reason || "").trim().slice(0, 200),
      CANDIDATE_SOURCES.has(candidate.source) ? candidate.source : "frequency",
      candidate.selected ? 1 : null,
      Number(candidate.createdAt || now),
      now,
    );
  }
  return listGlossaryCandidates(databasePath, meetingId);
}

function _reviewSentences(record) {
  const rows = [];
  for (const item of Array.isArray(record?.transcript) ? record.transcript : []) {
    if (item?.isFinal === false || !String(item?.text || "").trim()) continue;
    const text = String(item.text).trim();
    const sentences = text.split(/[。！？!?；;\n]+/u).map((part) => part.trim()).filter(Boolean);
    for (const sentence of sentences.length ? sentences : [text]) {
      rows.push({ ...item, text: sentence.slice(0, 500) });
    }
  }
  return rows;
}

function _extractLocalMemoryItems(record) {
  const output = [];
  const seen = new Set();
  const decisions = /(确定|决定|确认|同意|采用|结论|最终方案|定为|敲定)/u;
  const actions = /(待办|需要|负责|跟进|补充|提交|整理|安排|完成|截止|下周|本周|明天|后续|发我|给我)/u;
  for (const row of _reviewSentences(record)) {
    const kind = decisions.test(row.text) ? "decision" : actions.test(row.text) ? "action_item" : null;
    if (!kind) continue;
    const content = _normalizeMemoryContent(row.text);
    const key = `${kind}:${content.toLowerCase()}`;
    if (!content || seen.has(key)) continue;
    seen.add(key);
    const ownerMatch = content.match(/([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z·]{0,10})(?:负责|来跟进|跟进)/u);
    const dueMatch = content.match(/(今天|明天|本周|下周|月底|\d{1,2}[月/-]\d{1,2}[日号]?)/u);
    output.push({
      id: `memory-${_shortHash(`${kind}:${content}`)}`,
      kind,
      status: "candidate",
      content,
      owner: ownerMatch ? ownerMatch[1] : null,
      dueAt: dueMatch ? dueMatch[1] : null,
      evidenceTranscriptId: row.id || null,
      evidenceText: row.text,
      source: "rule",
    });
  }
  return output.slice(0, 80);
}

function _extractGlossaryCandidates(databasePath, record) {
  const sourceTexts = [
    { text: String(record?.title || ""), source: "title" },
    ...(Array.isArray(record?.documents) ? record.documents : [])
      .map((item) => ({ text: String(item?.name || ""), source: "document" }))
      .filter((item) => item.text),
    ..._reviewSentences(record).map((item) => ({ text: item.text, source: "frequency" })),
  ];
  const texts = sourceTexts.map((item) => item.text);
  const fullText = texts.join(" ");
  const counts = new Map();
  const sample = new Map();
  const add = (term, source = "frequency") => {
    const normalized = _normalizeGlossaryTerm(term);
    if (!normalized || [...normalized].length < 2 || [...normalized].length > GLOSSARY_MAX_TERM_CHARS) return;
    const blocked = new Set(["我们", "你们", "这个", "那个", "可以", "需要", "确认", "一下", "现在", "如果", "然后", "就是", "能够", "相关", "具体", "问题", "会议", "需求"]);
    if (blocked.has(normalized)) return;
    const current = counts.get(normalized) || { frequency: 0, source };
    current.frequency += 1;
    const sourcePriority = { frequency: 0, document: 1, title: 2, asr: 2, model: 3 };
    if ((sourcePriority[source] || 0) >= (sourcePriority[current.source] || 0)) {
      current.source = source;
    }
    counts.set(normalized, current);
    if (!sample.has(normalized)) {
      const hit = texts.find((text) => text.toLowerCase().includes(normalized.toLowerCase()));
      sample.set(normalized, String(hit || "").slice(0, 180));
    }
  };
  for (const entry of sourceTexts) {
    for (const match of entry.text.matchAll(/[A-Za-z][A-Za-z0-9+.#_-]{1,30}/g)) {
      add(match[0], entry.source);
    }
    for (const match of entry.text.matchAll(/[\u4e00-\u9fff]{2,10}/gu)) {
      const value = match[0];
      if (value.length <= 8) add(value, entry.source);
    }
  }
  const knownTerms = ["公式定义器", "AI", "Bot", "Skill", "Agent", "低代码", "Webhook", "API", "REST"];
  for (const term of knownTerms) if (fullText.toLowerCase().includes(term.toLowerCase())) add(term, "model");
  const existing = new Set(
    listGlossaryTermsForMeeting(databasePath, record?.projectId || null).map((item) => item.term.toLowerCase()),
  );
  return Array.from(counts.entries())
    .filter(([term, value]) => !existing.has(term.toLowerCase()) && (value.frequency >= 2 || value.source !== "frequency"))
    .sort((a, b) => b[1].frequency - a[1].frequency || a[0].localeCompare(b[0]))
    .slice(0, 80)
    .map(([term, value]) => ({
      id: `glossary-candidate-${_shortHash(term)}`,
      term,
      frequency: value.frequency,
      weight: _clampGlossaryWeight(Math.min(5, 2 + value.frequency)),
      sampleContext: sample.get(term) || "",
      reason:
        value.source === "title"
          ? "会议标题中的领域词"
          : value.source === "document"
            ? "会议资料名称中的领域词"
            : value.source === "model"
              ? "命中重点领域词"
              : "会议中高频出现",
      source: value.source,
    }));
}

function generateMeetingReview(databasePath, meetingId) {
  assertRecordId(meetingId);
  const record = loadMeetingRecord(databasePath, meetingId);
  if (!record) throw new Error("找不到该会议记录");
  const existing = listMeetingMemoryItems(databasePath, meetingId);
  const existingByKey = new Map(existing.map((item) => [`${item.kind}:${item.content.toLowerCase()}`, item]));
  for (const item of _extractLocalMemoryItems(record)) {
    const previous = existingByKey.get(`${item.kind}:${item.content.toLowerCase()}`);
    saveMeetingMemoryItem(databasePath, meetingId, {
      ...item,
      id: previous?.id || item.id,
      status: previous?.status === "confirmed" || previous?.status === "rejected" ? previous.status : "candidate",
      source: previous?.source || item.source,
    });
  }
  const candidates = saveGlossaryCandidates(
    databasePath,
    meetingId,
    _extractGlossaryCandidates(databasePath, record),
  );
  const now = Date.now();
  getDatabase(databasePath).prepare(`
    UPDATE meetings
    SET review_status = 'local', review_generated_at = ?, review_message = NULL, updated_at = ?
    WHERE id = ?
  `).run(now, now, meetingId);
  return loadMeetingRecord(databasePath, meetingId);
}

function markMeetingReview(databasePath, meetingId, status, message = null) {
  assertRecordId(meetingId);
  const allowed = new Set(["pending", "local", "enhanced", "failed"]);
  const value = allowed.has(status) ? status : "failed";
  const now = Date.now();
  getDatabase(databasePath).prepare(`
    UPDATE meetings SET review_status = ?, review_message = ?,
      review_enhanced_at = CASE WHEN ? = 'enhanced' THEN ? ELSE review_enhanced_at END,
      updated_at = ? WHERE id = ?
  `).run(value, message ? String(message).slice(0, 500) : null, value, now, now, meetingId);
  return loadMeetingRecord(databasePath, meetingId);
}

function promoteGlossaryCandidates(databasePath, meetingId, candidateIds = []) {
  assertRecordId(meetingId);
  const record = loadMeetingRecord(databasePath, meetingId);
  if (!record) throw new Error("找不到该会议记录");
  const ids = new Set((Array.isArray(candidateIds) ? candidateIds : []).map(String));
  const candidates = listGlossaryCandidates(databasePath, meetingId).filter((item) => ids.has(item.id));
  const terms = [];
  for (const candidate of candidates) {
    try {
      terms.push(saveGlossaryTerm(databasePath, {
        term: candidate.term,
        weight: candidate.weight,
        note: `来源会议：${record.title}`,
      }));
    } catch (error) {
      if (!String(error?.message || "").includes("已有相同专有名词")) throw error;
      const existing = listGlossaryTerms(databasePath, "general").find(
        (item) => item.term.toLowerCase() === candidate.term.toLowerCase(),
      );
      if (existing) terms.push(existing);
    }
  }
  if (candidates.length) {
    saveGlossaryCandidates(databasePath, meetingId, candidates.map((item) => ({ ...item, selected: true })));
  }
  return terms;
}

// ── 文档（路径引用，共享库 + 项目挑选）─────────────────────
function _mapDocRow(row) {
  // 路径引用的代价：文件可能已被移走/重命名，必须如实反馈而非静默跳过
  let exists = false;
  let size = 0;
  let modifiedAt = 0;
  try {
    const stat = fs.statSync(row.path);
    exists = stat.isFile();
    size = stat.size;
    modifiedAt = stat.mtimeMs;
  } catch {
    exists = false;
  }
  return {
    id: row.id,
    projectId: row.project_id, // 保留：文档的"来源"项目（兼容字段）
    name: row.name,
    path: row.path,
    addedAt: Number(row.added_at),
    exists,
    size,
    modifiedAt,
  };
}

/**
 * 列文档。
 *   projectId === undefined → 全局文档库（知识库页用）
 *   projectId 为字符串     → 该项目挑选的"可用资料"（走 project_documents 关联）
 *   projectId === null     → 未被任何项目引用的库文档
 */
function listDocuments(databasePath, projectId) {
  const db = getDatabase(databasePath);
  let rows;
  if (projectId === undefined) {
    rows = db.prepare("SELECT * FROM documents ORDER BY added_at DESC").all();
  } else if (projectId === null) {
    rows = db
      .prepare(`
        SELECT d.* FROM documents d
        WHERE d.id NOT IN (SELECT document_id FROM project_documents)
        ORDER BY d.added_at DESC
      `)
      .all();
  } else {
    rows = db
      .prepare(`
        SELECT d.* FROM documents d
        JOIN project_documents pd ON pd.document_id = d.id
        WHERE pd.project_id = ?
        ORDER BY d.added_at DESC
      `)
      .all(projectId);
  }
  return rows.map(_mapDocRow);
}

/** 全局文档库（知识库页维护的对象） */
function listLibraryDocuments(databasePath) {
  return listDocuments(databasePath, undefined);
}

/** 某项目当前挑选的文档 id 列表 */
function getProjectDocumentIds(databasePath, projectId) {
  const db = getDatabase(databasePath);
  return db
    .prepare("SELECT document_id FROM project_documents WHERE project_id = ?")
    .all(String(projectId))
    .map((r) => r.document_id);
}

/** 覆盖设置某项目的"可用资料"（全量替换关联） */
function setProjectDocuments(databasePath, projectId, docIds) {
  const db = getDatabase(databasePath);
  db.exec("BEGIN IMMEDIATE");
  try {
    db.prepare("DELETE FROM project_documents WHERE project_id = ?").run(
      String(projectId),
    );
    const link = db.prepare(
      "INSERT OR IGNORE INTO project_documents (project_id, document_id) VALUES (?, ?)",
    );
    for (const id of docIds || []) link.run(String(projectId), String(id));
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
  return { ok: true };
}

/**
 * 导入文档到【全局库】（路径引用）。若给了 projectId，顺带关联到该项目。
 * 去重按全局 path（同一文件不重复登记）。
 */
function addDocuments(databasePath, projectId, paths) {
  const db = getDatabase(databasePath);
  const insert = db.prepare(`
    INSERT INTO documents (id, project_id, name, path, added_at)
    VALUES (?, ?, ?, ?, ?)
  `);
  const link = db.prepare(
    "INSERT OR IGNORE INTO project_documents (project_id, document_id) VALUES (?, ?)",
  );
  const byPath = new Map(
    db.prepare("SELECT id, path FROM documents").all().map((r) => [
      r.path.toLowerCase(),
      r.id,
    ]),
  );

  let added = 0;
  for (const raw of paths || []) {
    const filePath = String(raw);
    if (!/\.(md|txt|docx|pdf)$/i.test(filePath)) continue;
    let docId = byPath.get(filePath.toLowerCase());
    if (!docId) {
      docId = nowId("doc");
      insert.run(docId, projectId || null, path.basename(filePath), filePath, Date.now());
      byPath.set(filePath.toLowerCase(), docId);
      added += 1;
    }
    if (projectId) link.run(String(projectId), docId);
  }
  return {
    added,
    documents:
      projectId === undefined
        ? listLibraryDocuments(databasePath)
        : listDocuments(databasePath, projectId ?? null),
  };
}

function renameDocument(databasePath, id, name) {
  const db = getDatabase(databasePath);
  const trimmed = String(name || "").trim();
  if (!trimmed) throw new Error("文档名称不能为空");
  // 只改显示名，不动磁盘上的原文件
  db.prepare("UPDATE documents SET name = ? WHERE id = ?").run(trimmed, String(id));
  return { ok: true };
}

/** 清空数据前必须先关闭连接，否则 Windows 上文件被占用删不掉 */
function closeDatabase() {
  if (database) {
    database.close();
    database = undefined;
    openedPath = undefined;
  }
}

function removeDocument(databasePath, id) {
  const db = getDatabase(databasePath);
  db.prepare("DELETE FROM documents WHERE id = ?").run(String(id));
  return { ok: true };
}

// ── 会议的知识范围快照 ──────────────────────────────────
function saveMeetingDocuments(databasePath, meetingId, documents) {
  assertRecordId(meetingId);
  const db = getDatabase(databasePath);
  db.exec("BEGIN IMMEDIATE");
  try {
    db.prepare("DELETE FROM meeting_documents WHERE meeting_id = ?").run(meetingId);
    const insert = db.prepare(`
      INSERT INTO meeting_documents (meeting_id, document_id, name, path)
      VALUES (?, ?, ?, ?)
    `);
    for (const doc of documents || []) {
      insert.run(meetingId, String(doc.id), String(doc.name), String(doc.path));
    }
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
  return { ok: true };
}

function loadMeetingDocuments(databasePath, meetingId) {
  const db = getDatabase(databasePath);
  return db
    .prepare(`
      SELECT document_id, name, path FROM meeting_documents
      WHERE meeting_id = ? ORDER BY name ASC
    `)
    .all(meetingId)
    .map((row) => ({ id: row.document_id, name: row.name, path: row.path }));
}

function normalizeRecord(record) {
  assertRecordId(record?.id);
  const scene = ["general", "sales", "requirements"].includes(record.scene)
    ? record.scene
    : "general";
  const rawRuntime = record.runtimeConfig && typeof record.runtimeConfig === "object"
    ? record.runtimeConfig
    : null;
  const runtimeConfig = rawRuntime
    ? {
        provider: String(rawRuntime.provider || ""),
        model: String(rawRuntime.model || ""),
        asrProvider: String(rawRuntime.asrProvider || ""),
        asrLang: String(rawRuntime.asrLang || "zh_en"),
        timeoutSeconds: Number(rawRuntime.timeoutSeconds || 12),
        suggestionCount: Number(rawRuntime.suggestionCount || 3),
        silenceSeconds: Number(rawRuntime.silenceSeconds || 3),
        glossaryStatus: String(rawRuntime.glossaryStatus || "unknown"),
        glossaryCount: Math.max(0, Number(rawRuntime.glossaryCount || 0)),
      }
    : null;
  const transcriptVersion =
    record.transcriptVersion === "offline" ? "offline" : "realtime";
  const transcriptVersions =
    record.transcriptVersions && typeof record.transcriptVersions === "object"
      ? record.transcriptVersions
      : undefined;
  const rawHotwords =
    record.hotwords && typeof record.hotwords === "object"
      ? record.hotwords
      : null;
  const hotwordStatus = [
    "pending",
    "empty",
    "loaded",
    "degraded",
    "unsupported",
  ].includes(rawHotwords?.status)
    ? rawHotwords.status
    : null;
  const hotwords = hotwordStatus
    ? {
        status: hotwordStatus,
        count: Math.max(0, Math.round(Number(rawHotwords.count) || 0)),
        vocabularyId: rawHotwords.vocabularyId
          ? String(rawHotwords.vocabularyId)
          : null,
        reason: rawHotwords.reason ? String(rawHotwords.reason) : null,
      }
    : null;
  const rawLastError =
    record.lastError && typeof record.lastError === "object"
      ? record.lastError
      : null;
  let lastError = null;
  if (rawLastError?.message) {
    lastError = {
      stage: String(rawLastError.stage || "meeting").slice(0, 80),
      message: String(rawLastError.message).slice(0, 500),
      at: Number.isFinite(Number(rawLastError.at))
        ? Number(rawLastError.at)
        : Date.now(),
    };
    for (const key of ["provider", "model", "kind", "timeoutStage", "cause"]) {
      if (rawLastError[key] == null || String(rawLastError[key]).trim() === "") {
        continue;
      }
      lastError[key] = String(rawLastError[key]).slice(0, 160);
    }
    for (const key of ["attempts", "timeoutSeconds"]) {
      if (
        rawLastError[key] == null ||
        !Number.isFinite(Number(rawLastError[key]))
      ) {
        continue;
      }
      lastError[key] = Number(rawLastError[key]);
    }
    if (rawLastError.retryable != null) {
      lastError.retryable = Boolean(rawLastError.retryable);
    }
    if (isBenignAsrShutdownError(lastError, record.status)) {
      lastError = null;
    }
  }
  const normalizeContext = (context) => {
    if (!context || typeof context !== "object") return null;
    const toNumber = (value) => {
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    };
    const wallStartAt = toNumber(context.wallStartAt);
    const wallEndAt = toNumber(context.wallEndAt);
    const audioStartMs = toNumber(context.audioStartMs);
    const audioEndMs = toNumber(context.audioEndMs);
    const hasWall =
      wallStartAt != null && wallEndAt != null && wallEndAt >= wallStartAt;
    const hasAudio =
      audioStartMs != null && audioEndMs != null && audioEndMs > audioStartMs;
    if (!hasWall && !hasAudio) return null;
    return {
      wallStartAt: hasWall ? wallStartAt : null,
      wallEndAt: hasWall ? wallEndAt : null,
      audioStartMs: hasAudio ? audioStartMs : null,
      audioEndMs: hasAudio ? audioEndMs : null,
      approximate: Boolean(context.approximate),
    };
  };
  return {
    id: record.id,
    title: String(record.title || "未命名会议"),
    startedAt: Number(record.startedAt || Date.now()),
    endedAt: record.endedAt ? Number(record.endedAt) : undefined,
    status: ["completed", "interrupted"].includes(record.status)
      ? record.status
      : "active",
    meetingMode: ["in_person", "online"].includes(record.meetingMode)
      ? record.meetingMode
      : null,
    projectId: record.projectId ? String(record.projectId) : null,
    audioPath: record.audioPath ? String(record.audioPath) : null,
    audioSeconds: positiveAudioSeconds(record.audioSeconds),
    micAudioPath: record.micAudioPath ? String(record.micAudioPath) : null,
    micAudioSeconds: positiveAudioSeconds(record.micAudioSeconds),
    systemAudioPath: record.systemAudioPath ? String(record.systemAudioPath) : null,
    systemAudioSeconds: positiveAudioSeconds(record.systemAudioSeconds),
    hotwords,
    lastError,
    transcript: Array.isArray(record.transcript) ? record.transcript : [],
    batches: Array.isArray(record.batches)
      ? record.batches.map((batch) => ({
          ...batch,
          context: normalizeContext(batch?.context),
          runtime:
            batch?.runtime && typeof batch.runtime === "object"
              ? batch.runtime
              : undefined,
        }))
      : [],
    documents: Array.isArray(record.documents) ? record.documents : [],
    speakers: Array.isArray(record.speakers) ? record.speakers : [],
    scene,
    runtimeConfig,
    memoryItems: Array.isArray(record.memoryItems) ? record.memoryItems : [],
    glossaryCandidates: Array.isArray(record.glossaryCandidates)
      ? record.glossaryCandidates
      : [],
    transcriptVersion,
    transcriptVersions,
    minutes:
      record.minutes && typeof record.minutes.content === "string"
        ? {
            content: record.minutes.content,
            generatedAt: Number(record.minutes.generatedAt || Date.now()),
            sourceVersion:
              record.minutes.sourceVersion === "offline" ? "offline" : "realtime",
          }
        : null,
  };
}

function saveMeetingRecord(databasePath, record) {
  const db = getDatabase(databasePath);
  const normalized = normalizeRecord(record);
  db.exec("BEGIN IMMEDIATE");
  try {
    db.prepare(`
      INSERT INTO meetings
        (id, title, started_at, ended_at, status, scene, runtime_config_json,
         meeting_mode, project_id,
         audio_path, audio_seconds, mic_audio_path, mic_audio_seconds,
         system_audio_path, system_audio_seconds,
         hotwords_status, hotwords_count, hotwords_vocabulary_id, hotwords_reason,
         last_error_json,
         transcript_mode, transcript_versions_json,
         minutes_text, minutes_generated_at, minutes_source, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(id) DO UPDATE SET
        title = excluded.title,
         started_at = excluded.started_at,
         ended_at = excluded.ended_at,
        status = excluded.status,
        scene = COALESCE(excluded.scene, meetings.scene),
        runtime_config_json = COALESCE(excluded.runtime_config_json, meetings.runtime_config_json),
         meeting_mode = COALESCE(excluded.meeting_mode, meetings.meeting_mode),
         project_id = excluded.project_id,
        -- 录音信息只在本次带值时覆盖，避免会中自动保存把已录路径清空
         audio_path = COALESCE(excluded.audio_path, meetings.audio_path),
         audio_seconds = COALESCE(excluded.audio_seconds, meetings.audio_seconds),
         mic_audio_path = COALESCE(excluded.mic_audio_path, meetings.mic_audio_path),
         mic_audio_seconds = COALESCE(
           excluded.mic_audio_seconds,
           meetings.mic_audio_seconds
         ),
         system_audio_path = COALESCE(
           excluded.system_audio_path,
           meetings.system_audio_path
         ),
        system_audio_seconds = COALESCE(
          excluded.system_audio_seconds,
          meetings.system_audio_seconds
        ),
        hotwords_status = COALESCE(excluded.hotwords_status, meetings.hotwords_status),
        hotwords_count = COALESCE(excluded.hotwords_count, meetings.hotwords_count),
        hotwords_vocabulary_id = COALESCE(
          excluded.hotwords_vocabulary_id,
          meetings.hotwords_vocabulary_id
        ),
        hotwords_reason = COALESCE(excluded.hotwords_reason, meetings.hotwords_reason),
        last_error_json = COALESCE(excluded.last_error_json, meetings.last_error_json),
        transcript_mode = excluded.transcript_mode,
        transcript_versions_json = COALESCE(
          excluded.transcript_versions_json,
          meetings.transcript_versions_json
        ),
        minutes_text = COALESCE(excluded.minutes_text, meetings.minutes_text),
        minutes_generated_at = COALESCE(
          excluded.minutes_generated_at,
          meetings.minutes_generated_at
        ),
        minutes_source = COALESCE(excluded.minutes_source, meetings.minutes_source),
        updated_at = excluded.updated_at
    `).run(
      normalized.id,
      normalized.title,
      normalized.startedAt,
      normalized.endedAt ?? null,
      normalized.status,
      normalized.scene,
      normalized.runtimeConfig ? JSON.stringify(normalized.runtimeConfig) : null,
      normalized.meetingMode,
      normalized.projectId,
      normalized.audioPath,
      normalized.audioSeconds,
      normalized.micAudioPath,
      normalized.micAudioSeconds,
      normalized.systemAudioPath,
      normalized.systemAudioSeconds,
      normalized.hotwords?.status || null,
      normalized.hotwords?.count ?? null,
      normalized.hotwords?.vocabularyId || null,
      normalized.hotwords?.reason || null,
      normalized.lastError ? JSON.stringify(normalized.lastError) : null,
      normalized.transcriptVersion,
      normalized.transcriptVersions
        ? JSON.stringify(normalized.transcriptVersions)
        : null,
      normalized.minutes?.content || null,
      normalized.minutes?.generatedAt || null,
      normalized.minutes?.sourceVersion || null,
      Date.now(),
    );

    // 知识范围快照：只在本次显式提供时覆写，避免会议中途的自动保存
    // 把开会前定好的范围清空
    if (normalized.documents.length) {
      db.prepare("DELETE FROM meeting_documents WHERE meeting_id = ?").run(
        normalized.id,
      );
      const insertDoc = db.prepare(`
        INSERT INTO meeting_documents (meeting_id, document_id, name, path)
        VALUES (?, ?, ?, ?)
      `);
      for (const doc of normalized.documents) {
        insertDoc.run(
          normalized.id,
          String(doc.id),
          String(doc.name || ""),
          String(doc.path || ""),
        );
      }
    }

    db.prepare("DELETE FROM transcripts WHERE meeting_id = ?").run(normalized.id);
    db.prepare("DELETE FROM suggestion_batches WHERE meeting_id = ?").run(
      normalized.id,
    );

    const insertTranscript = db.prepare(`
      INSERT INTO transcripts
        (meeting_id, id, speaker, speaker_id, text, is_final, at,
         audio_start_ms, audio_end_ms)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    `);
    for (const item of normalized.transcript) {
      insertTranscript.run(
        normalized.id,
        String(item.id),
        String(item.speaker || "未知说话人"),
        item.speakerId == null ? null : String(item.speakerId),
        String(item.text || ""),
        item.isFinal === false ? 0 : 1,
        Number(item.at || normalized.startedAt),
        item.audioStartMs != null && Number.isFinite(Number(item.audioStartMs))
          ? Number(item.audioStartMs)
          : null,
        item.audioEndMs != null && Number.isFinite(Number(item.audioEndMs))
          ? Number(item.audioEndMs)
          : null,
      );
    }

    const insertBatch = db.prepare(`
      INSERT INTO suggestion_batches
        (meeting_id, id, elapsed, at, hits_json, error_json, context_json, runtime_json)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `);
    const insertSuggestion = db.prepare(`
      INSERT INTO suggestions
        (meeting_id, batch_id, position, intent, script, grounded, level,
         references_json, evidence_json, adopted, notice, sensitive, category)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `);
    for (const batch of normalized.batches) {
      const batchId = String(batch.id);
      insertBatch.run(
        normalized.id,
        batchId,
        Number(batch.elapsed || 0),
        Number(batch.at || normalized.startedAt),
        JSON.stringify(Array.isArray(batch.hits) ? batch.hits : []),
        batch.parseError ? JSON.stringify(batch.parseError) : null,
        batch.context ? JSON.stringify(batch.context) : null,
        batch.runtime ? JSON.stringify(batch.runtime) : null,
      );
      (Array.isArray(batch.suggestions) ? batch.suggestions : []).forEach(
        (suggestion, position) => {
          insertSuggestion.run(
            normalized.id,
            batchId,
            position,
            String(suggestion.intent || ""),
            String(suggestion.script || ""),
            suggestion.grounded == null ? null : suggestion.grounded ? 1 : 0,
            suggestion.level || null,
            JSON.stringify(
              Array.isArray(suggestion.references)
                ? suggestion.references
                : [],
            ),
            JSON.stringify(
              Array.isArray(suggestion.evidence) ? suggestion.evidence : [],
            ),
            suggestion.adopted ? 1 : null,
            suggestion.notice || null,
            suggestion.sensitive || null,
            suggestion.category || null,
          );
        },
      );
    }

    // 说话人档案：独立落表，历史回看可再编辑名称（此前只焊死在转写文本里）
    db.prepare("DELETE FROM meeting_speakers WHERE meeting_id = ?").run(
      normalized.id,
    );
    if (normalized.speakers.length) {
      const insertSpeaker = db.prepare(`
        INSERT INTO meeting_speakers (meeting_id, speaker_id, name, is_me)
        VALUES (?, ?, ?, ?)
      `);
      for (const sp of normalized.speakers) {
        if (sp?.id == null) continue;
        insertSpeaker.run(
          normalized.id,
          String(sp.id),
          String(sp.name || ""),
          sp.isMe ? 1 : null,
        );
      }
    }
    // 会后复盘数据按增量 upsert：会中频繁保存会议快照时不能把已确认的记忆清空。
    if (normalized.memoryItems.length) {
      for (const item of normalized.memoryItems) {
        saveMeetingMemoryItem(databasePath, normalized.id, item);
      }
    }
    if (normalized.glossaryCandidates.length) {
      saveGlossaryCandidates(databasePath, normalized.id, normalized.glossaryCandidates);
    }
    db.exec("COMMIT");
    // SQL 的 COALESCE 会保留数据库里已有的纪要、录音和复盘字段；
    // 返回最终回读值，避免渲染层拿着旧快照把这些字段暂时显示成丢失。
    return loadMeetingRecord(databasePath, normalized.id) || normalized;
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
}

/**
 * 把桥接层已经落盘的录音直接关联到会议。
 *
 * 录音文件由主进程按会议 ID 创建，不应只依赖渲染层收到最后一个 ended 事件后
 * 再随整条会议记录保存；窗口刷新或事件丢失时，WAV 仍在但历史页会误报无录音。
 */
function attachMeetingAudio(databasePath, meetingId, audioOrPath, audioSeconds) {
  assertRecordId(meetingId);
  const payload =
    audioOrPath && typeof audioOrPath === "object"
      ? audioOrPath
      : { mixed: { path: audioOrPath, seconds: audioSeconds } };
  const track = (name) => {
    const value = payload[name] || {};
    const seconds = Number(value.seconds);
    return {
      path: value.path ? String(value.path) : null,
      seconds: Number.isFinite(seconds) && seconds > 0 ? seconds : null,
    };
  };
  const mixed = track("mixed");
  const mic = track("mic");
  const system = track("system");
  if (
    !mixed.path && mixed.seconds === null &&
    !mic.path && mic.seconds === null &&
    !system.path && system.seconds === null
  ) return { updated: false };

  const result = getDatabase(databasePath)
    .prepare(`
      UPDATE meetings SET
        audio_path = COALESCE(?, audio_path),
        audio_seconds = COALESCE(?, audio_seconds),
        mic_audio_path = COALESCE(?, mic_audio_path),
        mic_audio_seconds = COALESCE(?, mic_audio_seconds),
        system_audio_path = COALESCE(?, system_audio_path),
        system_audio_seconds = COALESCE(?, system_audio_seconds),
        updated_at = ?
      WHERE id = ?
    `)
    .run(
      mixed.path,
      mixed.seconds,
      mic.path,
      mic.seconds,
      system.path,
      system.seconds,
      Date.now(),
      meetingId,
    );
  return { updated: Number(result.changes || 0) > 0 };
}

function attachMeetingHotwords(databasePath, meetingId, hotwords) {
  assertRecordId(meetingId);
  const value = hotwords && typeof hotwords === "object" ? hotwords : {};
  const status = [
    "pending",
    "empty",
    "loaded",
    "degraded",
    "unsupported",
  ].includes(value.status)
    ? value.status
    : "degraded";
  const count = Math.max(0, Math.round(Number(value.count) || 0));
  const vocabularyId = value.vocabularyId ? String(value.vocabularyId) : null;
  const reason = value.reason ? String(value.reason) : null;
  const result = getDatabase(databasePath)
    .prepare(`
      UPDATE meetings SET
        hotwords_status = ?,
        hotwords_count = ?,
        hotwords_vocabulary_id = ?,
        hotwords_reason = ?,
        updated_at = ?
      WHERE id = ?
    `)
    .run(status, count, vocabularyId, reason, Date.now(), meetingId);
  return { updated: Number(result.changes || 0) > 0 };
}

function attachMeetingError(databasePath, meetingId, error) {
  assertRecordId(meetingId);
  const value = error && typeof error === "object" ? error : {};
  const message = String(value.message || "").trim().slice(0, 500);
  if (!message) return { updated: false };
  const safe = {
    stage: String(value.stage || "meeting").slice(0, 80),
    message,
    at: Number.isFinite(Number(value.at)) ? Number(value.at) : Date.now(),
  };
  for (const key of ["provider", "model", "kind", "timeoutStage", "cause"]) {
    if (value[key] == null || String(value[key]).trim() === "") continue;
    safe[key] = String(value[key]).slice(0, 160);
  }
  for (const key of ["attempts", "timeoutSeconds"]) {
    if (value[key] == null || !Number.isFinite(Number(value[key]))) continue;
    safe[key] = Number(value[key]);
  }
  if (value.retryable != null) safe.retryable = Boolean(value.retryable);
  const result = getDatabase(databasePath)
    .prepare(`
      UPDATE meetings SET
        last_error_json = ?,
        updated_at = ?
      WHERE id = ?
    `)
    .run(JSON.stringify(safe), Date.now(), meetingId);
  return { updated: Number(result.changes || 0) > 0 };
}

function clearMeetingError(databasePath, meetingId, stages = []) {
  assertRecordId(meetingId);
  const current = getDatabase(databasePath)
    .prepare("SELECT last_error_json FROM meetings WHERE id = ?")
    .get(meetingId);
  if (!current?.last_error_json) return { updated: false };
  let parsed;
  try {
    parsed = JSON.parse(current.last_error_json);
  } catch {
    parsed = null;
  }
  const allowed = new Set((Array.isArray(stages) ? stages : [stages]).map(String));
  if (allowed.size && !allowed.has(String(parsed?.stage || ""))) {
    return { updated: false };
  }
  const result = getDatabase(databasePath)
    .prepare(
      "UPDATE meetings SET last_error_json = NULL, updated_at = ? WHERE id = ?",
    )
    .run(Date.now(), meetingId);
  return { updated: Number(result.changes || 0) > 0 };
}

function loadMeetingRecord(databasePath, id) {
  assertRecordId(id);
  const db = getDatabase(databasePath);
  const meeting = db
    .prepare(`
      SELECT m.id, m.title, m.started_at, m.ended_at, m.status, m.scene,
             m.runtime_config_json, m.review_status, m.review_generated_at,
             m.review_enhanced_at, m.review_message, m.meeting_mode, m.project_id,
             m.audio_path, m.audio_seconds,
             m.mic_audio_path, m.mic_audio_seconds,
             m.system_audio_path, m.system_audio_seconds,
             m.hotwords_status, m.hotwords_count,
             m.hotwords_vocabulary_id, m.hotwords_reason,
             m.last_error_json,
             m.transcript_mode,
             m.transcript_versions_json, m.minutes_text,
             m.minutes_generated_at, m.minutes_source,
             p.name AS project_name
      FROM meetings m
      LEFT JOIN projects p ON p.id = m.project_id
      WHERE m.id = ?
    `)
    .get(id);
  if (!meeting) return null;

  const transcript = db
    .prepare(`
      SELECT id, speaker, speaker_id, text, is_final, at,
             audio_start_ms, audio_end_ms
      FROM transcripts WHERE meeting_id = ? ORDER BY at ASC
    `)
    .all(id)
    .map((item) => ({
      id: item.id,
      speaker: item.speaker,
      speakerId: item.speaker_id,
      text: item.text,
      isFinal: Boolean(item.is_final),
      at: Number(item.at),
      audioStartMs:
        item.audio_start_ms == null ? null : Number(item.audio_start_ms),
      audioEndMs:
        item.audio_end_ms == null ? null : Number(item.audio_end_ms),
    }));

  const batches = db
    .prepare(`
      SELECT id, elapsed, at, hits_json, error_json, context_json, runtime_json
      FROM suggestion_batches WHERE meeting_id = ? ORDER BY at DESC
    `)
    .all(id)
    .map((batch) => ({
      id: batch.id,
      elapsed: Number(batch.elapsed),
      at: Number(batch.at),
      hits: JSON.parse(batch.hits_json || "[]"),
      parseError: batch.error_json ? JSON.parse(batch.error_json) : undefined,
      context: batch.context_json ? JSON.parse(batch.context_json) : undefined,
      runtime: batch.runtime_json ? JSON.parse(batch.runtime_json) : undefined,
      suggestions: db
        .prepare(`
          SELECT intent, script, grounded, level, references_json, adopted,
                 evidence_json, notice, sensitive, category
          FROM suggestions
          WHERE meeting_id = ? AND batch_id = ?
          ORDER BY position ASC
        `)
        .all(id, batch.id)
        .map((suggestion) => ({
          intent: suggestion.intent,
          script: suggestion.script,
          grounded:
            suggestion.grounded == null
              ? undefined
              : Boolean(suggestion.grounded),
          level: suggestion.level || undefined,
          references: JSON.parse(suggestion.references_json || "[]"),
          evidence: JSON.parse(suggestion.evidence_json || "[]"),
          adopted: suggestion.adopted ? true : undefined,
          notice: suggestion.notice || undefined,
          sensitive: suggestion.sensitive || undefined,
          category: suggestion.category || undefined,
        })),
    }));

  const speakers = db
    .prepare(`
      SELECT speaker_id, name, is_me FROM meeting_speakers WHERE meeting_id = ?
    `)
    .all(id)
    .map((row) => ({
      id: row.speaker_id,
      name: row.name,
      isMe: Boolean(row.is_me),
      mergedInto: null,
    }));

  const memoryItems = listMeetingMemoryItems(databasePath, id);
  const glossaryCandidates = listGlossaryCandidates(databasePath, id);

  let transcriptVersions;
  try {
    const parsed = JSON.parse(meeting.transcript_versions_json || "null");
    if (parsed && typeof parsed === "object") transcriptVersions = parsed;
  } catch {
    transcriptVersions = undefined;
  }

  let lastError;
  try {
    const parsed = JSON.parse(meeting.last_error_json || "null");
    if (parsed && typeof parsed === "object" && parsed.message) {
      lastError = {
        stage: String(parsed.stage || "meeting"),
        message: String(parsed.message),
        at: Number(parsed.at || 0),
      };
      for (const key of ["provider", "model", "kind", "timeoutStage", "cause"]) {
        if (parsed[key] == null || String(parsed[key]).trim() === "") continue;
        lastError[key] = String(parsed[key]);
      }
      for (const key of ["attempts", "timeoutSeconds"]) {
        if (parsed[key] == null || !Number.isFinite(Number(parsed[key]))) continue;
        lastError[key] = Number(parsed[key]);
      }
      if (parsed.retryable != null) lastError.retryable = Boolean(parsed.retryable);
    }
  } catch {
    lastError = undefined;
  }
  if (isBenignAsrShutdownError(lastError, meeting.status)) {
    lastError = undefined;
  }

  return {
    id: meeting.id,
    title: meeting.title,
    startedAt: Number(meeting.started_at),
    endedAt:
      meeting.ended_at == null ? undefined : Number(meeting.ended_at),
    status: meeting.status,
    scene: ["sales", "requirements"].includes(meeting.scene)
      ? meeting.scene
      : "general",
    runtimeConfig: meeting.runtime_config_json
      ? JSON.parse(meeting.runtime_config_json)
      : null,
    meetingMode:
      meeting.meeting_mode === "online"
        ? "online"
        : meeting.meeting_mode === "in_person"
          ? "in_person"
          : undefined,
    projectId: meeting.project_id || null,
    projectName: meeting.project_name || null,
    audioPath: meeting.audio_path || null,
    audioSeconds: positiveAudioSeconds(meeting.audio_seconds),
    micAudioPath: meeting.mic_audio_path || null,
    micAudioSeconds: positiveAudioSeconds(meeting.mic_audio_seconds),
    systemAudioPath: meeting.system_audio_path || null,
    systemAudioSeconds: positiveAudioSeconds(meeting.system_audio_seconds),
    hotwords: meeting.hotwords_status
      ? {
          status: meeting.hotwords_status,
          count: Math.max(0, Number(meeting.hotwords_count || 0)),
          vocabularyId: meeting.hotwords_vocabulary_id || null,
          reason: meeting.hotwords_reason || null,
        }
      : undefined,
    lastError,
    transcript,
    batches,
    documents: loadMeetingDocuments(databasePath, id),
    speakers,
    memoryItems,
    glossaryCandidates,
    review: meeting.review_status
      ? {
          status: ["pending", "local", "enhanced", "failed"].includes(meeting.review_status)
            ? meeting.review_status
            : "failed",
          generatedAt: Number(meeting.review_generated_at || 0),
          enhancedAt: meeting.review_enhanced_at == null ? null : Number(meeting.review_enhanced_at),
          message: meeting.review_message || null,
          memoryItems,
          glossaryCandidates,
        }
      : null,
    transcriptVersion:
      meeting.transcript_mode === "offline" ? "offline" : "realtime",
    transcriptVersions,
    minutes: meeting.minutes_text
      ? {
          content: meeting.minutes_text,
          generatedAt: Number(meeting.minutes_generated_at || 0),
          sourceVersion:
            meeting.minutes_source === "offline" ? "offline" : "realtime",
        }
      : null,
  };
}

/**
 * 把上次运行遗留的"进行中"会议收尾。
 *
 * ⚠️ 会议是否在进行，取决于 Python 桥接【子进程】是否活着；进程随应用一起
 *    消失，所以应用一启动，库里任何 status='active' 都必然是上次没能正常
 *    结束的残留。此前没有任何机制处理它们，于是这些记录永远显示"进行中"，
 *    既回不去也停不掉（真机验证中留下过一条这样的孤儿记录）。
 *
 * 标为 interrupted 而不是 completed：用户需要知道这场是异常中断的，
 * 里面的转写很可能不完整。结束时间取最后一段转写的时刻，比"现在"诚实。
 */
function finalizeOrphanedMeetings(databasePath) {
  const db = getDatabase(databasePath);
  const orphans = db
    .prepare("SELECT id, started_at FROM meetings WHERE status = 'active'")
    .all();
  const lastAt = db.prepare(
    "SELECT MAX(at) AS last FROM transcripts WHERE meeting_id = ?",
  );
  const update = db.prepare(
    "UPDATE meetings SET status = 'interrupted', ended_at = ? WHERE id = ?",
  );
  for (const row of orphans) {
    const last = lastAt.get(row.id)?.last;
    update.run(Number(last || row.started_at), row.id);
  }
  return { finalized: orphans.length };
}

function listMeetingRecords(databasePath) {
  const db = getDatabase(databasePath);
  return db
    .prepare("SELECT id FROM meetings ORDER BY started_at DESC")
    .all()
    .map((row) => loadMeetingRecord(databasePath, row.id));
}

function deleteMeetingRecords(databasePath, meetingIds) {
  const ids = Array.from(
    new Set(
      (Array.isArray(meetingIds) ? meetingIds : [])
        .map((id) => String(id || "").trim())
        .filter(Boolean),
    ),
  );
  if (!ids.length) return { deleted: 0, audioPaths: [] };

  const db = getDatabase(databasePath);
  const find = db.prepare(
    "SELECT audio_path, mic_audio_path, system_audio_path FROM meetings WHERE id = ?",
  );
  const remove = db.prepare("DELETE FROM meetings WHERE id = ?");
  const audioPaths = [];
  let deleted = 0;

  db.exec("BEGIN IMMEDIATE");
  try {
    for (const id of ids) {
      const row = find.get(id);
      if (!row) continue;
      for (const key of ["audio_path", "mic_audio_path", "system_audio_path"]) {
        if (row[key]) audioPaths.push(String(row[key]));
      }
      deleted += Number(remove.run(id).changes || 0);
    }
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
  return { deleted, audioPaths };
}

module.exports = {
  finalizeOrphanedMeetings,
  listMeetingRecords,
  deleteMeetingRecords,
  loadMeetingRecord,
  saveMeetingRecord,
  attachMeetingAudio,
  attachMeetingHotwords,
  attachMeetingError,
  clearMeetingError,
  listProjects,
  saveProject,
  deleteProject,
  listDocuments,
  listLibraryDocuments,
  getProjectDocumentIds,
  setProjectDocuments,
  addDocuments,
  removeDocument,
  saveMeetingDocuments,
  loadMeetingDocuments,
  renameDocument,
  listGlossaryTerms,
  saveGlossaryTerm,
  deleteGlossaryTerm,
  listGlossaryTermsForMeeting,
  listMeetingMemoryItems,
  saveMeetingMemoryItem,
  listGlossaryCandidates,
  saveGlossaryCandidates,
  generateMeetingReview,
  markMeetingReview,
  promoteGlossaryCandidates,
  closeDatabase,
};
