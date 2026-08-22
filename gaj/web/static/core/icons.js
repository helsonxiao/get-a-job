/* ============================================
   Get A Job — Icon Refresh
   从 index.html 末尾内联 <script> 抽取, 提供全局 refreshIconsDebounced。
   用重入保护 + 防抖, 避免与 Alpine/自身 DOM 变化形成死循环。
   ============================================ */
(function () {
  let _iconRefreshing = false;
  let _iconRefreshTimer = null;

  function refreshIcons() {
    if (_iconRefreshing) return;
    if (!window.lucide || !window.lucide.createIcons) return;
    _iconRefreshing = true;
    try { window.lucide.createIcons(); } catch (e) {}
    _iconRefreshing = false;
  }

  // 防抖版本: 供 Alpine x-init/x-effect 调用, 合并多次数据变化
  function refreshIconsDebounced() {
    if (_iconRefreshTimer) clearTimeout(_iconRefreshTimer);
    _iconRefreshTimer = setTimeout(refreshIcons, 50);
  }

  // 暴露为全局, 供 x-effect="...; $nextTick(() => window.refreshIconsDebounced && window.refreshIconsDebounced())" 使用
  window.refreshIconsDebounced = refreshIconsDebounced;
  window.refreshIcons = refreshIcons;

  // 初次渲染
  document.addEventListener('DOMContentLoaded', refreshIconsDebounced);
  // Alpine 初始化完成
  document.addEventListener('alpine:initialized', refreshIconsDebounced);
})();
