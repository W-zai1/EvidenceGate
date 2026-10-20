"""
SQLite 数据层 —— 连接、schema、迁移。

为什么换掉 JSON 文件
--------------------
原实现每个岗位一个 `candidates.json`，`update_candidate` 改一个字段也要
反序列化 + 重写整个文件；`talent.aggregate()` 每次请求都全盘扫描所有岗位。
500 位候选人时人才库页面开始卡到秒级，并发写还可能互相覆盖。

`sqlite3` 是标准库——换成它不破坏"零依赖"，却一次性解决：
原子事务、并发读写（WAL）、索引查询、以及"改一个字段只写一行"。

为什么手写迁移而不是引 ORM
--------------------------
数据库演进需要可追溯：`schema_migrations` 表记录每一步，
新库自动跑到最新版本，老库按序补差。这里规模小，手写更透明。

库文件布局
----------
  data/talent.db          主库（PII 字段为密文，见 crypto.py）
  data/.secret_key        本地 PII 密钥（不进 git）

PII 字段（简历正文、测评、转写、联系方式）在**应用层**加解密，
库里永远是 `enc:v1:...` 密文——即使拿到 .db 文件也读不出简历内容。
"""
import json
import os
import sqlite3
import threading
import uuid
from   datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "talent.db")

SCHEMA_VERSION = 10

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

# 需要加密存储的候选人物料字段
PII_CANDIDATE_FIELDS = ("resume_text", "assessment_text", "transcript_text")


class SchemaError(RuntimeError):
    pass


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def new_id(prefix):
    """生成短 id。

    放在 db.py 而不是 store.py，是为了让迁移函数能用它而不用反向 import
    store（那会形成循环依赖：store 依赖 db）。
    """
    return "%s%s" % (prefix, uuid.uuid4().hex[:12])


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def connect():
    """取一个连接。

    每个线程一条连接（sqlite3 的默认 check_same_thread 语义），
    `ThreadingHTTPServer` 下天然线程隔离。行用 Row，便于按列名取值。
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    _local.conn = conn
    return conn


def close_current_thread():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None


class tx:
    """事务上下文：`with tx() as c:` 内要么全成、要么全不成。

    支持嵌套（内层只入计数，不另开事务）。
    """

    def __init__(self, conn=None):
        self.conn = conn or connect()
        self._depth = 0

    def __enter__(self):
        self._depth = getattr(_local, "tx_depth", 0)
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        _local.tx_depth = self._depth + 1
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        _local.tx_depth = self._depth
        if self._depth == 0:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        return False


def exec_script(conn, sql):
    """执行多语句 DDL。

    不用 `conn.executescript()`：它会先隐式 COMMIT，把外层事务提前结束，
    于是迁移失败时无法回滚（曾因此报 "cannot commit - no transaction is active"）。
    这里自己按分号切句，且引号内的分号不切。
    """
    for stmt in split_statements(sql):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)


def split_statements(sql):
    """按顶层分号切 SQL，跳过单引号/双引号/注释内的分号。"""
    out, buf = [], []
    i, n = 0, len(sql)
    quote = None
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                # SQL 里 '' / "" 表示转义，双写不结束
                if i + 1 < n and sql[i + 1] == quote:
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            if j < 0:
                break
            i = j + 1
            buf.append("\n")
            continue
        if ch == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_MIGRATIONS = []


def _migration(version):
    def deco(fn):
        _MIGRATIONS.append((version, fn))
        return fn
    return deco


@_migration(1)
def _m1_initial(conn):
    exec_script(conn, """
    CREATE TABLE users (
        id           TEXT PRIMARY KEY,
        username     TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL DEFAULT '',
        role         TEXT NOT NULL DEFAULT 'hr',
        pw_hash      TEXT NOT NULL,
        salt         TEXT NOT NULL,
        iterations   INTEGER NOT NULL DEFAULT 240000,
        created_at   TEXT NOT NULL,
        last_login   TEXT,
        disabled     INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE sessions (
        token_hash  TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        ip          TEXT DEFAULT '',
        ua          TEXT DEFAULT ''
    );
    CREATE INDEX idx_sessions_user ON sessions(user_id);

    CREATE TABLE jobs (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL DEFAULT '未命名岗位',
        status      TEXT NOT NULL DEFAULT 'draft',
        jd_json     TEXT NOT NULL DEFAULT '{}',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        created_by  TEXT DEFAULT ''
    );
    CREATE INDEX idx_jobs_created ON jobs(created_at DESC);

    CREATE TABLE candidates (
        id              TEXT PRIMARY KEY,
        job_id          TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        person_id       TEXT,
        name            TEXT NOT NULL DEFAULT '未命名候选人',
        identity_json   TEXT NOT NULL DEFAULT '{}',
        identity_state  TEXT NOT NULL DEFAULT 'auto',
        resume_enc      TEXT DEFAULT '',
        assessment_enc  TEXT DEFAULT '',
        transcript_enc  TEXT DEFAULT '',
        status          TEXT NOT NULL DEFAULT 'pending',
        error           TEXT,
        result_json     TEXT,
        interview_json  TEXT,
        rubric_version  TEXT DEFAULT '',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
    CREATE INDEX idx_cand_job ON candidates(job_id);
    CREATE INDEX idx_cand_person ON candidates(person_id);

    CREATE TABLE candidate_files (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
        job_id      TEXT NOT NULL,
        filename    TEXT NOT NULL,
        stored      TEXT NOT NULL,
        role        TEXT NOT NULL DEFAULT 'resume',
        kind        TEXT DEFAULT '',
        chars       INTEGER DEFAULT 0,
        warnings_json TEXT DEFAULT '[]',
        imported_from INTEGER DEFAULT 0,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_files_cand ON candidate_files(candidate_id);

    CREATE TABLE people (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL DEFAULT '',
        name_key    TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'new',
        note        TEXT DEFAULT '',
        tags_json   TEXT DEFAULT '[]',
        contact_enc TEXT DEFAULT '',
        identity_json TEXT NOT NULL DEFAULT '{}',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX idx_people_namekey ON people(name_key);

    CREATE TABLE outcomes (
        id           TEXT PRIMARY KEY,
        person_id    TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        candidate_id TEXT DEFAULT '',
        job_id       TEXT DEFAULT '',
        stage        TEXT NOT NULL,
        reached_at   TEXT NOT NULL,
        recorded_by  TEXT DEFAULT '',
        note         TEXT DEFAULT '',
        created_at   TEXT NOT NULL
    );
    CREATE INDEX idx_outcome_person ON outcomes(person_id);
    CREATE INDEX idx_outcome_job ON outcomes(job_id);

    CREATE TABLE llm_usage (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT NOT NULL,
        provider        TEXT DEFAULT '',
        model           TEXT DEFAULT '',
        role            TEXT DEFAULT '',
        job_id          TEXT DEFAULT '',
        candidate_id    TEXT DEFAULT '',
        prompt_tokens   INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        total_tokens    INTEGER DEFAULT 0,
        latency_ms      INTEGER DEFAULT 0,
        estimated       INTEGER NOT NULL DEFAULT 0,
        ok              INTEGER NOT NULL DEFAULT 1,
        error           TEXT DEFAULT ''
    );
    CREATE INDEX idx_usage_ts ON llm_usage(ts DESC);
    CREATE INDEX idx_usage_job ON llm_usage(job_id);
    CREATE INDEX idx_usage_cand ON llm_usage(candidate_id);

    CREATE TABLE calibration_sets (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        description TEXT DEFAULT '',
        created_at  TEXT NOT NULL
    );

    CREATE TABLE calibration_items (
        id           TEXT PRIMARY KEY,
        set_id       TEXT NOT NULL REFERENCES calibration_sets(id) ON DELETE CASCADE,
        candidate_id TEXT DEFAULT '',
        human_label  TEXT NOT NULL,
        human_score  REAL,
        rubric_version TEXT DEFAULT '',
        ai_total     REAL,
        ai_grade     TEXT,
        ai_scores_json TEXT DEFAULT '{}',
        model        TEXT DEFAULT '',
        recorded_at  TEXT NOT NULL,
        note         TEXT DEFAULT ''
    );
    CREATE INDEX idx_calib_set ON calibration_items(set_id);

    CREATE TABLE audit (
        seq         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        actor       TEXT NOT NULL DEFAULT 'system',
        action      TEXT NOT NULL,
        target_type TEXT DEFAULT '',
        target_id   TEXT DEFAULT '',
        detail_json TEXT DEFAULT '{}',
        prev_hash   TEXT NOT NULL,
        hash        TEXT NOT NULL
    );
    CREATE INDEX idx_audit_action ON audit(action);
    CREATE INDEX idx_audit_target ON audit(target_type, target_id);
    CREATE INDEX idx_audit_ts ON audit(ts DESC);
    """)


@_migration(2)
def _m2_user_settings(conn):
    """模型配置从"进程内全局变量"搬到库里，按用户隔离。

    原来两个 HR 同时用，谁后提交 Key 就用谁的，且无法按岗位区分。
    """
    exec_script(conn, """
    CREATE TABLE user_settings (
        user_id     TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        provider    TEXT DEFAULT '',
        model       TEXT DEFAULT '',
        base_url    TEXT DEFAULT '',
        api_key_enc TEXT DEFAULT '',
        updated_at  TEXT NOT NULL
    );
    """)


@_migration(3)
def _m3_run_locks(conn):
    """评审运行状态落库，替代进程内 _STREAMS 字典。

    进程内状态有两个问题：重复点击无法跨进程互斥；刷新页面就丢事件。
    """
    exec_script(conn, """
    CREATE TABLE runs (
        id           TEXT PRIMARY KEY,
        job_id       TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        status       TEXT NOT NULL DEFAULT 'running',
        candidate_ids_json TEXT DEFAULT '[]',
        started_by   TEXT DEFAULT '',
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        total        INTEGER DEFAULT 0,
        succeeded    INTEGER DEFAULT 0,
        failed       INTEGER DEFAULT 0,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        rubric_version TEXT DEFAULT '',
        job_title    TEXT DEFAULT ''
    );
    CREATE INDEX idx_runs_job ON runs(job_id, started_at DESC);
    CREATE UNIQUE INDEX idx_runs_active ON runs(job_id) WHERE status = 'running';

    CREATE TABLE run_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        seq         INTEGER NOT NULL,
        ts          TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        UNIQUE(run_id, seq)
    );
    CREATE INDEX idx_events_run ON run_events(run_id, seq);
    """)


@_migration(4)
def _m4_identity_merge(conn):
    """身份合并记录：把"这两个候选人是同一人"的判断留痕。

    可解释性要求：事后必须能回答"为什么把 A 和 B 合并了"。
    """
    exec_script(conn, """
    CREATE TABLE identity_decisions (
        id           TEXT PRIMARY KEY,
        person_id    TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        decision     TEXT NOT NULL,
        score        REAL,
        signals_json TEXT DEFAULT '{}',
        decide       TEXT NOT NULL DEFAULT 'auto',
        actor        TEXT DEFAULT '',
        created_at   TEXT NOT NULL
    );
    CREATE INDEX idx_iddec_person ON identity_decisions(person_id);
    CREATE INDEX idx_iddec_cand ON identity_decisions(candidate_id);

    ALTER TABLE people ADD COLUMN merged_into TEXT DEFAULT '';
    ALTER TABLE candidates ADD COLUMN person_decision TEXT DEFAULT '';
    """)


@_migration(5)
def _m5_privacy(conn):
    """隐私生命周期：保留期、导出/删除记录。

    个保法要求"存储期限为实现处理目的所必要的最短时间"，且个人有权
    查阅、复制、删除。这里把"什么时候该删"变成可计算的字段。
    """
    exec_script(conn, """
    ALTER TABLE candidates ADD COLUMN retention_until TEXT DEFAULT '';
    ALTER TABLE people ADD COLUMN retention_until TEXT DEFAULT '';
    ALTER TABLE people ADD COLUMN erased_at TEXT DEFAULT '';
    ALTER TABLE jobs ADD COLUMN retention_days INTEGER DEFAULT 180;

    CREATE TABLE privacy_ops (
        id          TEXT PRIMARY KEY,
        op          TEXT NOT NULL,
        target_type TEXT DEFAULT '',
        target_id   TEXT DEFAULT '',
        actor       TEXT DEFAULT '',
        detail_json TEXT DEFAULT '{}',
        created_at  TEXT NOT NULL
    );
    """)


# ---------------------------------------------------------------------------
# 初始化 / 迁移
# ---------------------------------------------------------------------------
@_migration(6)
def _m6_alignment(conn):
    """回测对齐：记录 HR 的**真实决定**，用来回答"它排的前 N 名，
    有几个是我真会推的"。

    为什么单独建表而不是复用 outcomes：
      · outcomes 记的是"这个人走到了哪一步"（流程进展），
        而且只有进了人才库的人才有；
      · 这里要记的是"标准答案"——这一批里谁被推了面试、谁被拒了，
        包括从未进过流程的人（被拒者恰恰是过滤器最需要被检验的部分）。
    两者语义不同，混用会让命中率算得似是而非。

    decision 取值：
      advance  真被推去面试
      reject   真被拒
      hold     待定/未决定（不计入命中率分母，避免用"还没想好"污染结论）
    """
    exec_script(conn, """
    CREATE TABLE align_runs (
        id            TEXT PRIMARY KEY,
        job_id        TEXT NOT NULL,
        label         TEXT DEFAULT '',
        rubric_version TEXT DEFAULT '',
        source        TEXT DEFAULT '',
        note          TEXT DEFAULT '',
        created_by    TEXT DEFAULT '',
        created_at    TEXT NOT NULL
    );

    CREATE TABLE align_truth (
        id           TEXT PRIMARY KEY,
        align_id     TEXT NOT NULL REFERENCES align_runs(id) ON DELETE CASCADE,
        candidate_id TEXT DEFAULT '',
        person_id    TEXT DEFAULT '',
        name_key     TEXT NOT NULL,
        name_raw     TEXT DEFAULT '',
        decision     TEXT NOT NULL,
        note         TEXT DEFAULT '',
        created_at   TEXT NOT NULL
    );
    CREATE INDEX idx_truth_align ON align_truth(align_id, name_key);
    CREATE UNIQUE INDEX idx_truth_uniq ON align_truth(align_id, name_key);
    """)

    # 候选人级的人工决策与理由。原来 candidates.status 只有机器状态
    # （pending/running/done/failed），没有任何"人做的决定"的落点，
    # 于是"谁、基于什么、推了谁面试"在系统里不存在——
    # 而这恰恰是合规上最需要留下的一条记录。
    exec_script(conn, """
    ALTER TABLE candidates ADD COLUMN decision TEXT DEFAULT '';
    ALTER TABLE candidates ADD COLUMN decision_reason TEXT DEFAULT '';
    ALTER TABLE candidates ADD COLUMN decided_by TEXT DEFAULT '';
    ALTER TABLE candidates ADD COLUMN decided_at TEXT DEFAULT '';
    """)


@_migration(7)
def _m7_evaluations(conn):
    """评审历史：把 `candidates.result_json` 从"当前值"变成"最新值 + 历史"。

    为什么必须补
    ------------
    原来 `update_candidate({"result": res})` 直接 `SET result_json=?`。
    重跑一次，上一次的分数、辩论、裁定全被覆盖——于是：

      · 无法回答"这次比上次高了多少"，而这正是判断"改动有没有用"的唯一依据；
      · 无法验证评审是否可复现——没有历史，就没有第二份数据可对比；
      · 分数被改过也看不出来（审计链只记"谁改了候选人"，不记改前的值）。

    设计取舍
    --------
    · 历史**只追加不覆盖**。删候选人时随 CASCADE 一起清（同一条生命周期的数据）。
    · `candidates.result_json` 保留为"最新一次"的快速读取路径，
      避免每次列表都去 join 历史表做窗口查询（200 人 × N 次会产生明显延迟）。
    · 显式存 `prompt_fingerprint` 与 `repeats`/`stdev`：这两个是把分数
      放到正确语境里所必需的——不知道口径和重复次数，就无法判断分数可不可比。
    """
    exec_script(conn, """
    CREATE TABLE evaluations (
        id                TEXT PRIMARY KEY,
        candidate_id      TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
        job_id            TEXT NOT NULL,
        run_id            TEXT DEFAULT '',
        total             REAL,
        grade             TEXT DEFAULT '',
        rubric_version    TEXT DEFAULT '',
        prompt_fingerprint TEXT DEFAULT '',
        model             TEXT DEFAULT '',
        provider          TEXT DEFAULT '',
        repeats           INTEGER DEFAULT 1,
        stdev             REAL,
        result_json       TEXT,
        created_at        TEXT NOT NULL
    );
    CREATE INDEX idx_eval_cand ON evaluations(candidate_id, created_at);
    CREATE INDEX idx_eval_job  ON evaluations(job_id, created_at);
    CREATE INDEX idx_eval_ver  ON evaluations(rubric_version);
    """)

    # 把已有结果回填成第一条历史记录：否则升级后"历史"是空的，
    # 用户会以为评审从没跑过。
    rows = conn.execute(
        "SELECT id, job_id, result_json, rubric_version, updated_at "
        "FROM candidates WHERE result_json IS NOT NULL AND result_json<>''"
    ).fetchall()
    for r in rows:
        res = jload(r["result_json"], {}) or {}
        if not isinstance(res, dict):
            continue
        conn.execute(
            "INSERT INTO evaluations(id, candidate_id, job_id, run_id, total, grade,"
            " rubric_version, prompt_fingerprint, model, provider, repeats, stdev,"
            " result_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("ev"), r["id"], r["job_id"], "", res.get("total"),
             res.get("grade") or "", r["rubric_version"] or "",
             ((res.get("rubric_parts") or {}).get("prompt_fingerprint") or ""),
             res.get("model") or "", res.get("provider") or "",
             int(((res.get("uncertainty") or {}).get("repeats_ok")) or 1),
             (res.get("uncertainty") or {}).get("stdev"),
             r["result_json"], r["updated_at"] or now_iso()))


# ---------------------------------------------------------------------------
# 初始化 / 迁移
# ---------------------------------------------------------------------------

@_migration(9)
def _m9_cases(conn):
    """领域无关案件层：cases / case_artifacts / case_claims / case_findings /
    case_approvals / gate_results。

    把"一次取证-裁定"从 talent 的 candidates 表抽出来，成为通用主体。
    材料正文在应用层加密（`enc:v1:`），与 crypto.py 同一套机制。
    """
    exec_script(conn, """
    CREATE TABLE cases (
        id              TEXT PRIMARY KEY,
        title           TEXT NOT NULL DEFAULT '',
        profile_key     TEXT NOT NULL,
        profile_version TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'draft',
        gate_json       TEXT DEFAULT '',
        note            TEXT DEFAULT '',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
    CREATE INDEX idx_cases_profile ON cases(profile_key);

    CREATE TABLE case_artifacts (
        id          TEXT PRIMARY KEY,
        case_id     TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
        role        TEXT NOT NULL,
        filename    TEXT DEFAULT '',
        text_enc    TEXT DEFAULT '',
        sha256      TEXT DEFAULT '',
        created_by  TEXT DEFAULT '',
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_art_case ON case_artifacts(case_id);

    CREATE TABLE case_claims (
        id          TEXT PRIMARY KEY,
        case_id     TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
        ord         INTEGER NOT NULL DEFAULT 0,
        text        TEXT NOT NULL,
        stance      TEXT DEFAULT 'support',
        citations   TEXT NOT NULL DEFAULT '[]',
        status      TEXT NOT NULL DEFAULT 'unverified',
        note        TEXT DEFAULT '',
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_claim_case ON case_claims(case_id);

    CREATE TABLE case_findings (
        id          TEXT PRIMARY KEY,
        case_id     TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
        code        TEXT NOT NULL,
        title       TEXT DEFAULT '',
        category    TEXT DEFAULT '',
        status      TEXT DEFAULT 'open',
        detail      TEXT DEFAULT ''
    );
    CREATE INDEX idx_find_case ON case_findings(case_id);

    CREATE TABLE case_approvals (
        id          TEXT PRIMARY KEY,
        case_id     TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
        role        TEXT NOT NULL,
        actor       TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_appr_case ON case_approvals(case_id);

    CREATE TABLE gate_results (
        id          TEXT PRIMARY KEY,
        case_id     TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
        verdict     TEXT NOT NULL,
        result_json TEXT NOT NULL DEFAULT '{}',
        evaluated_at TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_gate_case ON gate_results(case_id);
    """)


@_migration(10)
def _m10_case_declaration(conn):
    """v10：案件声明「是否单机单人部署」。

    单机部署时无法满足「制单/复核/批准由不同人完成」的职责分离。这里不做成
    硬失败（否则单机用户永远出不了结论），而是显式声明降级，并把结论指纹
    单独记为 evidence_digest——导出证据包时一并带走，让看结论的人知道
    「这份结论是在职责分离被声明降级的条件下得出的」，而不是被蒙在鼓里。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(cases)").fetchall()}
    if "single_deploy" not in cols:
        conn.execute("ALTER TABLE cases ADD COLUMN single_deploy INTEGER NOT NULL DEFAULT 0")
    if "evidence_digest" not in cols:
        conn.execute("ALTER TABLE cases ADD COLUMN evidence_digest TEXT DEFAULT ''")
    # 单调递增的修订号。时间戳只到秒，同一秒内「求值完又改材料」用时间比不出
    # 先后，会把过期结论当成有效的导出去。计数器没有这个问题。
    if "revision" not in cols:
        conn.execute("ALTER TABLE cases ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
    if "gate_revision" not in cols:
        conn.execute("ALTER TABLE cases ADD COLUMN gate_revision INTEGER NOT NULL DEFAULT 0")


def _current_version(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not row:
        return 0
    r = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return int(r["v"] or 0)


def init(verbose=False):
    """确保库存在并迁移到最新版本。可重复调用。"""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = connect()
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT DEFAULT ''
        )""")
        cur = _current_version(conn)
        if cur > SCHEMA_VERSION:
            raise SchemaError(
                "数据库版本 %d 高于程序支持的 %d——请升级代码，不要用旧程序打开新库"
                % (cur, SCHEMA_VERSION)
            )
        for version, fn in sorted(_MIGRATIONS):
            if version <= cur:
                continue
            with tx(conn):
                fn(conn)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?,?,?)",
                    (version, now_iso(), fn.__name__),
                )
            if verbose:
                print("[db] 迁移到 v%d（%s）" % (version, fn.__name__))
        _initialized = True


def reset_init_flag():
    """测试辅助：重置"已初始化"标记（切换库路径后调用）。"""
    global _initialized
    _initialized = False


# ---------------------------------------------------------------------------
# JSON 小工具
# ---------------------------------------------------------------------------
def jload(raw, default):
    if not raw:
        return default() if callable(default) else default
    try:
        v = json.loads(raw)
        return v if v is not None else (default() if callable(default) else default)
    except Exception:
        return default() if callable(default) else default


def jdump(obj):
    return json.dumps(obj if obj is not None else {}, ensure_ascii=False)


def table_counts(conn=None):
    conn = conn or connect()
    out = {}
    for t in ("users", "jobs", "candidates", "people", "outcomes",
              "llm_usage", "audit", "runs", "calibration_items"):
        try:
            out[t] = conn.execute("SELECT COUNT(*) AS n FROM %s" % t).fetchone()["n"]
        except sqlite3.Error:
            out[t] = -1
    out["schema_version"] = _current_version(conn)
    out["db_path"] = DB_PATH
    return out
