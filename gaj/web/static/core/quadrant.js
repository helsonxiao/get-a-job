/* ============================================
   Get A Job — 公司象限散点图共享组件 (gajQuadrant)
   市场观察「公司象限」Tab / 行业观察详情区块 / 图鉴(旧) 共用。
   SVG 拼字符串走 x-html, 点击用事件委托 (circle[data-brand])。
   ============================================ */
(function () {
  const TIER = { S: 'tier-s', A: 'tier-a', B: 'tier-b', C: 'tier-c' };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  // 输入: items 列表 (company_score / salary_mid_avg / job_count / rank_tier / brand_id / name)
  function build(items) {
    const pts = [];
    const unknown = [];
    for (const c of items || []) {
      if (c.company_score == null) continue;
      if (c.salary_mid_avg == null) { unknown.push(c); continue; }
      pts.push(c);
    }
    const xMax = Math.max(20, ...pts.map(c => c.salary_mid_avg)) * 1.1;
    return { pts, unknown, xMax };
  }

  // viewBox 760x440, 绘图区 x:[60,730] y:[20,390]
  function xy(c, xMax) {
    return {
      x: 60 + (c.salary_mid_avg / xMax) * 670,
      y: 390 - (c.company_score / 10) * 370,
    };
  }

  function ticks(xMax) {
    const step = xMax > 60 ? 20 : 10;
    const out = [];
    for (let v = step; v <= xMax; v += step) out.push(v);
    return out;
  }

  function bubbleR(c) { return 7 + Math.min(13, (c.job_count || 1) * 1.6); }

  function gridSvg(qd) {
    let out = '';
    for (const v of ticks(qd.xMax)) {
      const x = (60 + (v / qd.xMax) * 670).toFixed(1);
      out += `<line x1="${x}" y1="390" x2="${x}" y2="20" class="qgrid"></line>`
           + `<text x="${x}" y="410" class="qaxis" text-anchor="middle">${v}</text>`;
    }
    for (const v of [2, 4, 6, 8, 10]) {
      const y = 390 - v * 37;
      out += `<line x1="60" y1="${y}" x2="730" y2="${y}" class="qgrid"></line>`
           + `<text x="52" y="${y + 3}" class="qaxis" text-anchor="end">${v}</text>`;
    }
    return out;
  }

  function bubblesSvg(qd) {
    return qd.pts.map(c => {
      const p = xy(c, qd.xMax);
      const title = esc(
        c.name + ' · 分 ' + (c.company_score != null ? c.company_score.toFixed(1) : '?')
        + ' · 薪资中位 ' + c.salary_mid_avg.toFixed(1) + '万 · ' + (c.job_count || 0) + '岗');
      return `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${bubbleR(c).toFixed(1)}" `
           + `class="qbubble ${TIER[c.rank_tier] || ''}" data-brand="${esc(c.brand_id)}">`
           + `<title>${title}</title></circle>`;
    }).join('');
  }

  // SVG 点击委托: 返回命中的 brand_id (无则 null)
  function hitBrand(e) {
    const t = e.target && e.target.closest ? e.target.closest('circle[data-brand]') : null;
    return t ? t.getAttribute('data-brand') : null;
  }

  window.gajQuadrant = { build, xy, ticks, bubbleR, gridSvg, bubblesSvg, hitBrand };
})();
