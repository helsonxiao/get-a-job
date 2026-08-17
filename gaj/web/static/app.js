function app() {
  return {
    stats: {}, jobs: [], jobsTotal: 0, jobsLoading: false,
    detail: null, detailLoading: false,
    selectedJobId: null,
    providers: ['deepseek', 'doubao', 'tongyi', 'kimi'],
    aiProvider: 'deepseek',
    filters: { search: '', city: '', status: '', scored: 'all', favorite: 'all', ignored: 'exclude', sort: 'best_total', offset: 0 },
    tasks: {},
    logs: [], sseConnected: false, logCollapsed: true,
    showConfig: false, configTab: 'rules',
    rulesCatalog: null, rulesEdit: {}, initialRulesEdit: {}, rulesSaving: false, scoringPresets: {},
    profileData: null, profileSaving: false, weightPresets: {},
    showResume: false, resumeData: {content: '', exists: false, size: 0}, resumeSaving: false, resumeSavedAt: '',
    manualEdit: {total: '', note: ''}, manualSaving: false,
    selectMode: false, selectedIds: [], batchDeleting: false,
    reparseFile: '', reparseExpand: '',
    companyReparseFile: '', companyReparseExpand: '',
    toasts: [], _toastSeq: 0,
    // ---- 公司图鉴 ----
    showGuide: false,
    guide: {
      facets: null, items: [], total: 0, loading: false,
      q: '', industry: '', stage: '', favorite: 'all',
      sort: 'score',            // score(分数榜) | jobs(招聘力度榜) | salary(薪资榜)
      mode: 'cards',            // cards | quadrant | compare
    },
    companyDetail: null, companyDetailLoading: false,
    compareIds: [], compareDetails: {},

    async init() {
      await this.loadStats();
      await this.loadJobs();
      await this.loadProviders();
      this.loadRules();
      this.loadProfile();
      this.loadWeightPresets();
      this.loadResume();
      this.connectSSE();
      this.initListWidth();
      // 动态轮询: 有任务在跑时 2s, 稳态 5s, 减少日志噪音
      this._pollTimer = setInterval(() => this.pollTasks(), 5000);
    },

    async api(path, method = 'GET', body = null) {
      try {
        const opts = { method };
        if (body) {
          opts.headers = {'Content-Type': 'application/json'};
          opts.body = JSON.stringify(body);
        }
        const r = await fetch(path, opts);
        if (!r.ok) { console.error('API error', r.status, await r.text()); return null; }
        return await r.json();
      } catch(e) { console.error('fetch error', e); return null; }
    },

    toast(msg, type = 'info') {
      const id = ++this._toastSeq;
      this.toasts.push({id, msg, type});
      setTimeout(() => this.removeToast(id), 3000);
    },
    removeToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },

    async loadRules() {
      const d = await this.api('/api/rules');
      if (!d) return;
      this.rulesCatalog = d;
      // 从后端返回的 overrides 初始化编辑状态 (null = 用默认值)
      const ov = d.overrides || {};
      this.rulesEdit = {...ov};
      this.initialRulesEdit = {...ov};
      // 并行加载评分项配比预设
      const p = await this.api('/api/scoring-config/presets');
      if (p && p.presets) this.scoringPresets = p.presets;
    },

    dimMaxSum(dim) {
      return dim.items.reduce((sum, item) => {
        const v = this.rulesEdit[item.override_key];
        return sum + (v != null ? v : item.default_max);
      }, 0).toFixed(1);
    },

    formatThreshold(t) {
      if (!t) return '';
      const v = t.value;
      if (Array.isArray(v)) return v.length ? v.join('、') : '(空)';
      if (typeof v === 'boolean') return v ? '是' : '否';
      return String(v);
    },

    async saveScoringConfig() {
      // 校验维度封顶: 各项满分之和不能超过 10
      if (this.rulesCatalog) {
        for (const dim of this.rulesCatalog.dimensions) {
          const sum = parseFloat(this.dimMaxSum(dim));
          if (sum > 10) {
            this.toast(`${dim.label} 各项满分之和 ${sum} 超过封顶 10, 请调整后再保存`, 'error');
            return;
          }
        }
      }
      this.rulesSaving = true;
      const d = await this.api('/api/scoring-config', 'PUT', this.rulesEdit);
      if (d && d.ok) {
        this.rulesEdit = {...d.overrides};
        this.initialRulesEdit = {...d.overrides};
        this.toast('规则参数已保存, 请点击"规则打分"刷新分数', 'success');
      } else {
        this.toast('保存失败, 请重试', 'error');
      }
      this.rulesSaving = false;
    },

    resetRulesEdit() {
      this.rulesEdit = {...this.initialRulesEdit};
    },

    resetAllRulesDefault() {
      if (!confirm('确认将所有规则参数恢复为代码默认值? (已保存的覆盖将被清除)')) return;
      const cleared = {};
      for (const k of Object.keys(this.rulesEdit)) cleared[k] = null;
      this.rulesEdit = cleared;
    },

    applyScoringPreset(name) {
      const p = this.scoringPresets[name];
      if (!p) return;
      // 覆盖全部评分项字段 (reject_confidence_floor 保持不动)
      const next = {...this.rulesEdit};
      for (const [k, v] of Object.entries(p)) next[k] = v;
      this.rulesEdit = next;
      this.toast(`已套用「${name}」配比预设, 记得点"保存规则参数"生效`, 'info');
    },

    async loadProfile() {
      const d = await this.api('/api/profile');
      if (d) {
        // list 字段: 后端返回 Array, 前端编辑用字符串 (x-model 绑 Array 会打断 IME)
        // 保存时后端 _to_list 会把逗号分隔字符串 split 回 Array
        if (d.groups && d.fields) {
          for (const g of d.groups) {
            for (const f of g.fields) {
              if (f.type === 'list' && Array.isArray(d.fields[f.name])) {
                d.fields[f.name] = d.fields[f.name].join(', ');
              }
            }
          }
        }
        this.profileData = d;
      }
    },

    async saveProfile() {
      if (!this.profileData) return;
      this.profileSaving = true;
      const d = await this.api('/api/profile', 'PUT', this.profileData.fields);
      if (d && d.ok) {
        // 后端返回的 list 字段是 Array, 转回字符串供前端编辑
        if (this.profileData.groups) {
          for (const g of this.profileData.groups) {
            for (const f of g.fields) {
              if (f.type === 'list' && Array.isArray(d.fields[f.name])) {
                d.fields[f.name] = d.fields[f.name].join(', ');
              }
            }
          }
        }
        this.profileData.fields = d.fields;
        this.profileData.weights = d.weights;
        this.toast('画像已保存, 建议点击"规则打分"刷新分数', 'success');
      } else {
        this.toast('画像保存失败, 请重试', 'error');
      }
      this.profileSaving = false;
    },

    async loadWeightPresets() {
      const d = await this.api('/api/profile/weight-presets');
      if (d && d.presets) this.weightPresets = d.presets;
    },

    applyWeightPreset(name) {
      const p = this.weightPresets[name];
      if (!p || !this.profileData) return;
      // 预设 key (growth/finance/wlb/resource) → profile 字段 (weight_xxx)
      this.profileData.fields.weight_growth = p.growth;
      this.profileData.fields.weight_finance = p.finance;
      this.profileData.fields.weight_wlb = p.wlb;
      this.profileData.fields.weight_resource = p.resource;
      this.toast(`已套用「${name}」预设, 记得点"保存画像"生效`, 'info');
    },

    async loadResume() {
      const d = await this.api('/api/resume');
      if (d) {
        this.resumeData = d;
        if (d.exists && !this.resumeSavedAt) {
          this.resumeSavedAt = '已加载';
        }
      }
    },

    async saveResume() {
      const content = this.resumeData.content || '';
      this.resumeSaving = true;
      const d = await this.api('/api/resume', 'PUT', { content });
      if (d && d.ok) {
        this.resumeData.exists = true;
        this.resumeData.size = d.size;
        this.resumeSavedAt = new Date().toLocaleTimeString();
        this.toast('简历已保存', 'success');
      } else {
        this.toast('简历保存失败, 请重试', 'error');
      }
      this.resumeSaving = false;
    },

    clearResume() {
      if (!confirm('确认清空简历内容? (不会删除文件, 只是清空编辑器)')) return;
      this.resumeData.content = '';
    },

    onResumeFile(ev) {
      const file = ev.target.files[0];
      if (!file) return;
      // 只认 .md / .markdown / text
      const name = file.name.toLowerCase();
      if (!name.endsWith('.md') && !name.endsWith('.markdown') && !file.type.startsWith('text/')) {
        alert('只支持 .md / .markdown 格式的文本文件');
        ev.target.value = '';
        return;
      }
      const reader = new FileReader();
      reader.onload = (e) => {
        this.resumeData.content = e.target.result;
        console.log('已读取简历文件:', file.name);
      };
      reader.onerror = () => alert('读取文件失败');
      reader.readAsText(file, 'utf-8');
      ev.target.value = '';
    },

    async loadStats() {
      const d = await this.api('/api/stats');
      if (d) this.stats = d;
    },

    async loadProviders() {
      const d = await this.api('/api/providers');
      if (d && d.providers) this.providers = d.providers;
    },

    async loadJobs() {
      this.jobsLoading = true;
      const p = new URLSearchParams();
      if (this.filters.search) p.set('search', this.filters.search);
      if (this.filters.city) p.set('city', this.filters.city);
      if (this.filters.status) p.set('status', this.filters.status);
      if (this.filters.scored !== 'all') p.set('scored', this.filters.scored);
      if (this.filters.favorite !== 'all') p.set('favorite', this.filters.favorite);
      if (this.filters.ignored !== 'exclude') p.set('ignored', this.filters.ignored);
      p.set('sort', this.filters.sort);
      p.set('limit', '50');
      p.set('offset', String(this.filters.offset));
      const d = await this.api('/api/jobs?' + p.toString());
      if (d) { this.jobs = d.items || []; this.jobsTotal = d.total || 0; }
      this.jobsLoading = false;
    },

    async selectJob(id) {
      this.selectedJobId = id;
      this.detailLoading = true;
      this.detail = null;
      const d = await this.api('/api/jobs/' + encodeURIComponent(id));
      if (d) this.detail = d;
      this.detailLoading = false;
    },

    // 列表宽度拖拽
    startResize(e) {
      e.preventDefault();
      const panel = document.querySelector('.list-panel');
      const resizer = e.target;
      panel.classList.add('dragging');
      resizer.classList.add('active');
      const startX = e.clientX;
      const startWidth = panel.offsetWidth;
      const onMove = (ev) => {
        const delta = ev.clientX - startX;
        const newWidth = Math.max(280, Math.min(window.innerWidth * 0.7, startWidth + delta));
        panel.style.width = newWidth + 'px';
      };
      const onUp = () => {
        panel.classList.remove('dragging');
        resizer.classList.remove('active');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        try { localStorage.setItem('listWidth', panel.offsetWidth); } catch(_) {}
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    },

    initListWidth() {
      try {
        const w = parseInt(localStorage.getItem('listWidth') || '0', 10);
        if (w >= 280) {
          const panel = document.querySelector('.list-panel');
          if (panel) panel.style.width = Math.min(window.innerWidth * 0.7, w) + 'px';
        }
      } catch(_) {}
    },

    async scoreAll() {
      // 规则打分很快, 始终 force=true 强制重打, 确保画像修改后分数会更新
      const d = await this.api('/api/score-all?force=true', 'POST');
      if (d && d.status === 'started') {
        this.toast('规则打分已启动...', 'success');
        // 立即轮询一次, 让按钮马上变成"打分中"状态
        this.pollTasks();
      } else if (d && d.status === 'already_running') {
        this.toast('规则打分正在进行中', 'warning');
      } else {
        this.toast('启动打分失败', 'error');
      }
    },

    // 分数轨迹 sparkline: 按 created_at 升序的 AI total_score 折线
    sparklineData(scores) {
      const valid = (scores || [])
        .filter(s => s.status !== 'PARSE_FAILED' && s.total_score != null)
        .sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')));
      const w = 240, h = 56, pad = 10;
      const n = valid.length;
      const dots = valid.map((s, i) => ({
        x: n === 1 ? w / 2 : pad + (i / (n - 1)) * (w - pad * 2),
        y: h - 8 - (Math.max(0, Math.min(10, s.total_score)) / 10) * (h - 16),
        v: s.total_score,
        t: s.created_at || '',
      }));
      return { w, h, dots, points: dots.map(d => d.x.toFixed(1) + ',' + d.y.toFixed(1)).join(' ') };
    },

    async saveManualOverride() {
      if (!this.selectedJobId) return;
      const total = this.manualEdit.total;
      if (total === '' || total === null) {
        this.toast('请输入调整后的分数', 'warning');
        return;
      }
      this.manualSaving = true;
      const d = await this.api('/api/jobs/' + encodeURIComponent(this.selectedJobId) + '/manual-override', 'POST', {
        total: parseFloat(total),
        note: this.manualEdit.note || ''
      });
      if (d && d.ok) {
        if (this.detail && this.detail.job) {
          this.detail.job.manual_override = d.manual_override;
        }
        this.manualEdit = {total: '', note: ''};
        await this.loadJobs();
        this.toast('人工调分已保存', 'success');
      } else {
        this.toast('人工调分保存失败', 'error');
      }
      this.manualSaving = false;
    },

    async clearManualOverride() {
      if (!this.selectedJobId) return;
      if (!confirm('确认清除人工调分? 分数将回退到 AI/规则分')) return;
      this.manualSaving = true;
      const d = await this.api('/api/jobs/' + encodeURIComponent(this.selectedJobId) + '/manual-override', 'POST', {
        total: null,
        note: ''
      });
      if (d && d.ok) {
        if (this.detail && this.detail.job) {
          this.detail.job.manual_override = d.manual_override;
        }
        this.manualEdit = {total: '', note: ''};
        await this.loadJobs();
        this.toast('已清除人工调分, 回退到 AI/规则分', 'success');
      } else {
        this.toast('清除失败, 请重试', 'error');
      }
      this.manualSaving = false;
    },

    async toggleFavorite(jobId, fav) {
      const d = await this.api('/api/jobs/' + encodeURIComponent(jobId) + '/favorite', 'POST', { favorite: fav });
      if (d && d.ok) {
        // 同步列表项
        const job = this.jobs.find(j => j.job_id === jobId);
        if (job) { job.favorite = d.favorite; job.favorited_at = d.favorited_at || ''; }
        // 同步详情
        if (this.detail && this.detail.job && this.detail.job.job_id === jobId) {
          this.detail.job.favorite = d.favorite;
          this.detail.job.favorited_at = d.favorited_at || '';
        }
        await this.loadStats();
        this.toast(d.favorite ? '已收藏' : '已取消收藏', 'success');
      } else {
        this.toast('操作失败, 请重试', 'error');
      }
    },

    async toggleIgnore(jobId, ign) {
      const d = await this.api('/api/jobs/' + encodeURIComponent(jobId) + '/ignore', 'POST', { ignored: ign });
      if (d && d.ok) {
        // 同步详情
        if (this.detail && this.detail.job && this.detail.job.job_id === jobId) {
          this.detail.job.ignored = d.ignored;
        }
        // 列表默认排除忽略项, 忽略后从列表移除; 取消忽略则重新加载
        if (this.filters.ignored === 'exclude') {
          if (d.ignored) {
            this.jobs = this.jobs.filter(j => j.job_id !== jobId);
            this.jobsTotal = Math.max(0, this.jobsTotal - 1);
          }
        } else {
          const job = this.jobs.find(j => j.job_id === jobId);
          if (job) job.ignored = d.ignored;
        }
        await this.loadStats();
        this.toast(d.ignored ? '已忽略, 列表不再展示' : '已取消忽略', 'success');
      } else {
        this.toast('操作失败, 请重试', 'error');
      }
    },

    async deleteJob(jobId) {
      if (!confirm('确认删除该职位? 此操作不可恢复 (含所有打分文件)')) return;
      const d = await this.api('/api/jobs/' + encodeURIComponent(jobId), 'DELETE');
      if (d && d.ok) {
        if (this.selectedJobId === jobId) { this.selectedJobId = null; this.detail = null; }
        this.selectedIds = this.selectedIds.filter(id => id !== jobId);
        await this.loadJobs();
        await this.loadStats();
        this.toast('职位已删除', 'success');
      } else {
        this.toast('删除失败, 请重试', 'error');
      }
    },

    async deleteAiScore(file) {
      if (!file) return;
      if (!confirm('确认删除该条 AI 打分?')) return;
      const d = await this.api('/api/jobs/' + encodeURIComponent(this.selectedJobId) + '/ai-scores/' + encodeURIComponent(file), 'DELETE');
      if (d && d.ok) {
        if (this.detail) this.detail.ai_scores = (this.detail.ai_scores || []).filter(s => s._file !== file);
        await this.loadJobs();
        await this.loadStats();
        this.toast('AI 打分已删除', 'success');
      } else {
        this.toast('删除失败, 请重试', 'error');
      }
    },

    async reparseAi(jobId, file) {
      if (!jobId || !file) return;
      if (this.reparseFile === file) return;  // 防重入
      const scoreItem = (this.detail?.ai_scores || []).find(s => s._file === file);
      if (!scoreItem) return;
      const rawText = (scoreItem._edit_text || scoreItem.raw_response || '').trim();
      if (!rawText) { this.toast('请输入 AI 原始回复文本', 'error'); return; }
      this.reparseFile = file;
      try {
        const d = await this.api('/api/jobs/' + encodeURIComponent(jobId) + '/ai-reparse', 'POST', {
          raw_text: rawText,
          provider: scoreItem.provider || 'unknown',
        });
        if (d && d.ok) {
          this.toast('重新解析成功: ' + d.result.status + ' ' + d.result.total_score + '/10', 'success');
          await this.selectJob(jobId);
          await this.loadJobs();
        } else {
          this.toast('解析失败, 请检查文本是否包含合法 JSON', 'error');
        }
      } catch (e) {
        this.toast('解析失败: ' + (e.message || '未知错误'), 'error');
      } finally {
        this.reparseFile = '';
      }
    },

    job_id_for_detail() {
      return this.selectedJobId;
    },

    toggleSelect(jobId) {
      if (this.selectedIds.includes(jobId)) {
        this.selectedIds = this.selectedIds.filter(id => id !== jobId);
      } else {
        this.selectedIds = [...this.selectedIds, jobId];
      }
    },

    clearSelection() {
      this.selectedIds = [];
    },

    selectAllOnPage() {
      const ids = this.jobs.map(j => j.job_id);
      this.selectedIds = [...new Set([...this.selectedIds, ...ids])];
    },

    async batchDelete() {
      const ids = [...this.selectedIds];
      if (ids.length === 0) return;
      if (!confirm(`确认删除选中的 ${ids.length} 个职位? 此操作不可恢复`)) return;
      this.batchDeleting = true;
      const d = await this.api('/api/jobs/batch-delete', 'POST', { ids });
      if (d && d.ok) {
        const msg = `删除完成: ${d.deleted_count} 成功` + (d.failed.length ? `, ${d.failed.length} 失败` : '');
        this.toast(msg, d.failed.length ? 'warning' : 'success');
        if (this.selectedJobId && d.deleted.includes(this.selectedJobId)) {
          this.selectedJobId = null; this.detail = null;
        }
        this.clearSelection();
        await this.loadJobs();
        await this.loadStats();
      } else {
        this.toast('批量删除失败', 'error');
      }
      this.batchDeleting = false;
    },

    async triggerAiScore(deep = false) {
      if (!this.selectedJobId) return;
      const p = new URLSearchParams({ provider: this.aiProvider, deep: String(deep) });
      await this.api('/api/jobs/' + encodeURIComponent(this.selectedJobId) + '/ai-score?' + p, 'POST');
      this.logCollapsed = false;
    },

    async triggerResume() {
      if (!this.selectedJobId) return;
      const p = new URLSearchParams({ provider: this.aiProvider, style: 'optimize' });
      await this.api('/api/jobs/' + encodeURIComponent(this.selectedJobId) + '/resume?' + p, 'POST');
      this.logCollapsed = false;
    },

    taskRunning(key) {
      return Object.values(this.tasks).some(t => t.status === 'running' && t.key.includes(key));
    },

    async pollTasks() {
      const d = await this.api('/api/tasks');
      if (!d || !d.tasks) return;
      const cur = d.tasks;
      // 对比上次快照, 只在 "running → done/error" 状态变化时刷新数据
      const prev = this._lastTaskSnap || {};
      let changed = false;
      for (const [k, t] of Object.entries(cur)) {
        if (prev[k]?.status === 'running' && t.status !== 'running') {
          changed = true;
          // 后台任务完成/失败提示
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
      if (changed) {
        await this.loadStats();
        if (this.selectedJobId) await this.selectJob(this.selectedJobId);
        await this.loadJobs();
        if (this.showGuide) {
          await this.loadGuide();
          if (this.companyDetail) await this.openCompany(this.companyDetail.company.brand_id);
          for (const id of this.compareIds) delete this.compareDetails[id];
          if (this.guide.mode === 'compare') this.loadCompareDetails();
        }
      }
      // 动态调整轮询间隔: 有任务在跑时 2s, 稳态 5s
      const hasRunning = Object.values(cur).some(t => t.status === 'running');
      const wantInterval = hasRunning ? 2000 : 5000;
      if (this._pollInterval !== wantInterval) {
        clearInterval(this._pollTimer);
        this._pollTimer = setInterval(() => this.pollTasks(), wantInterval);
        this._pollInterval = wantInterval;
      }
    },

    _taskLabel(key) {
      if (key === 'score-all') return '规则打分';
      if (key === 'reindex') return '重建索引';
      if (key.startsWith('ai-score-')) return 'AI 打分';
      if (key.startsWith('company-analyze-')) return '公司 AI 评价';
      if (key.startsWith('resume-')) return '简历生成';
      return '任务';
    },

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
          this.$nextTick(() => {
            if (this.$refs.logBody) this.$refs.logBody.scrollTop = this.$refs.logBody.scrollHeight;
          });
        } catch(e) {}
      };
    },

    // ---- 公司图鉴 ----

    openGuide() {
      this.showGuide = true;
      this.showConfig = false;
      this.showResume = false;
      if (!this.guide.facets) this.loadGuide();
    },

    async loadGuide() {
      this.guide.loading = true;
      const facets = await this.api('/api/companies/facets');
      if (facets) this.guide.facets = facets;
      await this.loadGuideItems();
      this.guide.loading = false;
    },

    async loadGuideItems() {
      const g = this.guide;
      const p = new URLSearchParams();
      if (g.q) p.set('q', g.q);
      if (g.industry) p.set('industry', g.industry);
      if (g.stage) p.set('stage', g.stage);
      if (g.favorite === 'only') p.set('favorite', 'only');
      p.set('sort', g.sort);
      p.set('limit', '200');
      const d = await this.api('/api/companies?' + p.toString());
      if (d) { this.guide.items = d.items || []; this.guide.total = d.total || 0; }
      return d;
    },

    setGuideSort(s) { this.guide.sort = s; this.loadGuideItems(); },

    setGuideMode(m) {
      this.guide.mode = m;
      if (m === 'compare') this.loadCompareDetails();
    },

    guideUnlocked(c) { return (c.ai_scored_count || 0) > 0; },

    tierClass(tier) {
      return {S: 'tier-s', A: 'tier-a', B: 'tier-b', C: 'tier-c'}[tier] || '';
    },

    async openCompany(brandId) {
      this.companyDetailLoading = true;
      this.companyDetail = null;
      const d = await this.api('/api/companies/' + encodeURIComponent(brandId));
      if (d) this.companyDetail = d;
      this.companyDetailLoading = false;
    },

    closeCompany() { this.companyDetail = null; this.companyDetailLoading = false; },

    async toggleCompanyFavorite(brandId, fav) {
      const d = await this.api('/api/companies/' + encodeURIComponent(brandId) + '/favorite', 'POST', {favorite: fav});
      if (d && d.ok) {
        const item = this.guide.items.find(c => c.brand_id === brandId);
        if (item) item.favorite = d.favorite ? 1 : 0;
        if (this.companyDetail && this.companyDetail.company.brand_id === brandId) {
          this.companyDetail.company.favorite = d.favorite;
        }
        const f = await this.api('/api/companies/facets');
        if (f) this.guide.facets = f;
        // 想去清单视图里取消收藏 → 直接从列表移除
        if (this.guide.favorite === 'only' && !d.favorite) {
          this.guide.items = this.guide.items.filter(c => c.brand_id !== brandId);
        }
        this.toast(d.favorite ? '已加入想去清单' : '已移出想去清单', 'success');
      } else {
        this.toast('操作失败, 请重试', 'error');
      }
    },

    toggleCompare(brandId) {
      if (this.compareIds.includes(brandId)) {
        this.compareIds = this.compareIds.filter(id => id !== brandId);
        delete this.compareDetails[brandId];
      } else {
        if (this.compareIds.length >= 4) { this.toast('最多对比 4 家公司', 'warning'); return; }
        this.compareIds = [...this.compareIds, brandId];
        if (this.guide.mode === 'compare') this.loadCompareDetails();
      }
    },

    async loadCompareDetails() {
      for (const id of this.compareIds) {
        if (!this.compareDetails[id]) {
          const d = await this.api('/api/companies/' + encodeURIComponent(id));
          if (d) this.compareDetails[id] = d;
        }
      }
    },

    async appraiseCompany(c) {
      // 剪影卡"去鉴定": 对公司最佳岗位发起 AI 打分, 联动打分 backlog
      if (!c.top_job_id) { this.toast('这家公司暂无可鉴定的岗位', 'warning'); return; }
      const p = new URLSearchParams({provider: this.aiProvider});
      const d = await this.api('/api/jobs/' + encodeURIComponent(c.top_job_id) + '/ai-score?' + p.toString(), 'POST');
      if (d) {
        this.toast('AI 鉴定已启动: ' + (c.top_job_title || c.top_job_id), 'success');
        this.logCollapsed = false;
      } else {
        this.toast('启动 AI 鉴定失败', 'error');
      }
    },

    openJobFromCompany(jobId) {
      this.closeCompany();
      this.showGuide = false;
      this.selectJob(jobId);
    },

    async analyzeCompany(brandId) {
      // 公司级 AI 评价 (图鉴词条): 纯手动触发, 全量落盘历史
      const p = new URLSearchParams({ provider: this.aiProvider });
      const d = await this.api('/api/companies/' + encodeURIComponent(brandId) + '/ai-analyze?' + p.toString(), 'POST');
      if (d && d.status === 'started') {
        this.toast('公司 AI 评价已启动, 进度见日志面板', 'success');
        this.logCollapsed = false;
      } else if (d && d.status === 'already_running') {
        this.toast('这家公司正在评价中', 'warning');
      } else {
        this.toast('启动公司评价失败', 'error');
      }
    },

    async deleteCompanyAiScore(file) {
      if (!this.companyDetail) return;
      if (!confirm('确认删除这条公司级 AI 评价?')) return;
      const brandId = this.companyDetail.company.brand_id;
      const d = await this.api('/api/companies/' + encodeURIComponent(brandId) + '/ai-scores/' + encodeURIComponent(file), 'DELETE');
      if (d && d.ok) {
        this.companyDetail.ai_scores = (this.companyDetail.ai_scores || []).filter(s => s._file !== file);
        this.toast('公司 AI 评价已删除', 'success');
      } else {
        this.toast('删除失败, 请重试', 'error');
      }
    },

    async reparseCompanyAi(file) {
      // 公司级 AI 评价人工重新解析 (与岗位侧 reparseAi 对称)
      if (!this.companyDetail || !file) return;
      if (this.companyReparseFile === file) return;  // 防重入
      const scoreItem = (this.companyDetail.ai_scores || []).find(s => s._file === file);
      if (!scoreItem) return;
      const rawText = (scoreItem._edit_text || scoreItem.raw_response || '').trim();
      if (!rawText) { this.toast('请输入 AI 原始回复文本', 'error'); return; }
      const brandId = this.companyDetail.company.brand_id;
      this.companyReparseFile = file;
      try {
        const d = await this.api('/api/companies/' + encodeURIComponent(brandId) + '/ai-reparse', 'POST', {
          raw_text: rawText,
          provider: scoreItem.provider || 'unknown',
        });
        if (d && d.ok) {
          this.toast(
            '重新解析成功: ' + d.result.worth_joining
            + ' ' + (d.result.company_score_ai != null ? d.result.company_score_ai.toFixed(1) : '?') + '/10',
            'success'
          );
          this.companyReparseExpand = '';
          await this.openCompany(brandId);
          if (this.showGuide) await this.loadGuide();
        } else {
          this.toast('解析失败, 请检查文本是否包含合法 JSON', 'error');
        }
      } catch (e) {
        this.toast('解析失败: ' + (e.message || '未知错误'), 'error');
      } finally {
        this.companyReparseFile = '';
      }
    },

    // ---- 象限图 (内联 SVG, 不引图表库) ----

    quadrantData() {
      const pts = [];
      const unknown = [];
      for (const c of this.guide.items) {
        if (c.company_score == null) continue;
        if (c.salary_mid_avg == null) { unknown.push(c); continue; }
        pts.push(c);
      }
      const xMax = Math.max(20, ...pts.map(c => c.salary_mid_avg)) * 1.1;
      return {pts, unknown, xMax};
    },

    quadrantXY(c, xMax) {
      // viewBox 760x440, 绘图区 x:[60,730] y:[20,390]
      return {
        x: 60 + (c.salary_mid_avg / xMax) * 670,
        y: 390 - (c.company_score / 10) * 370,
      };
    },

    quadrantXTicks(xMax) {
      const step = xMax > 60 ? 20 : 10;
      const out = [];
      for (let v = step; v <= xMax; v += step) out.push(v);
      return out;
    },

    bubbleR(c) { return 7 + Math.min(13, (c.job_count || 1) * 1.6); },

    // SVG 内部不能用 <template x-for> (HTML 解析器会把它移出 svg, 导致循环变量 undefined),
    // 所以网格/气泡/散点都拼成字符串走 x-html 渲染, 点击用事件委托
    _esc(s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
      }[ch]));
    },

    quadrantGridSvg(qd) {
      let out = '';
      for (const v of this.quadrantXTicks(qd.xMax)) {
        const x = (60 + (v / qd.xMax) * 670).toFixed(1);
        out += `<line x1="${x}" y1="390" x2="${x}" y2="20" class="qgrid"></line>`
             + `<text x="${x}" y="410" class="qaxis" text-anchor="middle">${v}</text>`;
      }
      for (const v of [2, 4, 6, 8, 10]) {
        const y = 390 - v * 37;
        out += `<line x1="60" y1="${y}" x2="730" y2="${y}" class="qgrid"></line>`
             + `<text x="48" y="${y + 4}" class="qaxis" text-anchor="end">${v}</text>`;
      }
      return out;
    },

    quadrantBubblesSvg(qd) {
      return qd.pts.map(c => {
        const p = this.quadrantXY(c, qd.xMax);
        const title = this._esc(
          c.name + ' · 分 ' + (c.company_score != null ? c.company_score.toFixed(1) : '?')
          + ' · 均薪 ' + c.salary_mid_avg.toFixed(1) + '万 · ' + (c.job_count || 0) + '岗');
        return `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${this.bubbleR(c).toFixed(1)}" `
             + `class="qbubble ${this.tierClass(c.rank_tier)}" data-brand="${this._esc(c.brand_id)}">`
             + `<title>${title}</title></circle>`;
      }).join('');
    },

    quadrantClick(e) {
      const t = e.target.closest ? e.target.closest('circle[data-brand]') : null;
      if (t) this.openCompany(t.getAttribute('data-brand'));
    },

    sparkDotsSvg(sp) {
      return sp.dots.map(pt =>
        `<circle cx="${pt.x.toFixed(1)}" cy="${pt.y.toFixed(1)}" r="3.2" class="spark-dot">`
        + `<title>${this._esc(pt.t)} · ${pt.v.toFixed(1)}/10</title></circle>`).join('');
    },

    // ---- 雷达图 ----

    radarPoints(dims, cx, cy, r) {
      if (!dims) return '';
      const axes = ['finance', 'growth', 'resource', 'wlb'];
      const vec = [[0, -1], [1, 0], [0, 1], [-1, 0]];  // 上/右/下/左
      return axes.map((k, i) => {
        const v = Math.max(0, Math.min(10, dims[k] || 0));
        const t = (v / 10) * r;
        return (cx + vec[i][0] * t) + ',' + (cy + vec[i][1] * t);
      }).join(' ');
    },

    prevPage() { if (this.filters.offset > 0) { this.filters.offset -= 50; this.loadJobs(); } },
    nextPage() { if (this.filters.offset + this.jobs.length < this.jobsTotal) { this.filters.offset += 50; this.loadJobs(); } },
  };
}
