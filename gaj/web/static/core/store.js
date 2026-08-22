/* ============================================
   Get A Job — Alpine.store 共享层
   提供 api/toast/stats/tasks/SSE 等跨视图共享功能。
   视图岛通过 $store.core.xxx 调用, 避免重复实现。

   设计要点:
   - pollTasks 数据层与视图刷新分离: store.fetchTasks() 返回 changed 信号,
     由组件决定是否刷新视图 (loadJobs/selectJob/loadGuide 等)。
   - SSE 只负责 logs 数据推送, DOM 滚动由组件通过 x-effect 监听 logs.length 触发。
   - 轮询调度由 store.startPolling(hook) 启动, hook 通常是组件的 pollTasks 包装。
   ============================================ */
document.addEventListener('alpine:init', () => {
  Alpine.store('core', {
    // ---- 状态 ----
    toasts: [],
    _toastSeq: 0,
    stats: {},
    tasks: {},
    logs: [],
    sseConnected: false,

    // ---- 内部句柄 ----
    _sse: null,
    _pollTimer: null,
    _pollInterval: 5000,
    _pollHook: null,
    _lastTaskSnap: {},

    // ---- API ----
    async api(path, method = 'GET', body = null) {
      try {
        const opts = { method };
        if (body) {
          opts.headers = { 'Content-Type': 'application/json' };
          opts.body = JSON.stringify(body);
        }
        const r = await fetch(path, opts);
        if (!r.ok) { console.error('API error', r.status, await r.text()); return null; }
        return await r.json();
      } catch (e) { console.error('fetch error', e); return null; }
    },

    // ---- Toast ----
    toast(msg, type = 'info') {
      const id = ++this._toastSeq;
      this.toasts.push({ id, msg, type });
      setTimeout(() => this.removeToast(id), 3000);
    },
    removeToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },

    // ---- Stats ----
    async loadStats() {
      const d = await this.api('/api/stats');
      if (d) this.stats = d;
    },

    // ---- Tasks 数据层 ----
    _taskLabel(key) {
      if (key === 'score-all') return '规则打分';
      if (key === 'reindex') return '重建索引';
      if (key.startsWith('ai-score-')) return 'AI 打分';
      if (key.startsWith('company-analyze-')) return '公司 AI 评价';
      if (key.startsWith('resume-')) return '简历生成';
      return '任务';
    },

    // 拉取 tasks, 推送状态变化 toast, 返回是否有 running→done/error 的状态变化
    async fetchTasks() {
      const d = await this.api('/api/tasks');
      if (!d || !d.tasks) return false;
      const cur = d.tasks;
      const prev = this._lastTaskSnap || {};
      let changed = false;
      for (const [k, t] of Object.entries(cur)) {
        if (prev[k]?.status === 'running' && t.status !== 'running') {
          changed = true;
          const label = this._taskLabel(k);
          if (t.status === 'done') {
            this.toast(label + '已完成', 'success');
          } else if (t.status === 'error') {
            this.toast(label + '失败: ' + (t.error || '未知错误'), 'error');
          }
        }
      }
      this.tasks = cur;
      this._lastTaskSnap = cur;
      // 动态调整轮询间隔: 有任务在跑时 2s, 稳态 5s
      const hasRunning = Object.values(cur).some(t => t.status === 'running');
      const wantInterval = hasRunning ? 2000 : 5000;
      if (this._pollInterval !== wantInterval && this._pollHook) {
        clearInterval(this._pollTimer);
        this._pollTimer = setInterval(this._pollHook, wantInterval);
        this._pollInterval = wantInterval;
      }
      return changed;
    },

    // 启动轮询, hook 通常是组件的 pollTasks 包装 (负责 changed 后的视图刷新)
    startPolling(hook) {
      this._pollHook = hook;
      clearInterval(this._pollTimer);
      this._pollTimer = setInterval(hook, this._pollInterval);
    },

    // ---- SSE ----
    connectSSE() {
      // 关闭旧连接, 防止 HMR 重载导致多个 EventSource 累积
      if (this._sse) { try { this._sse.close(); } catch (_) {} this._sse = null; }
      const es = new EventSource('/api/logs/stream');
      this._sse = es;
      es.onopen = () => { this.sseConnected = true; };
      es.onerror = () => { this.sseConnected = false; };
      es.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data);
          if (d.type === 'connected' || d.type === 'heartbeat' || d.type === 'shutdown') return;
          this.logs.push(d);
          if (this.logs.length > 500) this.logs = this.logs.slice(-300);
        } catch (e) {}
      };
    },
  });

  // 自检标记 (验收: 控制台可见即代表共享层已加载)
  console.log('[gaj] core store registered');
});
