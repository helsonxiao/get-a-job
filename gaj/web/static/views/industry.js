/* ============================================
   Get A Job — 行业观察视图岛 (industryPanel)
   行业卡片墙 → 行业详情 (薪资结构/技能/信号/公司/岗位/政策占位)。
   模板: views/industry.html · 样式: styles/industry.css
   数据: /api/observatory/industry (列表) + /industry/{name} (详情)
   ============================================ */
document.addEventListener('alpine:init', () => {
  Alpine.data('industryPanel', () => ({
    // ---- 列表态 ----
    items: [],
    loading: false,
    search: '',
    sort: 'jobs',        // jobs | salary | redflag
    marketMedian: null,
    unknownJobCount: 0,
    // ---- 详情态 ----
    detail: null,
    detailName: null,
    detailLoading: false,
    // ---- 岗位预览 (详情页) ----
    previewJobs: [],
    previewTotal: 0,
    previewLoading: false,

    init() {
      this.loadList();
    },

    // 观察台联动入口 (store.openIndustry 派发)
    onOpenIndustry(detail) {
      if (detail && detail.name) {
        this.openIndustry(detail.name);
      } else {
        this.loadList();
      }
    },

    async loadList() {
      this.loading = true;
      const d = await this.$store.core.api('/api/observatory/industry');
      if (d) {
        this.items = d.items || [];
        this.marketMedian = d.market_median;
        this.unknownJobCount = d.unknown_job_count || 0;
      }
      this.loading = false;
    },

    filteredItems() {
      let list = this.items;
      if (this.search) {
        const q = this.search.toLowerCase();
        list = list.filter(x => x.name.toLowerCase().includes(q));
      }
      const arr = [...list];
      if (this.sort === 'salary') {
        arr.sort((a, b) => (b.salary_median ?? -1) - (a.salary_median ?? -1));
      } else if (this.sort === 'redflag') {
        arr.sort((a, b) => (b.red_flag_rate ?? -1) - (a.red_flag_rate ?? -1));
      } else {
        arr.sort((a, b) => b.job_count - a.job_count);
      }
      return arr;
    },

    // 薪资相对市场溢价 % (null = 无数据)
    premiumPct(median) {
      if (median == null || !this.marketMedian) return null;
      return Math.round(((median - this.marketMedian) / this.marketMedian) * 100);
    },

    premiumClass(pct) {
      if (pct == null) return '';
      return pct >= 5 ? 'premium-up' : (pct <= -5 ? 'premium-down' : 'premium-flat');
    },

    // 红旗率分级: <0.25 低 / 0.25-0.5 中 / >0.5 高
    redFlagClass(rate) {
      if (rate == null) return '';
      return rate > 0.5 ? 'flag-high' : (rate >= 0.25 ? 'flag-mid' : 'flag-low');
    },

    // ---- 详情 ----
    async openIndustry(name) {
      this.detailName = name;
      this.detail = null;
      this.detailLoading = true;
      this.previewJobs = [];
      this.previewTotal = 0;
      const d = await this.$store.core.api('/api/observatory/industry/' + encodeURIComponent(name));
      if (d) this.detail = d;
      this.detailLoading = false;
      this.loadPreviewJobs(name);
    },

    backToList() { this.detail = null; this.detailName = null; },

    // 详情页岗位预览 (Top 20, 复用职位列表接口)
    async loadPreviewJobs(name) {
      this.previewLoading = true;
      const p = new URLSearchParams({ industry: name, limit: '20', sort: 'best_total' });
      const d = await this.$store.core.api('/api/jobs?' + p.toString());
      if (d) { this.previewJobs = d.items || []; this.previewTotal = d.total || 0; }
      this.previewLoading = false;
    },

    // 跨视图: 公司 → 全局公司抽屉; 岗位 → 职位列表
    openCompany(brandId) { this.$store.core.openCompanyDrawer(brandId); },
    openJob(jobId) { this.$store.core.openJob(jobId); },

    // 技能溢价 vs 全市场中位
    skillPremiumText(avg, market) {
      if (avg == null || market == null) return '—';
      const pct = Math.round(((avg - market) / market) * 100);
      return (pct > 0 ? '+' : '') + pct + '%';
    },
    skillPremiumClass(avg, market) {
      if (avg == null || market == null) return '';
      const pct = (avg - market) / market;
      return pct >= 0.05 ? 'premium-up' : (pct <= -0.05 ? 'premium-down' : 'premium-flat');
    },
  }));
});
