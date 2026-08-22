/* ============================================
   Get A Job — 规则配置视图岛 (configPanel)
   两个 Tab: 规则概览 (硬规则/四维评分/AI 触发/配比预设)
   + 画像编辑 (字段表单/权重预设)。
   含 AI 规则矫正 (人工复核后应用, v0.3 暂隐藏)。
   模板: index.html <!-- VIEW: CONFIG --> 区段。
   ============================================ */
document.addEventListener('alpine:init', () => {
  Alpine.data('configPanel', () => ({
    configTab: 'rules',
    rulesCatalog: null,
    rulesEdit: {},
    initialRulesEdit: {},
    rulesSaving: false,
    scoringPresets: {},
    // AI 规则矫正: 建议仅在人工复核勾选后才应用
    // showCalibration=false: v0.3 暂隐藏入口 (后端能力保留), 启用时翻 true 即恢复
    showCalibration: false,
    calibration: {
      provider: 'deepseek', result: null, checked: {},
      showRaw: false, applying: false, loaded: false,
    },
    profileData: null,
    profileSaving: false,
    weightPresets: {},

    init() {
      this.loadRules();
      this.loadProfile();
      this.loadWeightPresets();
    },

    // ---- 规则参数 ----

    async loadRules() {
      const d = await this.$store.core.api('/api/rules');
      if (!d) return;
      this.rulesCatalog = d;
      // 从后端返回的 overrides 初始化编辑状态 (null = 用默认值)
      const ov = d.overrides || {};
      this.rulesEdit = { ...ov };
      this.initialRulesEdit = { ...ov };
      // 并行加载评分项配比预设
      const p = await this.$store.core.api('/api/scoring-config/presets');
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
            this.$store.core.toast(`${dim.label} 各项满分之和 ${sum} 超过封顶 10, 请调整后再保存`, 'error');
            return;
          }
        }
      }
      this.rulesSaving = true;
      const d = await this.$store.core.api('/api/scoring-config', 'PUT', this.rulesEdit);
      if (d && d.ok) {
        this.rulesEdit = { ...d.overrides };
        this.initialRulesEdit = { ...d.overrides };
        this.$store.core.toast('规则参数已保存, 请点击"规则打分"刷新分数', 'success');
      } else {
        this.$store.core.toast('保存失败, 请重试', 'error');
      }
      this.rulesSaving = false;
    },

    resetRulesEdit() {
      this.rulesEdit = { ...this.initialRulesEdit };
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
      const next = { ...this.rulesEdit };
      for (const [k, v] of Object.entries(p)) next[k] = v;
      this.rulesEdit = next;
      this.$store.core.toast(`已套用「${name}」配比预设, 记得点"保存规则参数"生效`, 'info');
    },

    // ---- AI 规则矫正 (人工复核后应用) ----

    async triggerCalibration() {
      const p = new URLSearchParams({ provider: this.calibration.provider });
      const d = await this.$store.core.api('/api/config/calibrate?' + p.toString(), 'POST');
      if (d && d.status === 'started') {
        this.$store.core.toast('AI 矫正已启动, 进度见日志面板', 'success');
        this.$store.core.logOpen = true;
      } else if (d && d.status === 'already_running') {
        this.$store.core.toast('矫正任务正在运行中', 'warning');
      } else {
        this.$store.core.toast('启动 AI 矫正失败', 'error');
      }
    },

    async loadCalibration() {
      const d = await this.$store.core.api('/api/config/calibrate/latest');
      if (d && d.result) {
        this.calibration.result = d.result;
        // 默认全选未应用的合法建议
        const checked = {};
        for (const s of (d.result.suggestions || [])) {
          checked[s.target + ':' + s.field] = !this.calibApplied(s);
        }
        this.calibration.checked = checked;
      } else {
        this.calibration.result = null;
        this.calibration.checked = {};
      }
      this.calibration.loaded = true;
    },

    calibKey(s) { return s.target + ':' + s.field; },

    calibApplied(s) {
      const applied = (this.calibration.result && this.calibration.result.applied) || [];
      return applied.some(a => a.field === s.target + ':' + s.field || a.field === s.field);
    },

    calibCheckedCount() {
      const items = (this.calibration.result && this.calibration.result.suggestions) || [];
      return items.filter(s => this.calibration.checked[this.calibKey(s)]).length;
    },

    // 字段中文标签 (画像字段 + 常见评分项)
    calibFieldLabel(s) {
      if (s.target === 'scoring') {
        const labels = {
          reject_confidence_floor: '淘汰置信度下限',
          f01_max: 'F-01 薪资下限达标', f02_max: 'F-02 薪资中位数达标', f03_max: 'F-03 薪资上限惊喜',
          f04_max: 'F-04 股票期权激励', f05_max: 'F-05 高价值福利', f06_max: 'F-06 薪资构成质量',
          g01_max: 'G-01 技术栈重合度', g02_max: 'G-02 公司阶段价值', g03_max: 'G-03 业务方向匹配',
          g04_max: 'G-04 团队规模契合', g05_max: 'G-05 经验学历兼容', g06_max: 'G-06 技术深度信号',
          g07_max: 'G-07 技术前瞻性',
          r01_max: 'R-01 城市匹配', r02_max: 'R-02 行业经验重叠', r03_max: 'R-03 公司体量偏好',
          r04_max: 'R-04 特殊资源', r05_max: 'R-05 文化适配信号',
          w01_max: 'W-01 工作模式弹性', w02_max: 'W-02 弹性福利信号', w03_max: 'W-03 企业规范性',
          w04_max: 'W-04 加班强度', w05_max: 'W-05 通勤便利',
        };
        return labels[s.field] || s.field;
      }
      const labels = {
        hard_min_salary_10k: '薪资硬下限(万)', expect_min_salary_10k: '期望最低薪资(万)',
        expect_max_salary_10k: '期望最高薪资(万)', max_commute_minutes: '最长通勤(分钟)',
        reject_scale_below: '公司规模下限(人)',
        weight_growth: '成长权重', weight_finance: '财务权重', weight_wlb: 'WLB权重', weight_resource: '资源权重',
        team_size_min: '团队规模下限', team_size_max: '团队规模上限',
      };
      return labels[s.field] || s.field;
    },

    async applyCalibration() {
      const r = this.calibration.result;
      if (!r || !r.file) return;
      const items = (r.suggestions || []).filter(s => this.calibration.checked[this.calibKey(s)]);
      if (!items.length) { this.$store.core.toast('请先勾选要应用的建议', 'warning'); return; }
      if (!confirm(`确认应用选中的 ${items.length} 条矫正建议? 将直接写入画像与规则配置。`)) return;

      this.calibration.applying = true;
      // 按 target 分组: profile 走 PUT /api/profile, scoring 走 PUT /api/scoring-config
      const profileBody = {};
      const scoringBody = {};
      const appliedFields = [];
      for (const s of items) {
        if (s.target === 'profile') profileBody[s.field] = s.suggested;
        else scoringBody[s.field] = s.suggested;
        appliedFields.push(this.calibKey(s));
      }
      let ok = true;
      if (Object.keys(profileBody).length) {
        const d = await this.$store.core.api('/api/profile', 'PUT', profileBody);
        if (!d || !d.ok) { ok = false; this.$store.core.toast('画像更新失败, 请重试', 'error'); }
      }
      if (ok && Object.keys(scoringBody).length) {
        const d = await this.$store.core.api('/api/scoring-config', 'PUT', scoringBody);
        if (!d || !d.ok) { ok = false; this.$store.core.toast('规则配置更新失败', 'error'); }
      }
      if (ok) {
        await this.$store.core.api('/api/config/calibrate/applied', 'POST', { file: r.file, fields: appliedFields });
        this.$store.core.toast('矫正建议已应用, 请点击"规则打分"刷新分数', 'success');
        await Promise.all([this.loadRules(), this.loadProfile(), this.loadCalibration()]);
      }
      this.calibration.applying = false;
    },

    // ---- 画像 ----

    async loadProfile() {
      const d = await this.$store.core.api('/api/profile');
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
      const d = await this.$store.core.api('/api/profile', 'PUT', this.profileData.fields);
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
        this.$store.core.toast('画像已保存, 建议点击"规则打分"刷新分数', 'success');
      } else {
        this.$store.core.toast('画像保存失败, 请重试', 'error');
      }
      this.profileSaving = false;
    },

    async loadWeightPresets() {
      const d = await this.$store.core.api('/api/profile/weight-presets');
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
      this.$store.core.toast(`已套用「${name}」预设, 记得点"保存画像"生效`, 'info');
    },

    // ---- 跨视图事件 ----

    // 后台任务状态变化: 矫正任务完成 → 刷新最新矫正结果
    onRefresh() {
      if (this.$store.core.view !== 'config') return;
      const calibDone = Object.values(this.$store.core.tasks).some(t => t.key.startsWith('config-calibrate-'));
      if (calibDone && this.showCalibration) this.loadCalibration();
    },
  }));
});
