/* ============================================
   Get A Job — 来源口径管理视图岛 (scopePanel)
   口径链接列表 / 重命名 (用于报告标题) / 未分口径报数。
   模板: views/scope.html (loader 注入 index.html [data-tpl="scope"])。
   ============================================ */
document.addEventListener('alpine:init', () => {
  Alpine.data('scopePanel', () => ({
    data: {},
    loading: false,
    err: '',

    init() {
      this.reload();
    },

    async reload() {
      this.loading = true;
      this.err = '';
      try {
        const res = await fetch('/api/scope/links');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = await res.json();
        // _draft: 每行的重命名草稿 (Alpine 双向绑定用)
        body.links = (body.links || []).map((x) => ({ ...x, _draft: x.label || '', saving: false }));
        this.data = body;
      } catch (e) {
        this.err = `加载失败: ${e.message}`;
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
      } catch (e) {
        alert(`保存失败: ${e.message}`);
      } finally {
        item.saving = false;
      }
    },
  }));
});
