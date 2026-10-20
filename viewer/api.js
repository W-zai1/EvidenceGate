/* =====================================================================
   viewer/api.js —— 统一数据访问层

   所有后端交互都走这里，app.js 不直接 fetch。
   与旧版的关键差异：

   1. **CSRF**：每个写请求自动带上 X-CSRF-Token（值来自 ts_csrf cookie）。
      后端对这些方法强制校验，不带就是 403。
   2. **401 统一处理**：任何请求返回 401 就广播 auth:expired，由 app.js
      切到登录页。不要在 20 个调用点各写一遍跳转逻辑。
   3. **SSE 断线重放**：EventSource 自带重连，但重连后不会补历史。
      这里记录 lastSeq，重连时带 ?after=，把错过的过程补回来。
      这是"刷新页面能看见刚才跑到哪"的关键。
   ===================================================================== */
(function () {
  'use strict';

  function csrfToken() {
    const m = document.cookie.match(/(?:^|;\s*)ts_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  function emit(name, detail) {
    try {
      window.dispatchEvent(new CustomEvent(name, { detail }));
    } catch (e) { /* 老浏览器忽略 */ }
  }

  async function jsonFetch(url, opts, retried) {
    const o = Object.assign({ credentials: 'same-origin' }, opts || {});
    o.headers = Object.assign({}, o.headers || {});
    const method = (o.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      const t = csrfToken();
      if (t) o.headers['X-CSRF-Token'] = t;
    }

    let res;
    try {
      res = await fetch(url, o);
    } catch (netErr) {
      throw new Error('网络请求失败：' + (netErr.message || netErr));
    }

    let body = null;
    try { body = await res.json(); } catch (e) { body = {}; }

    if (res.status === 401) {
      emit('auth:expired', { url });
      throw new Error((body && body.error) || '未登录或会话已过期');
    }
    if (!res.ok) {
      const err = new Error((body && body.error) || ('HTTP ' + res.status));
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return body;
  }

  const send = (method) => (url, data) => jsonFetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data || {}),
  });
  const post = send('POST');
  const put = send('PUT');

  const enc = encodeURIComponent;
  const qs = (params) => {
    const s = Object.entries(params || {})
      .filter(([, v]) => v !== '' && v != null && v !== false)
      .map(([k, v]) => enc(k) + '=' + enc(v === true ? '1' : v))
      .join('&');
    return s ? '?' + s : '';
  };

  const API = {
    /* ---- 认证 ---- */
    bootstrap:   ()      => jsonFetch('/api/bootstrap'),
    health:      ()      => jsonFetch('/api/health'),
    setup:       (d)     => post('/api/setup', d),
    login:       (u, p)  => post('/api/auth/login', { username: u, password: p }),
    logout:      ()      => post('/api/auth/logout', {}),
    me:          ()      => jsonFetch('/api/auth/me'),
    changePassword: (oldPw, newPw) =>
      post('/api/auth/password', { old_password: oldPw, new_password: newPw }),
    passwordStrength: (pw) => {
      // 前端本地算一份，输入时即时反馈；服务端仍会复核
      const p = pw || '';
      let s = 0;
      if (p.length >= 8) s++;
      if (p.length >= 12) s++;
      if (/[a-z]/.test(p) && /[A-Z]/.test(p)) s++;
      if (/\d/.test(p)) s++;
      if (/[^\w]/.test(p)) s++;
      const labels = ['很弱', '弱', '一般', '较强', '强', '很强'];
      return { score: s, label: labels[Math.min(s, 5)], ok: p.length >= 8 };
    },

    /* ---- 用户 ---- */
    users:        ()      => jsonFetch('/api/users'),
    createUser:   (d)     => post('/api/users', d),
    disableUser:  (id, on) => post('/api/users/' + enc(id) + '/disable', { disabled: !!on }),
    resetPassword: (id, pw) => post('/api/users/' + enc(id) + '/password', { password: pw }),

    /* ---- 岗位 ---- */
    jobs:        ()           => jsonFetch('/api/jobs'),
    job:         (id)         => jsonFetch('/api/jobs/' + enc(id)),
    createJob:   (data)       => post('/api/jobs', data),
    updateJob:   (id, data)   => put('/api/jobs/' + enc(id), data),
    deleteJob:   (id)         => jsonFetch('/api/jobs/' + enc(id), { method: 'DELETE' }),
    structureJd: (jobId, d)   => post('/api/jobs/' + enc(jobId) + '/jd/structure', d),
    parseDocs:   (d)          => post('/api/parse', d),

    /* ---- 候选人 ---- */
    uploadCandidates: (jobId, d) => post('/api/jobs/' + enc(jobId) + '/candidates', d),
    updateCandidate:  (cid, d)   => put('/api/candidates/' + enc(cid), d),
    deleteCandidate:  (cid)      => jsonFetch('/api/candidates/' + enc(cid), { method: 'DELETE' }),
    confirmIdentity:  (cid, isSame, mergeInto) =>
      post('/api/candidates/' + enc(cid) + '/identity',
           { is_same: !!isSame, merge_into_person_id: mergeInto || null }),
    pendingIdentity:  ()        => jsonFetch('/api/identity/pending'),
    compareIdentity:  (d)       => post('/api/identity/compare', d),

    /* ---- 评审 ---- */
    startRun:  (jobId, d) => post('/api/jobs/' + enc(jobId) + '/run', d),
    runState:  (jobId)    => jsonFetch('/api/jobs/' + enc(jobId) + '/run'),
    cancelRun: (rid)      => post('/api/runs/' + enc(rid) + '/cancel', {}),
    runEvents: (rid, after) => jsonFetch('/api/runs/' + enc(rid) + '/events?after=' + (after || 0)),
    estimate:  (jobId, d) => post('/api/jobs/' + enc(jobId) + '/estimate', d),
    generateInterview: (cid) => post('/api/candidates/' + enc(cid) + '/interview', {}),

    /**
     * 订阅评审事件流（带断点续传）。
     *
     * onEvent(ev)  每个事件一次；ev.seq 单调递增
     * onEnd(info)  服务端收尾 / 连接彻底断开后回调一次
     * 返回 close()
     */
    streamRun(runId, onEvent, onEnd, opts) {
      const o = opts || {};
      let lastSeq = o.after || 0;
      let closed = false;
      let es = null;
      let retries = 0;

      const open = () => {
        if (closed) return;
        const url = '/api/runs/' + enc(runId) + '/stream?after=' + lastSeq;
        es = new EventSource(url, { withCredentials: true });

        es.addEventListener('hello', (e) => {
          retries = 0;
          try { onEvent(Object.assign({ type: 'hello' }, JSON.parse(e.data))); } catch (x) {}
        });

        // 服务端给每种事件都写了 event: <type>，所以必须按类型监听，
        // 只挂 onmessage 会收不到（EventSource 不会把命名事件派发给 onmessage）。
        const TYPES = ['run_start', 'candidate_queue', 'candidate_start', 'agent_start',
                       'agent_message', 'judge_scored', 'adjudication', 'candidate_done',
                       'candidate_failed', 'injection_warning', 'run_cancelled',
                       'run_done', 'error', 'cancelling', 'timeout'];
        TYPES.forEach((t) => {
          es.addEventListener(t, (e) => {
            let ev;
            try { ev = JSON.parse(e.data); } catch (x) { return; }
            if (ev.seq && ev.seq > lastSeq) lastSeq = ev.seq;
            if (ev.type === 'run_done' || ev.type === '__end__') {
              closed = true;
              try { es.close(); } catch (x) {}
              onEnd && onEnd(ev);
              return;
            }
            onEvent(ev);
          });
        });

        es.addEventListener('__end__', (e) => {
          let info = {};
          try { info = JSON.parse(e.data); } catch (x) {}
          if (info.cursor && info.cursor > lastSeq) lastSeq = info.cursor;
          closed = true;
          try { es.close(); } catch (x) {}
          onEnd && onEnd(info);
        });

        es.onerror = () => {
          if (closed) return;
          // EventSource 自己会重连，但我们希望重连时带上 after=lastSeq。
          // 浏览器重连用的还是原 URL，所以这里主动关掉并重建。
          try { es.close(); } catch (x) {}
          retries++;
          if (retries > 12) {
            closed = true;
            onEnd && onEnd({ type: '__end__', aborted: true, cursor: lastSeq,
                             message: '事件流多次重连失败，请刷新页面查看结果' });
            return;
          }
          setTimeout(open, Math.min(1000 * retries, 5000));
        };
      };

      open();
      return () => { closed = true; try { es.close(); } catch (x) {} };
    },

    /* ---- 模型设置 ---- */
    modelConfig:  ()  => jsonFetch('/api/model/config'),
    setModelConfig: (cfg) => post('/api/model/config', cfg),
    probe:        (d) => post('/api/model/probe', d),
    workbuddy:    ()  => jsonFetch('/api/model/workbuddy'),

    /* ---- 人才库 ---- */
    talent:      (params) => jsonFetch('/api/talent' + qs(params)),
    talentPerson: (pid)   => jsonFetch('/api/talent/' + enc(pid)),
    updateTalent: (pid, d) => put('/api/talent/' + enc(pid), d),
    deleteTalent: (pid)   => jsonFetch('/api/talent/' + enc(pid), { method: 'DELETE' }),
    importTalent: (pid, jobId) =>
      post('/api/talent/' + enc(pid) + '/import', { job_id: jobId }),

    /* ---- 结果回填 ---- */
    outcomes:     (params) => jsonFetch('/api/outcomes' + qs(params)),
    recordOutcome: (pid, d) => post('/api/talent/' + enc(pid) + '/outcome', d),
    deleteOutcome: (oid)   => jsonFetch('/api/outcomes/' + enc(oid), { method: 'DELETE' }),
    hitRate:      (params) => jsonFetch('/api/analytics/hitrate' + qs(params)),
    drift:        ()       => jsonFetch('/api/analytics/drift'),

    /* ---- 校准 ---- */
    calibration:  (setId) => jsonFetch('/api/calibration' + qs({ set_id: setId })),
    createCalibSet: (d)   => post('/api/calibration/sets', d),
    addCalibItem: (d)     => post('/api/calibration/items', d),

    /* ---- 用量 / 审计 / 隐私 ---- */
    usage:    (params) => jsonFetch('/api/usage' + qs(params)),
    auditLog: (params) => jsonFetch('/api/audit' + qs(params)),
    privacy:  ()       => jsonFetch('/api/privacy'),
    purge:    (dryRun) => post('/api/privacy/purge', { dry_run: dryRun !== false }),
    rotateKey: ()      => post('/api/privacy/rotate', {}),
    exportPerson: (pid) => jsonFetch('/api/privacy/export/' + enc(pid)),
    erasePerson:  (pid) => post('/api/privacy/erase/' + enc(pid), {}),

    /* ---- 回测对齐：把 HR 的真实决定当标准答案 ---- */
    alignList:    (jid)  => jsonFetch('/api/jobs/' + enc(jid) + '/align'),
    alignCreate:  (jid, d) => post('/api/jobs/' + enc(jid) + '/align', d || {}),
    alignPreview: (text) =>
      post('/api/align/preview', { text: text }),
    alignImport:  (aid, text) =>
      post('/api/align/' + enc(aid) + '/import', { text: text }),
    alignReport:  (aid, ks) => jsonFetch('/api/align/' + enc(aid) + '/report'
                                         + (ks ? qs({ ks: ks }) : '')),
    alignDelete:  (aid)  => post('/api/align/' + enc(aid) + '/delete', {}),
    alignPending: (aid)  => jsonFetch('/api/align/' + enc(aid) + '/pending'),
    alignBind:    (aid, truthId, candidateId) =>
      post('/api/align/' + enc(aid) + '/bind',
           { truth_id: truthId, candidate_id: candidateId }),

    /* ---- 候选人决策（理由必填） ---- */
    decide: (cid, decision, reason) =>
      post('/api/candidates/' + enc(cid) + '/decide',
           { decision: decision, reason: reason }),

    /* ---- 评审历史与重复评审的波动 ---- */
    candidateHistory: (cid) => jsonFetch('/api/candidates/' + enc(cid) + '/history'),

    /* ---- 备份 ---- */
    backups:       ()  => jsonFetch('/api/backups'),
    backupCreate:  (d) => post('/api/backups/create', d || {}),
    backupRestore: (file) =>
      post('/api/backups/restore', { file: file, confirm: true }),

    /* ---- 当前评分口径（版本号由哪些部分组成） ---- */
    rubric: () => jsonFetch('/api/rubric'),

    /* ---- 启动自检（probe=1 才会真探测，结果服务端缓存 10 分钟） ---- */

    /* ---- 批量确认身份（只支持"不是同一人"） ---- */
    batchIdentity: (items) => post('/api/identity/batch-confirm', { items: items }),

    /* ---- 岗位级评审细则（权重 + 每维"看什么"） ---- */
    jobRubric: (jid) => jsonFetch('/api/jobs/' + enc(jid) + '/rubric'),
    jobRubricSave: (jid, d) => put('/api/jobs/' + enc(jid) + '/rubric', d || {}),

    /* ---- EvidenceGate：场景 / 案件 / 证据链 ---- */
    profiles:      ()          => jsonFetch('/api/profiles'),
    cases:         ()          => jsonFetch('/api/cases'),
    caseDetail:    (cid)       => jsonFetch('/api/cases/' + enc(cid)),
    caseCreate:    (d)         => post('/api/cases', d),
    caseArtifact:  (cid, d)    => post('/api/cases/' + enc(cid) + '/artifacts', d),
    caseClaims:    (cid, d)    => post('/api/cases/' + enc(cid) + '/claims', d),
    caseGate:      (cid)       => post('/api/cases/' + enc(cid) + '/gate', {}),
    caseExtract:   (cid)       => post('/api/cases/' + enc(cid) + '/extract-claims', {}),
    caseSign:      (cid, d)    => post('/api/cases/' + enc(cid) + '/sign', d || {}),
    caseDelete:    (cid)       => jsonFetch('/api/cases/' + enc(cid), { method: 'DELETE' }),
    caseBundle:    (cid, d)    => post('/api/cases/' + enc(cid) + '/bundle', d || {}),
    goldenThread:  ()          => jsonFetch('/api/validation/golden-thread'),
  };

  window.API = API;
})();
