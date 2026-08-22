/* ============================================
   Get A Job — Alpine.store 共享层 (core)
   跨视图共享的唯一真相源: api/toast/stats/tasks/SSE/
   view 路由/跨视图跳转 (openJob/openCompany)。

   视图岛通过 $store.core.xxx 调用, 禁止各岛自带副本。
   (架构决策见 .trae/documents/adr-001-frontend-framework.md)

   设计要点:
   - view 单一状态替代旧 N 个布尔 flag (showGuide/showConfig/...)
   - pollTasks 数据层与视图刷新分离: store.poll() 在任务状态变化时
     dispatch 'gaj:refresh' 事件, 各视图岛自行监听刷新。
   - SSE 只负责 logs 数据推送, DOM 滚动由 log panel 自行 x-effect。
   - 跨视图跳转 (观察台→职位详情 等) 走 openJob/openCompany:
     切换 view + dispatch 事件, 目标岛监听后加载。
   ============================================ */
document.addEventListener('alpine:init', () => {
  Alpine.store('core', {
    // ---- 视图路由 (单一真相源) ----
    // jobs | guide | observatory | config | resume
    view: 'jobs',

    // ---- 跨视图共享状态 ----
    toasts: [],
    _toastSeq: 0,
    stats: {},
    tasks: {},
    logs: [],
    sseConnected: false,
    providers: ['deepseek', 'doubao', 'tongyi', 'kimi'],
    aiProvider: 'deepseek',
    // header "上传简历" 按钮标签依赖 (resumePanel 加载/保存后回写)
    resumeExists: false,
    // header 批量按钮与 jobsPanel 共享的 UI 态
    ui: { selectMode: false },
    // 日志面板展开态 (各操作触发任务后置 true 提示看进度)
    logOpen: false,

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

    // ---- 视图切换 ----
    switchView(v) { this.view = v; },
    // config/resume 类面板按钮语义: 再点一次回到 jobs 视图
    toggleView(v) { this.view = (this.view === v) ? 'jobs' : v; },

    // 跨视图跳转: 观察台/图鉴抽屉 → 职位详情 (jobsPanel 监听加载)
    openJob(jobId) {
      this.view = 'jobs';
      window.dispatchEvent(new CustomEvent('gaj:open-job', { detail: { jobId } }));
    },
    // 跨视图跳转: 观察台 → 公司抽屉 (guidePanel 监听打开)
    openCompany(brandId) {
      this.view = 'guide';
      window.dispatchEvent(new CustomEvent('gaj:open-company', { detail: { brandId } }));
    },

    // ---- Stats ----
    async loadStats() {
      const d = await this.api('/api/stats');
      if (d) this.stats = d;
    },

    async loadProviders() {
      const d = await this.api('/api/providers');
      if (d && d.providers) this.providers = d.providers;
    },

    // ---- Tasks ----
    taskRunning(key) {
      return Object.values(this.tasks).some(t => t.status === 'running' && t.key.includes(key));
    },

    _taskLabel(key) {
      if (key === 'score-all') return '规则打分';
      if (key === 'reindex') return '重建索引';
      if (key.startsWith('ai-score-')) return 'AI 打分';
      if (key.startsWith('company-analyze-')) return '公司 AI 评价';
      if (key.startsWith('config-calibrate-')) return 'AI 规则矫正';
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

    // 轮询入口: 任务状态变化时刷新 stats 并广播, 各视图岛监听 'gaj:refresh' 自行刷新
    async poll() {
      const changed = await this.fetchTasks();
      if (changed) {
        await this.loadStats();
        window.dispatchEvent(new Event('gaj:refresh'));
      }
    },

    // 启动轮询
    startPolling(hook) {
      this._pollHook = hook;
      clearInterval(this._pollTimer);
      this._pollTimer = setInterval(hook, this._pollInterval);
    },

    // ---- 全局动作 ----
    async scoreAll() {
      // 规则打分很快, 始终 force=true 强制重打, 确保画像修改后分数会更新
      const d = await this.api('/api/score-all?force=true', 'POST');
      if (d && d.status === 'started') {
        this.toast('规则打分已启动...', 'success');
        // 立即轮询一次, 让按钮马上变成"打分中"状态
        this.poll();
      } else if (d && d.status === 'already_running') {
        this.toast('规则打分正在进行中', 'warning');
      } else {
        this.toast('启动打分失败', 'error');
      }
    },

    // ---- SSE ----
    connectSSE() {
      // 关闭旧连接, 防止 HMR 重载导致多个 EventSource 累积
      if (this._sse) { try { this._sse.close(); } catch(_) {} this._sse = null; }
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
        } catch(e) {}
      };
    },

    // ---- 启动 (body x-init 调一次) ----
    async bootstrap() {
      await this.loadStats();
      await this.loadProviders();
      this.connectSSE();
      this.startPolling(() => this.poll());
    },
  });

  // 自检标记 (验收: 控制台可见即代表共享层已加载)
  console.log('[gaj] core store registered');
});
