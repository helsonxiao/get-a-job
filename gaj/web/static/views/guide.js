/* ============================================
   Get A Job — 公司图鉴视图岛 (guidePanel)
   三榜卡片墙/象限气泡图/四维雷达对比 + 公司详情抽屉
   (含抽屉内嵌岗位详情、公司级 AI 评价、想去清单)。
   模板: index.html <!-- VIEW: GUIDE --> 区段。

   数据惰性加载: 首次切到 guide 视图时才拉 facets/items
   (x-effect 监听 $store.core.view)。
   ============================================ */
document.addEventListener('alpine:init', () => {
  Alpine.data('guidePanel', () => ({
    facets: null,
    items: [],
    total: 0,
    loading: false,
    q: '',
    industry: '',
    stage: '',
    favorite: 'all',
    sort: 'score',            // score(分数榜) | jobs(招聘力度榜) | salary(薪资榜)
    mode: 'cards',            // cards | quadrant | compare
    companyDetail: null,
    companyDetailLoading: false,
    // 公司抽屉内嵌岗位详情: 抽屉内点击岗位不离开当前页面
    companyJobDetail: null,
    compareIds: [],
    compareDetails: {},
    // AI 评价重新解析状态 (与岗位侧对称)
    companyReparseFile: '',
    companyReparseExpand: '',

    // 惰性加载: 首次进入 guide 视图才拉数据
    onFirstEnter() {
      if (!this.facets && !this.loading) this.loadGuide();
    },

    async loadGuide() {
      this.loading = true;
      const facets = await this.$store.core.api('/api/companies/facets');
      if (facets) this.facets = facets;
      await this.loadGuideItems();
      this.loading = false;
    },

    async loadGuideItems() {
      const p = new URLSearchParams();
      if (this.q) p.set('q', this.q);
      if (this.industry) p.set('industry', this.industry);
      if (this.stage) p.set('stage', this.stage);
      if (this.favorite === 'only') p.set('favorite', 'only');
      p.set('sort', this.sort);
      p.set('limit', '200');
      const d = await this.$store.core.api('/api/companies?' + p.toString());
      if (d) { this.items = d.items || []; this.total = d.total || 0; }
      return d;
    },

    setGuideSort(s) { this.sort = s; this.loadGuideItems(); },

    setGuideMode(m) {
      this.mode = m;
      if (m === 'compare') this.loadCompareDetails();
    },

    guideUnlocked(c) { return (c.ai_scored_count || 0) > 0; },

    tierClass(tier) {
      return { S: 'tier-s', A: 'tier-a', B: 'tier-b', C: 'tier-c' }[tier] || '';
    },

    // ---- 公司详情抽屉 ----

    async openCompany(brandId) {
      this.companyDetailLoading = true;
      this.companyDetail = null;
      this.companyJobDetail = null;
      const d = await this.$store.core.api('/api/companies/' + encodeURIComponent(brandId));
      if (d) this.companyDetail = d;
      this.companyDetailLoading = false;
    },

    closeCompany() { this.companyDetail = null; this.companyDetailLoading = false; this.companyJobDetail = null; },

    // 公司抽屉内嵌: 加载岗位详情, 不离开抽屉
    async openJobInCompany(jobId) {
      this.companyJobDetail = { loading: true, data: null };
      const d = await this.$store.core.api('/api/jobs/' + encodeURIComponent(jobId));
      this.companyJobDetail = { loading: false, data: d };
    },

    // 返回公司详情 (关闭内嵌岗位详情)
    backToCompany() { this.companyJobDetail = null; },

    async toggleCompanyFavorite(brandId, fav) {
      const d = await this.$store.core.api('/api/companies/' + encodeURIComponent(brandId) + '/favorite', 'POST', { favorite: fav });
      if (d && d.ok) {
        const item = this.items.find(c => c.brand_id === brandId);
        if (item) item.favorite = d.favorite ? 1 : 0;
        if (this.companyDetail && this.companyDetail.company.brand_id === brandId) {
          this.companyDetail.company.favorite = d.favorite;
        }
        const f = await this.$store.core.api('/api/companies/facets');
        if (f) this.facets = f;
        // 想去清单视图里取消收藏 → 直接从列表移除
        if (this.favorite === 'only' && !d.favorite) {
          this.items = this.items.filter(c => c.brand_id !== brandId);
        }
        this.$store.core.toast(d.favorite ? '已加入想去清单' : '已移出想去清单', 'success');
      } else {
        this.$store.core.toast('操作失败, 请重试', 'error');
      }
    },

    // ---- 对比 ----

    toggleCompare(brandId) {
      if (this.compareIds.includes(brandId)) {
        this.compareIds = this.compareIds.filter(id => id !== brandId);
        delete this.compareDetails[brandId];
      } else {
        if (this.compareIds.length >= 4) { this.$store.core.toast('最多对比 4 家公司', 'warning'); return; }
        this.compareIds = [...this.compareIds, brandId];
        if (this.mode === 'compare') this.loadCompareDetails();
      }
    },

    async loadCompareDetails() {
      for (const id of this.compareIds) {
        if (!this.compareDetails[id]) {
          const d = await this.$store.core.api('/api/companies/' + encodeURIComponent(id));
          if (d) this.compareDetails[id] = d;
        }
      }
    },

    // ---- 公司级 AI 操作 ----

    async appraiseCompany(c) {
      // 剪影卡"去鉴定": 对公司最佳岗位发起 AI 打分, 联动打分 backlog
      if (!c.top_job_id) { this.$store.core.toast('这家公司暂无可鉴定的岗位', 'warning'); return; }
      const p = new URLSearchParams({ provider: this.$store.core.aiProvider });
      const d = await this.$store.core.api('/api/jobs/' + encodeURIComponent(c.top_job_id) + '/ai-score?' + p.toString(), 'POST');
      if (d) {
        this.$store.core.toast('AI 鉴定已启动: ' + (c.top_job_title || c.top_job_id), 'success');
        this.$store.core.logOpen = true;
      } else {
        this.$store.core.toast('启动 AI 鉴定失败', 'error');
      }
    },

    async analyzeCompany(brandId) {
      // 公司级 AI 评价 (图鉴词条): 纯手动触发, 全量落盘历史
      const p = new URLSearchParams({ provider: this.$store.core.aiProvider });
      const d = await this.$store.core.api('/api/companies/' + encodeURIComponent(brandId) + '/ai-analyze?' + p.toString(), 'POST');
      if (d && d.status === 'started') {
        this.$store.core.toast('公司 AI 评价已启动, 进度见日志面板', 'success');
        this.$store.core.logOpen = true;
      } else if (d && d.status === 'already_running') {
        this.$store.core.toast('这家公司正在评价中', 'warning');
      } else {
        this.$store.core.toast('启动公司评价失败', 'error');
      }
    },

    async deleteCompanyAiScore(file) {
      if (!this.companyDetail) return;
      if (!confirm('确认删除这条公司级 AI 评价?')) return;
      const brandId = this.companyDetail.company.brand_id;
      const d = await this.$store.core.api('/api/companies/' + encodeURIComponent(brandId) + '/ai-scores/' + encodeURIComponent(file), 'DELETE');
      if (d && d.ok) {
        this.companyDetail.ai_scores = (this.companyDetail.ai_scores || []).filter(s => s._file !== file);
        this.$store.core.toast('公司 AI 评价已删除', 'success');
      } else {
        this.$store.core.toast('删除失败, 请重试', 'error');
      }
    },

    async reparseCompanyAi(file) {
      // 公司级 AI 评价人工重新解析 (与岗位侧 reparseAi 对称)
      if (!this.companyDetail || !file) return;
      if (this.companyReparseFile === file) return;  // 防重入
      const scoreItem = (this.companyDetail.ai_scores || []).find(s => s._file === file);
      if (!scoreItem) return;
      const rawText = (scoreItem._edit_text || scoreItem.raw_response || '').trim();
      if (!rawText) { this.$store.core.toast('请输入 AI 原始回复文本', 'error'); return; }
      const brandId = this.companyDetail.company.brand_id;
      this.companyReparseFile = file;
      try {
        const d = await this.$store.core.api('/api/companies/' + encodeURIComponent(brandId) + '/ai-reparse', 'POST', {
          raw_text: rawText,
          provider: scoreItem.provider || 'unknown',
        });
        if (d && d.ok) {
          this.$store.core.toast(
            '重新解析成功: ' + d.result.worth_joining
            + ' ' + (d.result.company_score_ai != null ? d.result.company_score_ai.toFixed(1) : '?') + '/10',
            'success'
          );
          this.companyReparseExpand = '';
          await this.openCompany(brandId);
        } else {
          this.$store.core.toast('解析失败, 请检查文本是否包含合法 JSON', 'error');
        }
      } catch (e) {
        this.$store.core.toast('解析失败: ' + (e.message || '未知错误'), 'error');
      } finally {
        this.companyReparseFile = '';
      }
    },

    // ---- 象限图 (内联 SVG, 不引图表库) ----

    quadrantData() {
      const pts = [];
      const unknown = [];
      for (const c of this.items) {
        if (c.company_score == null) continue;
        if (c.salary_mid_avg == null) { unknown.push(c); continue; }
        pts.push(c);
      }
      const xMax = Math.max(20, ...pts.map(c => c.salary_mid_avg)) * 1.1;
      return { pts, unknown, xMax };
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

    // ---- 四维雷达 (对比模式 + 公司抽屉共用) ----

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

    // ---- 跨视图事件 ----

    // 观察台 "在主视图打开公司" (store.openCompany 派发)
    onOpenCompany(brandId) { if (brandId) this.openCompany(brandId); },

    // 后台任务状态变化: 刷新图鉴 + 重开抽屉 + 清对比缓存
    async onRefresh() {
      if (this.$store.core.view !== 'guide') return;
      await this.loadGuide();
      if (this.companyDetail) await this.openCompany(this.companyDetail.company.brand_id);
      for (const id of this.compareIds) delete this.compareDetails[id];
      if (this.mode === 'compare') this.loadCompareDetails();
    },
  }));
});
