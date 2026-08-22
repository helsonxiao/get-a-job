/* ============================================
   Get A Job — 职位视图岛 (jobsPanel)
   左列表 + 右详情: 筛选/分页/批量删除/收藏/忽略/
   人工调分/AI 打分触发/重新解析/分数轨迹。
   模板: index.html <!-- VIEW: JOBS --> 区段。

   共享态 (stats/tasks/aiProvider/providers/selectMode)
   走 $store.core, 本岛只持有职位视图私有状态。
   ============================================ */
document.addEventListener('alpine:init', () => {
  Alpine.data('jobsPanel', () => ({
    jobs: [],
    jobsTotal: 0,
    jobsLoading: false,
    filters: { search: '', city: '', status: '', scored: 'all', favorite: 'all', ignored: 'exclude', sort: 'best_total', offset: 0 },
    selectedJobId: null,
    detail: null,
    detailLoading: false,
    manualEdit: { total: '', note: '' },
    manualSaving: false,
    reparseFile: '',
    reparseExpand: '',
    selectedIds: [],
    batchDeleting: false,

    init() {
      this.loadJobs();
      this.initListWidth();
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
      const d = await this.$store.core.api('/api/jobs?' + p.toString());
      if (d) { this.jobs = d.items || []; this.jobsTotal = d.total || 0; }
      this.jobsLoading = false;
    },

    async selectJob(id) {
      this.selectedJobId = id;
      this.detailLoading = true;
      this.detail = null;
      const d = await this.$store.core.api('/api/jobs/' + encodeURIComponent(id));
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

    // ---- 收藏 / 忽略 / 删除 ----

    async toggleFavorite(jobId, fav) {
      const d = await this.$store.core.api('/api/jobs/' + encodeURIComponent(jobId) + '/favorite', 'POST', { favorite: fav });
      if (d && d.ok) {
        // 同步列表项
        const job = this.jobs.find(j => j.job_id === jobId);
        if (job) { job.favorite = d.favorite; job.favorited_at = d.favorited_at || ''; }
        // 同步详情
        if (this.detail && this.detail.job && this.detail.job.job_id === jobId) {
          this.detail.job.favorite = d.favorite;
          this.detail.job.favorited_at = d.favorited_at || '';
        }
        await this.$store.core.loadStats();
        this.$store.core.toast(d.favorite ? '已收藏' : '已取消收藏', 'success');
      } else {
        this.$store.core.toast('操作失败, 请重试', 'error');
      }
    },

    async toggleIgnore(jobId, ign) {
      const d = await this.$store.core.api('/api/jobs/' + encodeURIComponent(jobId) + '/ignore', 'POST', { ignored: ign });
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
        await this.$store.core.loadStats();
        this.$store.core.toast(d.ignored ? '已忽略, 列表不再展示' : '已取消忽略', 'success');
      } else {
        this.$store.core.toast('操作失败, 请重试', 'error');
      }
    },

    async deleteJob(jobId) {
      if (!confirm('确认删除该职位? 此操作不可恢复 (含所有打分文件)')) return;
      const d = await this.$store.core.api('/api/jobs/' + encodeURIComponent(jobId), 'DELETE');
      if (d && d.ok) {
        if (this.selectedJobId === jobId) { this.selectedJobId = null; this.detail = null; }
        this.selectedIds = this.selectedIds.filter(id => id !== jobId);
        await this.loadJobs();
        await this.$store.core.loadStats();
        this.$store.core.toast('职位已删除', 'success');
      } else {
        this.$store.core.toast('删除失败, 请重试', 'error');
      }
    },

    // ---- AI 打分 ----

    async deleteAiScore(file) {
      if (!file) return;
      if (!confirm('确认删除该条 AI 打分?')) return;
      const d = await this.$store.core.api('/api/jobs/' + encodeURIComponent(this.selectedJobId) + '/ai-scores/' + encodeURIComponent(file), 'DELETE');
      if (d && d.ok) {
        if (this.detail) this.detail.ai_scores = (this.detail.ai_scores || []).filter(s => s._file !== file);
        await this.loadJobs();
        await this.$store.core.loadStats();
        this.$store.core.toast('AI 打分已删除', 'success');
      } else {
        this.$store.core.toast('删除失败, 请重试', 'error');
      }
    },

    async reparseAi(jobId, file) {
      if (!jobId || !file) return;
      if (this.reparseFile === file) return;  // 防重入
      const scoreItem = (this.detail?.ai_scores || []).find(s => s._file === file);
      if (!scoreItem) return;
      const rawText = (scoreItem._edit_text || scoreItem.raw_response || '').trim();
      if (!rawText) { this.$store.core.toast('请输入 AI 原始回复文本', 'error'); return; }
      this.reparseFile = file;
      try {
        const d = await this.$store.core.api('/api/jobs/' + encodeURIComponent(jobId) + '/ai-reparse', 'POST', {
          raw_text: rawText,
          provider: scoreItem.provider || 'unknown',
        });
        if (d && d.ok) {
          this.$store.core.toast('重新解析成功: ' + d.result.status + ' ' + d.result.total_score + '/10', 'success');
          await this.selectJob(jobId);
          await this.loadJobs();
        } else {
          this.$store.core.toast('解析失败, 请检查文本是否包含合法 JSON', 'error');
        }
      } catch (e) {
        this.$store.core.toast('解析失败: ' + (e.message || '未知错误'), 'error');
      } finally {
        this.reparseFile = '';
      }
    },

    job_id_for_detail() {
      return this.selectedJobId;
    },

    async triggerAiScore(deep = false) {
      if (!this.selectedJobId) return;
      const p = new URLSearchParams({ provider: this.$store.core.aiProvider, deep: String(deep) });
      await this.$store.core.api('/api/jobs/' + encodeURIComponent(this.selectedJobId) + '/ai-score?' + p, 'POST');
      this.$store.core.logOpen = true;
    },

    async triggerResume() {
      if (!this.selectedJobId) return;
      const p = new URLSearchParams({ provider: this.$store.core.aiProvider, style: 'optimize' });
      await this.$store.core.api('/api/jobs/' + encodeURIComponent(this.selectedJobId) + '/resume?' + p, 'POST');
      this.$store.core.logOpen = true;
    },

    // ---- 人工调分 ----

    async saveManualOverride() {
      if (!this.selectedJobId) return;
      const total = this.manualEdit.total;
      if (total === '' || total === null) {
        this.$store.core.toast('请输入调整后的分数', 'warning');
        return;
      }
      this.manualSaving = true;
      const d = await this.$store.core.api('/api/jobs/' + encodeURIComponent(this.selectedJobId) + '/manual-override', 'POST', {
        total: parseFloat(total),
        note: this.manualEdit.note || ''
      });
      if (d && d.ok) {
        if (this.detail && this.detail.job) {
          this.detail.job.manual_override = d.manual_override;
        }
        this.manualEdit = { total: '', note: '' };
        await this.loadJobs();
        this.$store.core.toast('人工调分已保存', 'success');
      } else {
        this.$store.core.toast('人工调分保存失败', 'error');
      }
      this.manualSaving = false;
    },

    async clearManualOverride() {
      if (!this.selectedJobId) return;
      if (!confirm('确认清除人工调分? 分数将回退到 AI/规则分')) return;
      this.manualSaving = true;
      const d = await this.$store.core.api('/api/jobs/' + encodeURIComponent(this.selectedJobId) + '/manual-override', 'POST', {
        total: null,
        note: ''
      });
      if (d && d.ok) {
        if (this.detail && this.detail.job) {
          this.detail.job.manual_override = d.manual_override;
        }
        this.manualEdit = { total: '', note: '' };
        await this.loadJobs();
        this.$store.core.toast('已清除人工调分, 回退到 AI/规则分', 'success');
      } else {
        this.$store.core.toast('清除失败, 请重试', 'error');
      }
      this.manualSaving = false;
    },

    // ---- 分数轨迹 sparkline (按 created_at 升序的 AI total_score 折线) ----

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

    sparkDotsSvg(sp) {
      return sp.dots.map(pt =>
        `<circle cx="${pt.x.toFixed(1)}" cy="${pt.y.toFixed(1)}" r="3.2" class="spark-dot">`
        + `<title>${this._esc(pt.t)} · ${pt.v.toFixed(1)}/10</title></circle>`).join('');
    },

    // ---- 批量选择 ----

    onToggleSelect() {
      const ui = this.$store.core.ui;
      ui.selectMode = !ui.selectMode;
      if (!ui.selectMode) this.clearSelection();
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
      const d = await this.$store.core.api('/api/jobs/batch-delete', 'POST', { ids });
      if (d && d.ok) {
        const msg = `删除完成: ${d.deleted_count} 成功` + (d.failed.length ? `, ${d.failed.length} 失败` : '');
        this.$store.core.toast(msg, d.failed.length ? 'warning' : 'success');
        if (this.selectedJobId && d.deleted.includes(this.selectedJobId)) {
          this.selectedJobId = null; this.detail = null;
        }
        this.clearSelection();
        await this.loadJobs();
        await this.$store.core.loadStats();
      } else {
        this.$store.core.toast('批量删除失败', 'error');
      }
      this.batchDeleting = false;
    },

    // ---- 分页 ----

    prevPage() { if (this.filters.offset > 0) { this.filters.offset -= 50; this.loadJobs(); } },
    nextPage() { if (this.filters.offset + this.jobs.length < this.jobsTotal) { this.filters.offset += 50; this.loadJobs(); } },

    // ---- 跨视图事件 ----

    // 观察台/图鉴抽屉 "在主视图打开" (store.openJob 派发)
    onOpenJob(jobId) { if (jobId) this.selectJob(jobId); },

    // 后台任务状态变化 (store.poll 派发): 刷新列表 + 当前详情
    async onRefresh() {
      await this.loadJobs();
      if (this.selectedJobId) await this.selectJob(this.selectedJobId);
    },

    _esc(s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
      }[ch]));
    },
  }));
});
