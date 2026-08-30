# 来源口径（source_link）使用手册

> 2026-08-31 · 适用: gaj v0.5.2+ (bundle schema 2.3) / gaj-reporter v0.2.0+
> 一句话: **一个 BOSS 筛选链接 = 一个数据口径**。入库带链接 → 按链接隔离出报告 → 报告标题用你的命名。

## 1. 字段含义

| 位置 | 字段 | 说明 |
|---|---|---|
| `jobs` 表 | `source_link TEXT` | 该岗位采集自哪个 BOSS 筛选列表页链接（口径唯一标识）；历史数据为空 |
| `source_links` 表 | `link / label / created_at` | 口径注册表：链接主键 + **自定义命名**（报告标题用）+ 首次登记时间 |
| bundle | `meta.scope` | `{scope_link, scope_label, job_count, total_job_count, unscoped_job_count}` |
| job.json | `source_link` | 文件真相源，reindex 后进索引 |

**未分口径** = 没有 `source_link` 的历史数据。它们保留在库里、参与全量报告，
但生成单口径报告时被显式排除，并在报告口径声明中报数（不会悄悄混入）。

## 2. 新数据入库（自动带链接）

```bash
python3 -m gaj crawl "https://www.zhipin.com/web/geek/jobs?city=101190200&position=100101&..."
```

采集 → 迁移 → 入库全链路自动把该列表页链接写入每条岗位的 `source_link`（并记入 provenance）。
无需额外操作。

## 3. 历史数据补归属（可选，手工）

```bash
# 按 job_id 精确归属 (写回 job.json + 重建索引)
python3 -m gaj scope-link assign --link "https://...筛选链接..." --job-ids "jobid1,jobid2"
```

不补归属也完全可以：历史数据保持「未分口径」，只影响单口径报告的覆盖面。

## 4. 查看口径与命名

```bash
python3 -m gaj scope-link list          # 链接/命名/岗位数 + 未分口径报数
python3 -m gaj scope-link rename --link "https://..." --label "无锡-后端-双休"
```

Web 图鉴：侧边栏「口径管理」—— 同样的列表与重命名入口，保存即生效。

## 5. 按口径出报告

```bash
# gaj 直出数据包
python3 -m gaj report-bundle --scope-link "https://..." 

# 报告生成器 (标题自动用命名)
python3 -m reporter --source cli --gaj-root /path/to/get-a-job \
  --version v1 --out-dir artifacts --scope-link "https://..."
```

- 标题：有命名 → `「无锡-后端-双休」无锡 计算机软件行业在招岗位…`；无命名 → 不加前缀（仍隔离）。
- 报告头部「数据口径」行 + 口径方法声明：明确显示本口径岗位数、全库岗位数、未分口径排除数。
- 不同口径的 bundle 指纹必然不同（口径链接掺入指纹）。

## 6. 隔离保证（原理）

`build_report_bundle(scope_link=...)` 用 **temp 表影子**（`temp.jobs` 覆盖同名表）过滤数据，
全部聚合代码零改动即在口径内运行；`finally` 中拆除影子，连接复用安全。
验收实测：奥特维口径 12 岗（中位 26.8 万）与先导口径 10 岗（中位 26.9 万）互不混算，
与直接 SQL 核对一致。

## 7. 注意事项

- 同一岗位只归属一个口径（字段是单值）；一岗多投口径时选主口径。
- `scope-link rename` 只改 `source_links` 表（标题展示），不影响岗位归属。
- 真实公司名/brand_id 仍不出现在 bundle；口径隔离只过滤范围，不改变脱敏规则。
- 历史回填不在本轮范围（成本高）；需要时用 `scope-link assign` 分批手工处理。
