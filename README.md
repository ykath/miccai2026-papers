# MICCAI 2026 论文库（本地数据库 + 浏览服务）

抓取 [MICCAI 2026 Open Access Reviews](https://papers.miccai.org/miccai-2026/) 全部论文信息（共 1165 篇），
存入本地 SQLite，用 DeepSeek 自动翻译**题目与摘要**为中文，并提供本地 Web 浏览服务。
不下载 PDF，全部使用远程链接。

数据来源包含每篇论文的：题目、作者、主题分类、摘要、BibTeX、公开评审意见（优点/缺点/评分/置信度）、
作者反馈、Meta-Review、代码仓库与数据集链接。

## 项目结构

```
scripts/
  scrape_miccai2026.py    # 抓取器：search.json 目录 + 1165 个详情页（断点续跑）
  translate_papers.py     # DeepSeek 翻译题目+摘要（API key 从注册表 HKCU\Environment 读取）
  analyze_papers.py       # 统计分析：读库生成 web/analysis_stats.js
  web_server.py           # 本地浏览服务（Python 标准库，无第三方依赖）
web/index.html            # 浏览界面（原生 JS 单文件）
web/analysis.html         # 统计分析页（ECharts，15 张图表）
data/miccai2026.sqlite    # 论文数据库（WAL + FTS5）
StartWeb.bat              # 一键启动浏览服务（http://127.0.0.1:8002）
```

## 使用

```bash
# 1. 抓取论文信息（支持断点续跑，中断后重跑即可）
python scripts/scrape_miccai2026.py

# 2. 翻译题目+摘要（deepseek-flash，key 自动从注册表读取）
python scripts/translate_papers.py --workers 4

# 3. 生成统计数据 + 启动浏览服务（或双击 StartWeb.bat）
python scripts/analyze_papers.py
python scripts/web_server.py --port 8002
```

浏览功能：中英文关键词搜索（多词与）、主题分类筛选、仅有代码筛选、分页；
详情页含中文摘要、English Abstract、评审意见（折叠面板，含评分/置信度）、
作者反馈、Meta-Review、数据集、BibTeX 复制、远程 PDF/补充材料/代码链接。

统计分析（主页右上「📊 统计分析」入口）：总览指标、官方主题分布、**评审质量分析**
（评分/置信度/最终意见/主题均分）、研究主题归类、中英文高频关键词、热点技术短语、
主题×技术热力图、开放科学（代码开源率）、高产作者 Top 20 与自动数据洞察；
子主题图表可点击跳转论文库对应筛选（`index.html?topic=...`）。

## 备注

- 抓取器内置混合网络策略：优先走系统代理（快），截断/超时自动切换直连（稳），
  并按 Content-Length 校验完整性（该站点大响应偶发截断）。
- 翻译需注册表 `HKCU\Environment` 中存在名称含 `deepseek` 的 API key 值；
  也可用 `--api-key` 或 `--api-key-env NAME` 指定。
- 常用参数：`--limit N`（试跑）、`--workers`（并发）、`--overwrite`（重新翻译）。
