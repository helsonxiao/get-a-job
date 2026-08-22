/* ============================================
   Get A Job — 主简历视图岛 (resumePanel)
   master.md 的上传 (.md 文件) / 粘贴编辑 / 保存。
   按职位定制生成简历的入口在职位详情 (jobsPanel)。
   模板: index.html <!-- VIEW: RESUME --> 区段。

   resumeExists 回写 $store.core 供 header 按钮标签显示。
   ============================================ */
document.addEventListener('alpine:init', () => {
  Alpine.data('resumePanel', () => ({
    resumeData: { content: '', exists: false, size: 0 },
    resumeSaving: false,
    resumeSavedAt: '',

    init() {
      this.loadResume();
    },

    async loadResume() {
      const d = await this.$store.core.api('/api/resume');
      if (d) {
        this.resumeData = d;
        this.$store.core.resumeExists = !!d.exists;
        if (d.exists && !this.resumeSavedAt) {
          this.resumeSavedAt = '已加载';
        }
      }
    },

    async saveResume() {
      const content = this.resumeData.content || '';
      this.resumeSaving = true;
      const d = await this.$store.core.api('/api/resume', 'PUT', { content });
      if (d && d.ok) {
        this.resumeData.exists = true;
        this.resumeData.size = d.size;
        this.$store.core.resumeExists = true;
        this.resumeSavedAt = new Date().toLocaleTimeString();
        this.$store.core.toast('简历已保存', 'success');
      } else {
        this.$store.core.toast('简历保存失败, 请重试', 'error');
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
  }));
});
