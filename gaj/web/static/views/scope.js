/* ============================================
   Get A Job — 来源口径管理视图岛 (scopePanel)
   列表 / 重命名 (报告标题) / 设为当前口径 (全局筛选) / 未分口径报数。
   模板: views/scope.html (loader 注入 index.html [data-tpl="scope"])。
   ============================================ */
document.addEventListener('alpine:init', () => {
  Alpine.data('scopePanel', () => ({
    data: {},
    loading: false,

    init() {
      this.reload();
    },

    async reload() {
      this.loading = true;
      try {
        const res = await fetch('/api/scope/links');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = await res.json();
        body.links = (body.links || []).map((x) => ({ ...x, _draft: x.label || '', saving: false }));
        this.data = body;
        // 同步侧边栏全局筛选的选项列表
        this.$store.core.scopeOptions = body.links;
      } catch (e) {
        this.$store.core.toast(`口径列表加载失败: ${e.message}`, 'error');
      } finally {
        this.loading = false;
        window.refreshIconsDebounced && window.refreshIconsDebounced();
      }
    },

    async save(item) {
      if (item.saving) return;
      item.saving = true;
      try {
        const res = await fetch('/api/scope/rename', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ link: item.link, label: (item._draft || '').trim() }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${res.status}`);
        }
        item.label = (item._draft || '').trim();
        this.$store.core.toast('命名已保存, 报告标题将使用该命名');
        this.$store.core.loadScopeOptions();
      } catch (e) {
        this.$store.core.toast(`保存失败: ${e.message}`, 'error');
      } finally {
        item.saving = false;
      }
    },

    setCurrent(item) {
      // 全局口径切换: 写入 store (localStorage 持久化) 并整页刷新,
      // 让职位/观察台/公司图鉴等全部只读视图按新口径重新拉取。
      this.$store.core.setScope(item.link, item.label || '');
    },
  }));
});
