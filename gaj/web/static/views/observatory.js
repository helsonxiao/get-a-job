/* ============================================
   Get A Job — 市场观察台 (Alpine.data 岛)
   4 视角: G1 区域热力 / G2 信号雷达 / S1 技能热度 / S2 薪资定价
   通过 $store.core.api 调共享层, 不往旧 app.js 塞状态。
   所有图表纯 SVG, 无 CDN 依赖, 遵循项目硬约束。
   ============================================ */
document.addEventListener('alpine:init', () => {
  Alpine.data('observatoryPanel', () => ({
    tab: 'salary',
    salary: null,
    geo: null,
    geoMode: 'jobs',  // jobs | companies (热力图按岗位数/公司数)
    radar: null,
    skills: null,
    loading: false,
    drill: null,
    // 抽屉内嵌详情: { type:'job'|'company', loading, data }
    // drill 存在时点击岗位/公司 → 在抽屉内继续查看, 不离开观察台
    drillDetail: null,

    init() {
      this.load('salary');
    },

    setTab(t) {
      this.tab = t;
      this.load(t);
    },

    // ---- 下钻: 点击图表元素 → 拉 /api/jobs 带筛选 → 抽屉显示岗位 ----
    // 注意: 方法名不能与状态属性 drill 同名, 否则对象字面量去重导致 bug
    // extra: 可选附加筛选 {industry, hasSalary} (表格下钻叠加行业; 薪资tab下钻保持薪资样本口径)
    // type='district_companies': 热力图"按公司"模式, 抽屉显示公司列表
    async openDrill(type, value, label, extra) {
      this.drillDetail = null;
      if (type === 'district_companies') {
        const params = new URLSearchParams({ district: value });
        if (extra && extra.industry) params.set('industry', extra.industry);
        this.drill = { title: label, mode: 'companies', sub: '加载中...', loading: true, jobs: [], companies: [], total: 0 };
        const d = await Alpine.store('core').api('/api/companies/by-district?' + params.toString());
        this.drill = {
          title: label,
          mode: 'companies',
          sub: (d ? d.total : 0) + ' 家公司 · 点击卡片在抽屉内查看详情',
          loading: false,
          jobs: [],
          companies: d ? d.items : [],
          total: d ? d.total : 0,
        };
        return;
      }
      const params = new URLSearchParams({ limit: '100', sort: 'salary_mid', desc: '1' });
      if (type === 'district') params.set('district', value);
      else if (type === 'industry') params.set('industry', value);
      else if (type === 'overtime') params.set('overtime', value);
      else if (type === 'skill') params.set('skill', value);
      else if (type === 'edu_level') params.set('edu_level', value);
      else if (type === 'company') params.set('company_id', value);
      if (extra && extra.industry) params.set('industry', extra.industry);
      // 薪资定价 tab 的柱图 count 只含有薪资样本, 下钻保持同口径
      if (extra && extra.hasSalary) params.set('has_salary', '1');
      this.drill = { title: label, mode: 'jobs', sub: '加载中...', loading: true, jobs: [], companies: [], total: 0 };
      const d = await Alpine.store('core').api('/api/jobs?' + params.toString());
      this.drill = {
        title: label,
        mode: 'jobs',
        sub: (d ? d.total : 0) + ' 个岗位 · 点击卡片在抽屉内查看详情',
        loading: false,
        jobs: d ? d.items : [],
        companies: [],
        total: d ? d.total : 0,
      };
    },

    closeDrill() { this.drill = null; this.drillDetail = null; },

    // 返回抽屉岗位列表 (关闭内嵌详情)
    backToDrillList() { this.drillDetail = null; },

    // SVG 图表事件委托: x-html 生成的元素无法直接绑 @click, 用 data-drill 标记
    onChartClick(e) {
      let el = e.target;
      while (el && el !== e.currentTarget) {
        const attr = el.getAttribute && el.getAttribute('data-drill');
        if (attr) {
          const idx = attr.indexOf(':');
          const type = idx > 0 ? attr.slice(0, idx) : '';
          const value = idx > 0 ? attr.slice(idx + 1) : '';
          const label = (el.getAttribute('data-label') || value) + ' 岗位';
          // 薪资样本口径标记 (薪资定价 tab 的柱图)
          const hasSalary = el.getAttribute('data-has-salary') === '1';
          if (type && value) this.openDrill(type, value, label, hasSalary ? { hasSalary: true } : null);
          return;
        }
        el = el.parentNode;
      }
    },

    // 公司点击: 抽屉打开 → 在抽屉内加载公司详情; 否则 → 根 app 公司抽屉
    openCompany(brandId) {
      if (this.drill) {
        this.openDrillCompany(brandId);
      } else {
        this.$dispatch('obs-open-company', { brandId });
      }
    },

    // 岗位卡片点击: 抽屉打开 → 在抽屉内加载岗位详情; 否则 → 根 app 职位详情
    openJob(jobId) {
      if (this.drill) {
        this.openDrillJob(jobId);
      } else {
        this.$dispatch('obs-open-job', { jobId });
      }
    },

    // 抽屉内嵌: 加载岗位详情
    async openDrillJob(jobId) {
      this.drillDetail = { type: 'job', loading: true, data: null };
      const d = await Alpine.store('core').api('/api/jobs/' + encodeURIComponent(jobId));
      this.drillDetail = { type: 'job', loading: false, data: d };
    },

    // 抽屉内嵌: 加载公司详情
    async openDrillCompany(brandId) {
      this.drillDetail = { type: 'company', loading: true, data: null };
      const d = await Alpine.store('core').api('/api/companies/' + encodeURIComponent(brandId));
      this.drillDetail = { type: 'company', loading: false, data: d };
    },

    async load(tab) {
      this.loading = true;
      const d = await Alpine.store('core').api('/api/observatory/' + tab);
      if (d) this[tab] = d;
      this.loading = false;
    },

    // ============================================================
    // G1 区域机会热力图
    // ============================================================

    // 经纬度投影到 SVG 坐标系 (经度→x, 纬度→y 翻转), 自适应 bounds
    geoHeatmap() {
      if (!this.geo || !this.geo.cells || !this.geo.bounds) return '';
      const b = this.geo.bounds;
      const w = 560, h = 420, pad = 24;
      const lngRange = Math.max(b.max_lng - b.min_lng, 0.01);
      const latRange = Math.max(b.max_lat - b.min_lat, 0.01);
      const scale = Math.min((w - pad * 2) / lngRange, (h - pad * 2) / latRange);
      const offsetX = pad + (w - pad * 2 - lngRange * scale) / 2;
      const offsetY = pad + (h - pad * 2 - latRange * scale) / 2;
      const project = (lng, lat) => ({
        x: offsetX + (lng - b.min_lng) * scale,
        y: offsetY + (b.max_lat - lat) * scale,  // 纬度大靠北→y 小
      });
      // 根据 geoMode 选择度量值: jobs=岗位数, companies=公司数
      const metric = this.geoMode === 'companies' ? 'company_count' : 'count';
      const maxCount = Math.max(...this.geo.cells.map(c => c[metric] || 0)) || 1;
      // 先画底层网格框, 再画热力点 (大半径在下, 小在上, 避免遮挡)
      const cells = [...this.geo.cells].sort((a, c) => (c[metric] || 0) - (a[metric] || 0));
      return cells.map(c => {
        const p = project(c.lng, c.lat);
        const v = c[metric] || 0;
        const r = 4 + (v / maxCount) * 16;
        const intensity = (v / maxCount).toFixed(2);
        // tooltip 用全区口径 (district_total), 与点击后抽屉显示的数字一致
        const tip = `${this._esc(c.top_district || '未知区域')}: ${c.district_total ?? c.count} 岗位 · ${c.district_company_total ?? c.company_count ?? 0} 公司` +
          (c.avg_salary ? ` · 网格均薪 ${c.avg_salary} 万` : '') +
          (c.top_company ? `\n代表公司: ${this._esc(c.top_company)}` : '') +
          `\n${this._esc(c.top_industry || '未知行业')}`;
        // 按公司模式 → 下钻公司列表; 按岗位模式 → 下钻岗位列表
        const drillType = this.geoMode === 'companies' ? 'district_companies' : 'district';
        const drill = c.top_district
          ? ` data-drill="${drillType}:${this._esc(c.top_district)}" data-label="${this._esc(c.top_district)}"`
          : '';
        return `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${r.toFixed(1)}" class="obs-heat-pt obs-clickable" style="--heat:${intensity}"${drill}><title>${tip}</title></circle>`;
      }).join('');
    },

    geoBoundsLabel() {
      if (!this.geo || !this.geo.bounds) return '';
      const b = this.geo.bounds;
      return `经 ${b.min_lng}~${b.max_lng} · 纬 ${b.min_lat}~${b.max_lat}`;
    },

    // ============================================================
    // G2 加班/红旗信号雷达
    // ============================================================

    // 加班分档横向柱图
    radarOvertimeBars() {
      if (!this.radar || !this.radar.summary) return '';
      const ot = this.radar.summary.overtime || {};
      const data = [
        { label: '加班重', key: 'heavy', value: ot.heavy, cls: 'ot-heavy' },
        { label: '加班中', key: 'moderate', value: ot.moderate, cls: 'ot-moderate' },
        { label: '加班轻', key: 'light', value: ot.light, cls: 'ot-light' },
        { label: '未知', key: 'unknown', value: ot.unknown, cls: 'ot-unknown' },
        { label: '无加班', key: 'none', value: ot.none, cls: 'ot-none' },
      ].filter(d => d.value > 0);
      if (!data.length) return '';
      const maxV = Math.max(...data.map(d => d.value));
      const barH = 26, gap = 8, padL = 60, padR = 40, padT = 8;
      const w = 480;
      return data.map((d, i) => {
        const y = padT + i * (barH + gap);
        const bw = (d.value / maxV) * (w - padL - padR);
        return `<text x="${padL - 6}" y="${(y + barH * 0.7).toFixed(1)}" class="obs-axis" text-anchor="end">${d.label}</text>` +
          `<rect x="${padL}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${barH}" class="obs-bar-h ${d.cls} obs-clickable" data-drill="overtime:${d.key}" data-label="${d.label}"><title>${d.label}: ${d.value} 岗位 (点击查看)</title></rect>` +
          `<text x="${(padL + bw + 6).toFixed(1)}" y="${(y + barH * 0.7).toFixed(1)}" class="obs-bar-label">${d.value}</text>`;
      }).join('');
    },

    radarOvertimeHeight() {
      if (!this.radar || !this.radar.summary) return 0;
      const ot = this.radar.summary.overtime || {};
      const n = [ot.heavy, ot.moderate, ot.light, ot.unknown, ot.none].filter(v => v > 0).length;
      return 16 + n * 34;
    },

    // 各行业红旗率榜 (横向柱)
    radarIndustryBars() {
      if (!this.radar || !this.radar.by_industry) return '';
      const data = this.radar.by_industry.slice(0, 8);
      if (!data.length) return '';
      const maxRate = Math.max(...data.map(d => d.red_flag_rate)) || 1;
      const barH = 22, gap = 6, padL = 90, padR = 50, padT = 8;
      const w = 520;
      return data.map((d, i) => {
        const y = padT + i * (barH + gap);
        const bw = (d.red_flag_rate / maxRate) * (w - padL - padR);
        return `<text x="${padL - 6}" y="${(y + barH * 0.7).toFixed(1)}" class="obs-axis" text-anchor="end">${this._esc(d.name)}</text>` +
          `<rect x="${padL}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${barH}" class="obs-bar-h obs-bar-red obs-clickable" data-drill="industry:${this._esc(d.name)}" data-label="${this._esc(d.name)} 红旗"><title>${d.name}: 红旗率 ${(d.red_flag_rate * 100).toFixed(0)}% (${d.count} 岗位) · 点击查看</title></rect>` +
          `<text x="${(padL + bw + 6).toFixed(1)}" y="${(y + barH * 0.7).toFixed(1)}" class="obs-bar-label">${(d.red_flag_rate * 100).toFixed(0)}%</text>`;
      }).join('');
    },

    radarIndustryHeight() {
      if (!this.radar || !this.radar.by_industry) return 0;
      return 16 + Math.min(this.radar.by_industry.length, 8) * 28;
    },

    // ============================================================
    // S1 技能热度榜 (表格为主, 溢价着色)
    // ============================================================

    skillPremiumText(p) {
      if (p == null) return '—';
      return (p > 0 ? '+' : '') + (p * 100).toFixed(1) + '%';
    },

    skillPremiumClass(p) {
      if (p == null || p === 0) return '';
      return p > 0 ? 'premium-up' : 'premium-down';
    },

    // ============================================================
    // S2 薪资定价曲线 (首发已实现)
    // ============================================================

    // 经验-薪资折线
    salaryExpChart() {
      if (!this.salary || !this.salary.by_exp) return '';
      const data = this.salary.by_exp.filter(e => e.median != null);
      if (data.length < 2) return '';
      const w = 560, h = 200, padL = 48, padR = 20, padT = 20, padB = 36;
      const maxV = Math.max(...data.map(d => d.median)) * 1.15;
      const step = (w - padL - padR) / (data.length - 1);
      const pts = data.map((d, i) => {
        const x = padL + i * step;
        const y = h - padB - (d.median / maxV) * (h - padT - padB);
        return { x, y, d };
      });
      const line = pts.map(p => p.x.toFixed(1) + ',' + p.y.toFixed(1)).join(' ');
      const dots = pts.map(p =>
        `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="4" class="obs-line-dot"><title>${p.d.label}: ${p.d.median}万 (${p.d.count}样本)</title></circle>`
      ).join('');
      const xLabels = pts.map(p =>
        `<text x="${p.x.toFixed(1)}" y="${(h - padB + 18).toFixed(1)}" class="obs-axis" text-anchor="middle">${p.d.label}</text>`
      ).join('');
      const yTicks = this._yTicks(0, maxV, h, padT, padB);
      return `<polyline points="${line}" class="obs-line"/>${dots}${xLabels}${yTicks}`;
    },

    // 学历-薪资柱图
    salaryEduBars() {
      if (!this.salary || !this.salary.by_edu) return '';
      const data = this.salary.by_edu.filter(e => e.median != null);
      if (!data.length) return '';
      const w = 560, h = 200, padL = 48, padR = 20, padT = 20, padB = 36;
      const maxV = Math.max(...data.map(d => d.median)) * 1.15;
      const bw = (w - padL - padR) / data.length;
      const barW = bw * 0.6;
      const bars = data.map((d, i) => {
        const x = padL + i * bw + (bw - barW) / 2;
        const bh = (d.median / maxV) * (h - padT - padB);
        const y = h - padB - bh;
        return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${bh.toFixed(1)}" class="obs-bar obs-bar-${d.level} obs-clickable" data-drill="edu_level:${d.level}" data-label="${d.label}学历" data-has-salary="1"><title>${d.label}: ${d.median}万 (${d.count}样本) · 点击查看</title></rect>` +
          `<text x="${(x + barW / 2).toFixed(1)}" y="${(h - padB + 18).toFixed(1)}" class="obs-axis" text-anchor="middle">${d.label}</text>` +
          `<text x="${(x + barW / 2).toFixed(1)}" y="${(y - 6).toFixed(1)}" class="obs-bar-label" text-anchor="middle">${d.median}</text>`;
      }).join('');
      const yTicks = this._yTicks(0, maxV, h, padT, padB);
      return bars + yTicks;
    },

    // 行业中位数对比柱图 (横向)
    salaryIndustryBars() {
      if (!this.salary || !this.salary.by_industry) return '';
      const data = this.salary.by_industry.filter(e => e.median != null);
      if (!data.length) return '';
      const maxV = Math.max(...data.map(d => d.median)) * 1.1;
      const barH = 22, gap = 6, padL = 110, padR = 50, padT = 8;
      const w = 560;
      return data.map((d, i) => {
        const y = padT + i * (barH + gap);
        const bw = (d.median / maxV) * (w - padL - padR);
        return `<text x="${(padL - 6).toFixed(1)}" y="${(y + barH * 0.7).toFixed(1)}" class="obs-axis" text-anchor="end">${this._esc(d.name)}</text>` +
          `<rect x="${padL}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${barH}" class="obs-bar-h obs-clickable" data-drill="industry:${this._esc(d.name)}" data-label="${this._esc(d.name)} 薪资" data-has-salary="1"><title>${d.name}: ${d.median}万 (${d.count}样本) · 点击查看</title></rect>` +
          `<text x="${(padL + bw + 6).toFixed(1)}" y="${(y + barH * 0.7).toFixed(1)}" class="obs-bar-label">${d.median}万</text>`;
      }).join('');
    },

    // 融资阶段中位数
    salaryStageBars() {
      if (!this.salary || !this.salary.by_stage) return '';
      const data = this.salary.by_stage.filter(e => e.median != null);
      if (!data.length) return '';
      const w = 560, h = 200, padL = 48, padR = 20, padT = 20, padB = 36;
      const maxV = Math.max(...data.map(d => d.median)) * 1.15;
      const bw = (w - padL - padR) / data.length;
      const barW = bw * 0.6;
      const bars = data.map((d, i) => {
        const x = padL + i * bw + (bw - barW) / 2;
        const bh = (d.median / maxV) * (h - padT - padB);
        const y = h - padB - bh;
        return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${bh.toFixed(1)}" class="obs-bar obs-bar-stage"><title>${d.name}: ${d.median}万 (${d.count}样本)</title></rect>` +
          `<text x="${(x + barW / 2).toFixed(1)}" y="${(h - padB + 18).toFixed(1)}" class="obs-axis" text-anchor="middle">${this._esc(d.name)}</text>` +
          `<text x="${(x + barW / 2).toFixed(1)}" y="${(y - 6).toFixed(1)}" class="obs-bar-label" text-anchor="middle">${d.median}</text>`;
      }).join('');
      const yTicks = this._yTicks(0, maxV, h, padT, padB);
      return bars + yTicks;
    },

    // ============================================================
    // 通用渲染辅助
    // ============================================================

    _yTicks(minV, maxV, h, padT, padB) {
      const steps = 4;
      let out = '';
      for (let i = 0; i <= steps; i++) {
        const v = minV + (maxV - minV) * i / steps;
        const y = h - padB - (i / steps) * (h - padT - padB);
        out += `<line x1="48" y1="${y.toFixed(1)}" x2="560" y2="${y.toFixed(1)}" class="obs-grid"/>` +
          `<text x="42" y="${(y + 4).toFixed(1)}" class="obs-axis" text-anchor="end">${v.toFixed(0)}</text>`;
      }
      return out;
    },

    _esc(s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
      }[ch]));
    },
  }));
});
