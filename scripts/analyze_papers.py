"""MICCAI 2026 论文统计与趋势分析脚本。

读取 SQLite 数据库（论文题目、中英文摘要、主题分类、评审意见、作者），
基于透明可复核的关键词规则完成主题归类、高频词统计、评审质量分析等，
输出 web/analysis_stats.js（window.ANALYSIS_STATS = {...}）。

用法：
    python scripts/analyze_papers.py --db data/miccai2026.sqlite --out web/analysis_stats.js
"""

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# 停用词与词形归并（标题/摘要词频统计用）
# ---------------------------------------------------------------------------
STOPWORDS = set("""
a an the and or of to in for on with by from at as is are was were be been being
we our this that these those it its their his her he she they them us you your
into over under between within without through across per via not no nor but if
then than so such more most other some any each all both few many much own same
can could will would should may might must shall do does did done have has had
having based using use used uses via also however thus therefore furthermore
which who whom whose what when where why how while although though since until
about above below after before again against during out off up down here there
new novel paper propose proposed proposes present presents presented approach
method methods framework model models task tasks results show shown demonstrate
demonstrates performance state art achieve achieves achieved outperform
outperforms existing recent years work works study studies experimental
experiments evaluation evaluations dataset datasets data image images imaging
medical clinical deep learning neural network networks convolutional machine ai
accuracy robust efficient effective automatic automated high low different
various large small scale multi single two three first second end real time
world available code github http https com org www net io gitlab zenodo
introduce introduces introduced result results experiment experiments
due significant address addresses addressed module modules multiple region
regions strategy strategies improve improves improved improvements specific
several include includes included including public publicly limited extensive
compared specifically accurate field fields key consider considering well
especially respectively moreover namely overall thus overall across without
and/or non pre post sub vs etc
""".split())

NORMALIZE = {
    "models": "model", "networks": "network", "images": "image", "datasets": "dataset",
    "transformers": "transformer", "segmentations": "segmentation", "features": "feature",
    "representations": "representation", "methods": "method", "approaches": "approach",
    "tasks": "task", "modalities": "modality", "scans": "scan", "tumours": "tumor",
    "tumors": "tumor", "lesions": "lesion", "vessels": "vessel", "organs": "organ",
    "atlases": "atlas", "annotations": "annotation", "labels": "label",
    "classifications": "classification", "predictions": "prediction", "encoders": "encoder",
    "embeddings": "embedding", "prompts": "prompt", "agents": "agent", "volumes": "volume",
    "structures": "structure", "boundaries": "boundary", "regions": "region",
}

# ---------------------------------------------------------------------------
# 研究主题归类：主题 -> 正则列表（小写文本匹配，多标签；首个命中为主主题）
# ---------------------------------------------------------------------------
THEMES = [
    ("手术与介入导航", [
        r"\bsurgical\b", r"\bsurgery\b", r"\bsurgeries", r"\bendoscop", r"\blaparoscop",
        r"\brobot", r"\bcatheter", r"\bintervention", r"\bintraoperative",
        r"\btool segmentation", r"\bbronchoscop", r"\bcolonoscop",
    ]),
    ("图像重建与增强", [
        r"\breconstruction", r"\bsuper[- ]resolution", r"\bdenois", r"\binpainting",
        r"\bharmoniz", r"\bmotion correction", r"\bartifact", r"\bimage enhancement",
        r"\blow[- ]dose", r"\bsinogram", r"\bk[- ]space", r"\bcompressed sensing",
    ]),
    ("图像配准", [r"\bregistration\b", r"\bdeformable", r"\bimage alignment"]),
    ("生成模型与数据合成", [
        r"\bdiffusion\b", r"\bgenerative\b", r"\bgan\b|\bgans\b", r"\btext[- ]to[- ]image",
        r"\bimage synthesis", r"\bsynthetic (data|image|images|sample)", r"\bdata synthesis",
        r"\bcounterfactual",
    ]),
    ("基础模型与大模型", [
        r"\bfoundation model", r"\blarge language model", r"\bllm\b|\bllms\b",
        r"\bvision[- ]language", r"\bvisual[- ]language", r"\bclip\b",
        r"\bsegment anything|\bsam\b|\bsam2\b", r"\bprompt", r"\bmultimodal large",
        r"\bvlm\b", r"\bchatgpt|\bgpt[- ]", r"\bagentic|\bagent", r"\bmamba",
    ]),
    ("自监督与高效学习", [
        r"\bself[- ]supervised", r"\bpretrain", r"\bpre[- ]train", r"\bcontrastive learning",
        r"\bmasked autoencoder", r"\bssl\b", r"\bsemi[- ]supervised", r"\bweakly[- ]supervised",
        r"\bfew[- ]shot", r"\bzero[- ]shot", r"\bparameter[- ]efficient", r"\bquantiz",
        r"\bdistill",
    ]),
    ("图像分割", [r"\bsegmentation", r"\bsegment\b", r"\bsegmenting", r"\bdelineation", r"\bparcellation"]),
    ("目标检测与定位", [r"\bdetection", r"\bdetect\b", r"\blocalization", r"\blocaliz", r"\blandmark"]),
    ("分类与诊断预测", [
        r"\bclassification", r"\bclassif", r"\bdiagnos", r"\bprognos", r"\boutcome",
        r"\bgrading", r"\bstaging", r"\bscreening", r"\bsurvival", r"\bdisease progression",
        r"\btreatment response", r"\brisk prediction",
    ]),
    ("联邦学习与隐私保护", [r"\bfederated", r"\bprivacy", r"\bdifferential privacy", r"\bsecure\b"]),
    ("可解释性与不确定性", [
        r"\buncertainty", r"\bexplainab", r"\binterpretab", r"\bcalibration",
        r"\btrustworth", r"\bfairness", r"\bconcept\b",
    ]),
    ("多模态融合", [r"\bmulti[- ]modal", r"\bmultimodal", r"\bfusion"]),
    ("数据集与评测基准", [r"\bbenchmark", r"\bchallenge\b", r"\bleaderboard", r"\bevaluation suite"]),
]

# 热点技术短语：标签 -> 正则（按 2026 热点更新）
HOT_PHRASES = {
    "扩散模型 diffusion": r"\bdiffusion\b",
    "基础模型 foundation model": r"\bfoundation model",
    "大语言模型 LLM/MLLM": r"\blarge language model|\bllm\b|\bllms\b|\bmllm",
    "视觉语言模型 VLM/CLIP": r"\bvision[- ]language|\bvisual[- ]language|\bclip\b|\bvlm\b",
    "SAM / Segment Anything": r"\bsegment anything|\bsam\b|\bsam2\b",
    "Agent / 智能体": r"\bagentic|\bagents?\b|\bmulti[- ]agent",
    "Mamba / 状态空间模型": r"\bmamba\b|\bstate[- ]space model",
    "自监督学习": r"\bself[- ]supervised",
    "对比学习": r"\bcontrastive learning|\bcontrastive loss",
    "掩码自编码 MAE": r"\bmasked autoencoder|\bmae\b|\bmask(ed)? (image|token|patch)",
    "Transformer": r"\btransformer",
    "联邦学习": r"\bfederated",
    "不确定性量化": r"\buncertainty\b|\buncertainties\b",
    "可解释/可解释性": r"\bexplainab|\binterpretab|\bexplainability",
    "域适应/域泛化": r"\bdomain adaptation|\bdomain generalization|\bdomain shift|\bout[- ]of[- ]distribution|\bood\b",
    "弱/半监督": r"\bweakly[- ]supervised|\bsemi[- ]supervised",
    "少样本/零样本": r"\bfew[- ]shot|\bzero[- ]shot",
    "异常检测": r"\banomaly detection|\banomal",
    "图神经网络 GNN": r"\bgraph neural|\bgnn\b|\bgraph convolution",
    "强化学习": r"\breinforcement learning",
    "知识蒸馏": r"\bknowledge distillation|\bdistill",
    "3D / 体积数据": r"\b3d\b|\bvolumetric",
    "NeRF / 隐式表示": r"\bnerf\b|\bneural radiance|\bimplicit representation|\bgaussian splat",
    "测试时适应/推理": r"\btest[- ]time",
    "合成数据/数据增强": r"\bsynthetic data|\bdata augmentation|\bdata synthesis",
    "隐私保护": r"\bprivacy|\bdifferential privacy",
    "标注高效 label-efficient": r"\blabel[- ]efficient|\bannotation[- ]efficient|\blimited annotation",
}

# 主题 × 热点技术热力图技术列表
HEATMAP_TECHS = [
    ("扩散模型", r"\bdiffusion\b"),
    ("基础模型", r"\bfoundation model"),
    ("LLM/Agent", r"\blarge language model|\bllm\b|\bagentic|\bagent"),
    ("视觉语言/CLIP", r"\bvision[- ]language|\bvisual[- ]language|\bclip\b|\bvlm\b"),
    ("SAM", r"\bsegment anything|\bsam\b|\bsam2\b"),
    ("自监督", r"\bself[- ]supervised|\bcontrastive learning|\bmasked autoencoder|\bpretrain"),
    ("Transformer", r"\btransformer"),
    ("联邦学习", r"\bfederated"),
    ("3D/高斯泼溅", r"\b3d\b|\bgaussian splat|\bnerf"),
    ("不确定性", r"\buncertainty"),
]

# 中文摘要术语词典（利用 DeepSeek 翻译后的 abstract_zh 统计）
ZH_TERMS = {
    "深度学习": r"深度学习", "分割": r"分割", "重建": r"重建", "配准": r"配准",
    "分类": r"分类", "检测": r"检测", "大语言模型": r"大语言模型|大模型",
    "基础模型": r"基础模型", "扩散模型": r"扩散模型", "多模态": r"多模态",
    "注意力机制": r"注意力机制|注意力", " Transformer": r"transformer",
    "图神经网络": r"图神经网络", "联邦学习": r"联邦学习", "自监督": r"自监督",
    "不确定性": r"不确定性", "可解释": r"可解释", "手术": r"手术",
    "机器人": r"机器人", "病理": r"病理", "超声": r"超声",
    "三维": r"三维|3D", "生成": r"生成式|生成模型", "增强现实": r"增强现实|混合现实|虚拟现实",
    "少样本": r"少样本|小样本", "零样本": r"零样本", "数据增强": r"数据增强",
    "分割 anything(SAM)": r"\bSAM\b|分割一切",
    "智能体": r"智能体", "临床": r"临床", "肿瘤": r"肿瘤|癌症",
    "心脏": r"心脏", "脑": r"\b脑\b|大脑", "肺部": r"肺",
}


def norm_token(tok: str) -> str:
    if tok in NORMALIZE:
        return NORMALIZE[tok]
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 MICCAI 2026 统计分析数据。")
    parser.add_argument("--db", default="data/miccai2026.sqlite", help="SQLite 数据库路径。")
    parser.add_argument("--out", default="web/analysis_stats.js", help="输出 JS 文件路径。")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    papers = conn.execute(
        """
        SELECT paper_no, title, abstract, abstract_zh, authors, topics,
               code_url IS NOT NULL AND COALESCE(code_url,'')<>'' AS has_code,
               supp_url IS NOT NULL AND COALESCE(supp_url,'')<>'' AS has_supp,
               dataset_info IS NOT NULL AND COALESCE(dataset_info,'')<>'' AS has_dataset,
               reviews
        FROM papers ORDER BY paper_no
        """
    ).fetchall()
    conn.close()

    total = len(papers)

    # ---- 官方主题分类 ----
    group_counter: Counter = Counter()
    subtopic_counter: Counter = Counter()
    group_papers = defaultdict(set)
    subtopic_papers = defaultdict(set)
    for p in papers:
        for topic in re.split(r"\s*;\s*", p["topics"] or ""):
            if not topic:
                continue
            subtopic_counter[topic] += 1
            subtopic_papers[topic].add(p["paper_no"])
            group = topic.split(" -> ")[0].strip()
            if group:
                group_counter[group] += 1
                group_papers[group].add(p["paper_no"])

    # ---- 关键词统计（标题权重 3 倍）----
    token_counter: Counter = Counter()
    token_doc: Counter = Counter()
    token_re = re.compile(r"[a-z][a-z0-9\-]{2,}")
    for p in papers:
        title = (p["title"] or "").lower()
        abstract = (p["abstract"] or "").lower()
        doc_tokens = set()
        for tok in token_re.findall(abstract):
            t = norm_token(tok.strip("-"))
            if tok not in STOPWORDS and t not in STOPWORDS and not t.isdigit():
                token_counter[t] += 1
                doc_tokens.add(t)
        for tok in token_re.findall(title):
            t = norm_token(tok.strip("-"))
            if tok not in STOPWORDS and t not in STOPWORDS and not t.isdigit():
                token_counter[t] += 3
                doc_tokens.add(t)
        token_doc.update(doc_tokens)

    # ---- 中文摘要术语 ----
    zh_counter: Counter = Counter()
    for p in papers:
        zh = p["abstract_zh"] or ""
        for label, pat in ZH_TERMS.items():
            if re.search(pat, zh, re.I):
                zh_counter[label] += 1

    # ---- 热点短语 ----
    phrase_doc: Counter = Counter()
    phrase_titles = defaultdict(list)
    paper_text = {}
    for p in papers:
        text = ((p["title"] or "") + " " + (p["abstract"] or "")).lower()
        paper_text[p["paper_no"]] = text
        for label, pat in HOT_PHRASES.items():
            if re.search(pat, text):
                phrase_doc[label] += 1
                if len(phrase_titles[label]) < 6:
                    phrase_titles[label].append(p["title"])

    # ---- 研究主题归类 ----
    theme_doc: Counter = Counter()
    primary_theme: Counter = Counter()
    theme_examples = defaultdict(list)
    paper_themes = {}
    for p in papers:
        text = paper_text[p["paper_no"]]
        matched = []
        for theme, pats in THEMES:
            if any(re.search(pat, text) for pat in pats):
                matched.append(theme)
        if not matched:
            matched = ["其他方向"]
        paper_themes[p["paper_no"]] = matched
        theme_doc.update(set(matched))
        primary_theme[matched[0]] += 1
        for th in matched[:2]:
            if len(theme_examples[th]) < 5:
                theme_examples[th].append(p["title"])

    # ---- 主题 × 技术热力图 ----
    tech_list = [t for t, _ in HEATMAP_TECHS]
    heatmap = {theme: {t: 0 for t in tech_list} for theme, _ in THEMES}
    heatmap["其他方向"] = {t: 0 for t in tech_list}
    for p in papers:
        text = paper_text[p["paper_no"]]
        for theme in paper_themes[p["paper_no"]]:
            for tech, pat in HEATMAP_TECHS:
                if re.search(pat, text):
                    heatmap[theme][tech] += 1

    # ---- 评审质量 ----
    rating_dist = Counter()
    conf_dist = Counter()
    final_dist = Counter()
    paper_ratings = defaultdict(list)
    total_reviews = 0
    rating_sum = 0
    for p in papers:
        try:
            reviews = json.loads(p["reviews"] or "[]")
        except json.JSONDecodeError:
            reviews = []
        for r in reviews:
            total_reviews += 1
            if r.get("rating") is not None:
                rating_dist[r["rating"]] += 1
                rating_sum += r["rating"]
                paper_ratings[p["paper_no"]].append(r["rating"])
            if r.get("confidence") is not None:
                conf_dist[r["confidence"]] += 1
            if r.get("final"):
                final_dist[r["final"].strip()] += 1

    # 各官方主题组:平均评分/开源率
    group_quality = []
    for group in group_counter:
        nos = group_papers[group]
        ratings = [x for no in nos for x in paper_ratings.get(no, [])]
        codes = sum(1 for p in papers if p["paper_no"] in nos and p["has_code"])
        group_quality.append({
            "name": group,
            "papers": len(nos),
            "avgRating": round(sum(ratings) / len(ratings), 2) if ratings else None,
            "codeRate": round(codes / len(nos) * 100, 1),
        })
    group_quality.sort(key=lambda g: -g["papers"])

    # ---- 主题平均评分 ----
    theme_quality = []
    no_to_themes = {p["paper_no"]: paper_themes[p["paper_no"]] for p in papers}
    for theme, _ in THEMES + [("其他方向", [])]:
        nos = [no for no, ths in no_to_themes.items() if theme in ths]
        ratings = [x for no in nos for x in paper_ratings.get(no, [])]
        if ratings:
            theme_quality.append({"name": theme, "avgRating": round(sum(ratings) / len(ratings), 2), "n": len(nos)})
    theme_quality.sort(key=lambda t: -t["avgRating"])

    # ---- 作者统计 ----
    author_counter: Counter = Counter()
    author_count_dist = Counter()
    for p in papers:
        authors = [a.strip() for a in (p["authors"] or "").split(";") if a.strip()]
        author_count_dist[min(len(authors), 15)] += 1
        author_counter.update(authors)

    # ---- 摘要长度 ----
    def wc(s):
        return len((s or "").split())
    en_lens = [wc(p["abstract"]) for p in papers if p["abstract"]]
    zh_lens = [len(re.sub(r"\s", "", p["abstract_zh"] or "")) for p in papers if p["abstract_zh"]]

    result = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "overview": {
            "total": total,
            "withCode": sum(1 for p in papers if p["has_code"]),
            "codeRatio": round(sum(1 for p in papers if p["has_code"]) / total * 100, 1),
            "withSupp": sum(1 for p in papers if p["has_supp"]),
            "withDataset": sum(1 for p in papers if p["has_dataset"]),
            "totalReviews": total_reviews,
            "avgRating": round(rating_sum / sum(rating_dist.values()), 2) if rating_dist else None,
            "avgAuthors": round(sum(author_count_dist[k] * k for k in author_count_dist) / total, 1),
            "avgAbstractEn": round(sum(en_lens) / len(en_lens)) if en_lens else 0,
            "avgAbstractZh": round(sum(zh_lens) / len(zh_lens)) if zh_lens else 0,
        },
        "topicGroups": [
            {"name": k, "count": len(group_papers[k]), "ratio": round(len(group_papers[k]) / total * 100, 1)}
            for k, _ in group_counter.most_common()
        ],
        "subtopics": [
            {"name": k, "count": v, "ratio": round(v / total * 100, 1)}
            for k, v in subtopic_counter.most_common(30)
        ],
        "themes": [
            {"name": k, "count": v, "ratio": round(v / total * 100, 1),
             "primary": primary_theme.get(k, 0), "examples": theme_examples.get(k, [])}
            for k, v in theme_doc.most_common()
        ],
        "keywords": [
            {"word": k, "count": v, "docs": token_doc.get(k, 0)}
            for k, v in token_counter.most_common(60)
        ],
        "zhTerms": [{"name": k, "count": zh_counter[k], "ratio": round(zh_counter[k] / total * 100, 1)}
                    for k in ZH_TERMS if zh_counter[k] > 0],
        "phrases": [
            {"name": k, "count": phrase_doc[k], "ratio": round(phrase_doc[k] / total * 100, 1),
             "examples": phrase_titles[k][:4]}
            for k in sorted(HOT_PHRASES, key=lambda x: -phrase_doc[x]) if phrase_doc[k] > 0
        ],
        "heatmap": {
            "themes": [t for t, _ in THEMES] + ["其他方向"],
            "techs": tech_list,
            "data": [[heatmap[t][tech] for tech in tech_list] for t, _ in THEMES] +
                    [[heatmap["其他方向"][tech] for tech in tech_list]],
        },
        "ratings": {
            "dist": {str(k): rating_dist.get(k, 0) for k in range(1, 7)},
            "confidence": {str(k): conf_dist.get(k, 0) for k in range(1, 5)},
            "final": dict(final_dist.most_common()),
            "themeQuality": theme_quality,
            "groupQuality": group_quality,
            "reviewsPerPaper": {str(k): sum(1 for no, rl in paper_ratings.items() if len(rl) == k)
                                for k in range(1, 7)},
        },
        "authors": {
            "top": [{"name": k, "count": v} for k, v in author_counter.most_common(20)],
            "countDist": {str(k if k < 15 else 15): v for k, v in sorted(author_count_dist.items())},
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write("// 由 scripts/analyze_papers.py 自动生成，请勿手工修改\n")
        f.write("window.ANALYSIS_STATS = ")
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    print(f"分析完成: {out_path}")
    print(f"论文 {total} 篇 | 评审 {total_reviews} 条 | 主题组 {len(group_counter)} | "
          f"热点短语 {len(result['phrases'])} | 高产作者首位 {result['authors']['top'][0]}")


if __name__ == "__main__":
    main()
