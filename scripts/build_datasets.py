"""Build the labelled dedup datasets under eval/datasets/dedup/.

All organisations, products and events are fictional. Each item carries a
``label`` with its gold cluster, the case type it exercises, and whether it is
``critical`` (it carries information found nowhere else and must never be
dropped). The evaluator strips labels before candidate code sees the items.

Run from the repo root:  python3 scripts/build_datasets.py
"""
from __future__ import annotations

import hashlib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "eval", "datasets", "dedup")


def item(iid, cluster, case, url, title, text, lang, critical=False, published="2026-10-20"):
    return {"id": iid, "url": url, "title": title, "text": text, "lang": lang,
            "published": published,
            "label": {"cluster": cluster, "case": case, "critical": critical}}


def reprint_en(base, iid, url, site):
    return item(iid, base["label"]["cluster"], "reprint", url,
                base["title"] + " | " + site,
                base["text"] + " Originally published by the original outlet and republished by %s." % site,
                base["lang"])


def reprint_zh(base, iid, url, site):
    return item(iid, base["label"]["cluster"], "reprint", url,
                "【转载】" + base["title"],
                base["text"] + "（本文转载自原发布方，%s 编辑整理。）" % site,
                base["lang"])


def variant(base, iid, case, url):
    return item(iid, base["label"]["cluster"], case, url, base["title"], base["text"], base["lang"])


# ---------------------------------------------------------------- dev split
def dev():
    a = item("d01", "c01", "original", "https://news.novalab.example/blog/orion-7-open-weights",
             "NovaLab releases Orion-7 open-weight model",
             "NovaLab today released Orion-7, a 70-billion-parameter language model with open weights under "
             "an Apache-2.0 license. The company says Orion-7 matches its previous closed model on coding "
             "benchmarks while running on a single eight-GPU server.", "en", critical=True)
    b = item("d06", "c02", "same-topic-new-info", "https://news.novalab.example/blog/orion-7-api-pricing",
             "NovaLab cuts Orion-7 API prices by 60 percent",
             "Two weeks after the Orion-7 launch, NovaLab cut hosted API prices by 60 percent and added a "
             "batch tier. Self-hosting customers are unaffected; the open weights remain under Apache-2.0.",
             "en", critical=True)
    c = item("d07", "c03", "original", "https://www.xinghe-lab.example/news/lumen-1-0",
             "星河实验室开源向量数据库 Lumen 1.0",
             "星河实验室宣布开源向量数据库 Lumen 1.0，采用 MIT 许可证。Lumen 支持十亿级向量检索，"
             "单机写入吞吐达到每秒二十万条，并提供 Python 与 Go 客户端。", "zh", critical=True)
    d = item("d10", "c04", "same-topic-different-event", "https://www.xinghe-lab.example/news/lumen-1-1",
             "星河实验室发布 Lumen 1.1，修复数据丢失漏洞",
             "Lumen 1.1 修复了在节点异常重启时可能丢失最近写入数据的严重漏洞，建议所有 1.0 用户尽快升级。"
             "新版本同时把压缩索引的内存占用降低了三成。", "zh", critical=True)
    e = item("d11", "c05", "original", "https://blog.rust-lang.example/2026/10/16/rust-1.92.html",
             "Announcing Rust 1.92",
             "The Rust team is happy to announce Rust 1.92. This release stabilizes async closures in trait "
             "objects, adds the `Vec::extract_if` method and speeds up incremental builds by about 12 percent.",
             "en", critical=True)
    f = item("d12", "c05", "near-duplicate-title", "https://devweekly.example/p/rust-192-is-out",
             "Rust 1.92 is out: async closures in trait objects",
             "The Rust team is happy to announce Rust 1.92. This release stabilizes async closures in trait "
             "objects, adds the `Vec::extract_if` method and speeds up incremental builds by about 12 percent. "
             "Upgrade with rustup update stable.", "en")
    g = item("d13", "c06", "same-topic-different-event", "https://blog.rust-lang.example/2026/11/27/rust-1.93.html",
             "Announcing Rust 1.93",
             "The Rust team has published Rust 1.93. Highlights include stable inline const patterns, a new "
             "`#[diagnostic::on_move]` attribute and support for the aarch64 Windows target as tier 1.",
             "en", critical=True)
    h = item("d14", "c07", "original", "https://www.cityauto.example/policy/2026/robotaxi-rules",
             "滨江市发布自动驾驶道路测试新规",
             "滨江市交通局发布自动驾驶道路测试管理办法，允许无安全员车辆在夜间指定区域开展测试，"
             "测试企业须每月公开脱离率与事故数据。新规自十一月一日起施行。", "zh", critical=True)
    i = item("d15", "c07", "near-duplicate-title", "https://auto-daily.example/a/88231",
             "夜间无人测试放开：滨江自动驾驶新规解读",
             "滨江市交通局发布自动驾驶道路测试管理办法，允许无安全员车辆在夜间指定区域开展测试，"
             "测试企业须每月公开脱离率与事故数据。新规自十一月一日起施行。业内人士认为这将加快商业化进程。", "zh")
    j = item("d16", "c08", "original", "https://kubernetes.example/blog/2026/10/k8s-1-36-release/",
             "Kubernetes 1.36 removes the legacy in-tree cloud providers",
             "Kubernetes 1.36 completes the removal of the legacy in-tree cloud provider code. Cluster "
             "operators must switch to external cloud-controller-managers before upgrading.", "en", critical=True)
    k = item("d18", "c09", "original", "https://openmaint.example/2026/ai-pr-surge",
             "Open-source maintainers report a surge in AI-generated pull requests",
             "A survey of 400 open-source maintainers found that AI-generated pull requests tripled this year. "
             "Most maintainers said such PRs take longer to review because tests are often missing.", "en",
             critical=True)
    k_zh = item("d19", "c09", "cross-language", "https://kaiyuan.example/news/ai-pr-surge",
                "开源维护者称 AI 生成的拉取请求激增",
                "一项针对 400 名开源维护者的调查发现，今年 AI 生成的拉取请求增加了两倍。"
                "多数维护者表示，这类请求往往缺少测试，审查耗时更长。", "zh")
    m = item("d20", "c10", "original", "https://papers.example/zh/long-context-retrieval",
             "论文：用分层索引改进长上下文检索",
             "研究者提出一种分层索引方法，把长文档切分为段落、章节与全文三级摘要，"
             "在百万字级别的问答数据上将召回率提高了九个百分点。", "zh", critical=True)
    n = item("d21", "c11", "original", "https://security.example/advisories/CVE-2026-31337",
             "CVE-2026-31337: heap overflow in libquill, patch available",
             "A heap overflow in libquill's PNG decoder (CVE-2026-31337) allows remote code execution via "
             "crafted images. Version 2.4.1 fixes the issue; distributions are shipping updates.", "en",
             critical=True)
    o = item("d22", "c12", "same-topic-new-info", "https://security.example/news/libquill-exploited",
             "libquill CVE-2026-31337 now exploited in the wild",
             "Attackers are actively exploiting CVE-2026-31337 in libquill through malicious avatars on chat "
             "platforms. CISA added the flaw to its catalog and set a two-week patch deadline.", "en",
             critical=True)
    p = item("d23", "c13", "original", "https://openforge.example/blog/review-bot",
             "OpenForge launches an open-source code review bot",
             "OpenForge released Sentry Review, a self-hostable bot that comments on pull requests with "
             "static-analysis findings and suggested fixes. It supports GitLab and Gitea.", "en", critical=True)
    q = item("d25", "c14", "original", "https://www.ai-weekly.example/2026/43/chip-export",
             "芯片出口新规影响国内大模型训练成本",
             "新的芯片出口限制将高端训练卡的交付周期延长至九个月，多家创业公司表示训练成本上升约四成，"
             "部分团队转向国产加速卡与混合精度方案。", "zh", critical=True)
    return [
        a,
        variant(a, "d02", "exact-url", a["url"]),
        variant(a, "d03", "tracking-params",
                a["url"] + "?utm_source=twitter&utm_medium=social&utm_campaign=launch"),
        reprint_en(a, "d04", "https://technews.example/2026/10/novalab-orion-7", "TechNews"),
        variant(a, "d05", "tracking-params", a["url"] + "?fbclid=IwAR3xYz"),
        b, c,
        variant(c, "d08", "scheme-and-www", "http://xinghe-lab.example/news/lumen-1-0?spm=a2c4g.11186623"),
        reprint_zh(c, "d09", "https://www.kaiyuan.example/news/2026/lumen-1-0", "开源资讯"),
        d, e, f, g, h, i, j,
        variant(j, "d17", "fragment-and-slash", "https://kubernetes.example/blog/2026/10/k8s-1-36-release#upgrade"),
        k, k_zh, m, n, o, p,
        variant(p, "d24", "tracking-params", p["url"] + "?ref=hn&utm_source=hackernews"),
        q,
        reprint_zh(q, "d26", "https://news.chip-observer.example/p/7781", "芯观察"),
    ]


# ------------------------------------------------------------ holdout split
def holdout():
    a = item("h01", "k01", "original", "https://quanta-ml.example/blog/sparrow-3",
             "Quanta ML ships Sparrow-3, a speech model for low-resource languages",
             "Quanta ML released Sparrow-3, a speech recognition model covering 140 languages, including 60 "
             "with fewer than ten hours of training audio. The model and evaluation sets are available on "
             "its research hub.", "en", critical=True)
    b = item("h05", "k02", "same-topic-different-event", "https://quanta-ml.example/blog/sparrow-3-tts",
             "Quanta ML adds text-to-speech to the Sparrow family",
             "Quanta ML introduced Sparrow-TTS, a voice synthesis model trained on licensed audiobooks. "
             "Unlike the recognition model, Sparrow-TTS is only offered through a paid API.", "en", critical=True)
    c = item("h06", "k03", "original", "https://www.yunfan.example/press/2026-10-18",
             "云帆科技发布开源编程助手 Pilot 2.0",
             "云帆科技发布开源编程助手 Pilot 2.0，新增多文件重构与测试生成功能，"
             "支持在本地运行十四亿参数的代码模型，项目采用 Apache-2.0 许可证。", "zh", critical=True)
    d = item("h09", "k04", "same-topic-new-info", "https://www.yunfan.example/press/2026-10-25",
             "云帆 Pilot 2.0 被曝存在远程代码执行漏洞",
             "安全研究人员发现 Pilot 2.0 的插件市场在安装扩展时未校验签名，攻击者可借此执行任意代码。"
             "云帆科技表示将在 2.0.3 版本中修复。", "zh", critical=True)
    e = item("h10", "k05", "original", "https://pg.example/about/news/postgresql-19-beta-1",
             "PostgreSQL 19 Beta 1 released",
             "The PostgreSQL Global Development Group announced the first beta of PostgreSQL 19. New features "
             "include native column-level encryption, faster logical replication slots and SQL/JSON tables.",
             "en", critical=True)
    f = item("h11", "k05", "near-duplicate-title", "https://dbnews.example/postgres-19-beta-lands",
             "Postgres 19 beta lands with column-level encryption",
             "The PostgreSQL Global Development Group announced the first beta of PostgreSQL 19. New features "
             "include native column-level encryption, faster logical replication slots and SQL/JSON tables. "
             "The final release is expected in September.", "en")
    g = item("h12", "k06", "same-topic-different-event", "https://pg.example/about/news/postgresql-18-3",
             "PostgreSQL 18.3, 17.7 and 16.11 released",
             "The PostgreSQL Global Development Group has released updates to all supported versions, fixing "
             "two security issues in the query planner and over 40 bugs reported since the last update.",
             "en", critical=True)
    h = item("h13", "k07", "original", "https://www.edu-tech.example/2026/10/ai-homework",
             "教育部发布中小学生成式人工智能使用指引",
             "教育部发布中小学生成式人工智能使用指引，要求学校不得将 AI 工具用于替代学生独立完成作业，"
             "同时鼓励教师在课堂中使用 AI 进行个性化辅导。", "zh", critical=True)
    i = item("h14", "k07", "near-duplicate-title", "https://jiaoyu-news.example/p/55120",
             "不能用 AI 代写作业：教育部新指引要点",
             "教育部发布中小学生成式人工智能使用指引，要求学校不得将 AI 工具用于替代学生独立完成作业，"
             "同时鼓励教师在课堂中使用 AI 进行个性化辅导。指引还要求学校建立 AI 使用登记制度。", "zh")
    j = item("h15", "k08", "original", "https://helix.example/blog/helix-editor-25-10/",
             "Helix editor 25.10 adds a plugin system",
             "The Helix modal editor shipped version 25.10 with an experimental Scheme-based plugin system, "
             "inline diagnostics and a built-in file explorer.", "en", critical=True)
    k = item("h17", "k09", "original", "https://climate-data.example/2026/arctic-ice",
             "Arctic sea ice reaches a record low for October",
             "Satellite data show Arctic sea ice extent fell to 5.1 million square kilometers in October, the "
             "lowest for the month since records began in 1979.", "en", critical=True)
    k_zh = item("h18", "k09", "cross-language", "https://qihou.example/news/arctic-ice-oct",
                "北极海冰十月面积创历史新低",
                "卫星数据显示，十月北极海冰面积降至 510 万平方公里，为 1979 年有记录以来同期最低。", "zh")
    m = item("h19", "k10", "original", "https://papers.example/en/kv-cache-quant",
             "Paper: 2-bit KV cache quantization with negligible quality loss",
             "Researchers show that grouping keys by attention head and quantizing values per token enables "
             "2-bit KV caches with less than 0.3 points of quality loss on long-context benchmarks.", "en",
             critical=True)
    n = item("h20", "k11", "original", "https://www.mobile-world.example/2026/10/foldable",
             "折叠屏手机出货量同比增长 45%",
             "市场研究机构数据显示，第三季度国内折叠屏手机出货量同比增长 45%，"
             "其中竖折机型占比首次超过横折机型。", "zh", critical=True)
    o = item("h22", "k12", "same-topic-different-event", "https://www.mobile-world.example/2026/10/foldable-price",
             "多家厂商下调折叠屏手机售价",
             "受上游屏幕成本下降影响，三家主要厂商宣布下调折叠屏机型售价，"
             "入门款价格首次降至五千元以下。", "zh", critical=True)
    p = item("h23", "k13", "original", "https://wasm.example/news/component-model-1-0",
             "WebAssembly Component Model reaches 1.0",
             "The Bytecode Alliance declared the WebAssembly Component Model stable at version 1.0, with "
             "toolchain support in Rust, Go and JavaScript and a frozen WIT interface format.", "en",
             critical=True)
    q = item("h25", "k14", "original", "https://www.robot-news.example/2026/humanoid-factory",
             "人形机器人首次进入汽车总装线试点",
             "一家车企宣布在总装车间试点部署二十台人形机器人，负责搬运与质检工序，"
             "试点期为六个月，目标是将单台车的装配时间缩短百分之八。", "zh", critical=True)
    r = item("h27", "k15", "same-topic-different-event", "https://pg.example/about/news/postgresql-19-beta-2",
             "PostgreSQL 19 Beta 2 released",
             "The second beta drops the experimental planner hints introduced in Beta 1 after performance "
             "regressions were reported, and changes the on-disk format, so a dump and restore is required.",
             "en", critical=True)
    s = item("h28", "k16", "same-topic-different-event", "https://www.edu-tech.example/2026/10/ai-university",
             "教育部发布高校生成式人工智能使用指引",
             "面向高等院校的指引允许研究生在论文写作中使用 AI 润色，但须在致谢中注明工具名称，"
             "禁止使用 AI 生成实验数据，违者按学术不端处理。", "zh", critical=True)
    return [
        a,
        variant(a, "h02", "tracking-params", a["url"] + "?utm_source=newsletter&utm_medium=email"),
        reprint_en(a, "h03", "https://ai-roundup.example/2026/10/sparrow-3", "AI Roundup"),
        variant(a, "h04", "scheme-and-www", "http://www.quanta-ml.example/blog/sparrow-3/"),
        b, c,
        variant(c, "h07", "tracking-params", c["url"] + "?share_source=wechat&from=timeline"),
        reprint_zh(c, "h08", "https://www.dev-china.example/article/2026/pilot-2", "开发者头条"),
        d, e, f, g, h, i, j,
        variant(j, "h16", "fragment-and-slash", "https://helix.example/blog/helix-editor-25-10#plugins"),
        k, k_zh, m, n,
        reprint_zh(n, "h21", "https://news.tech-hub.example/2026/10/30/foldable", "科技汇"),
        o, p,
        variant(p, "h24", "exact-url", p["url"]),
        q,
        variant(q, "h26", "tracking-params", q["url"] + "?spm=1001.2014&utm_campaign=robot"),
        r, s,
    ]


# ------------------------------------------------------------- batch2 split
def batch2():
    """A later, differently-composed batch used to check that an adopted
    version transfers, and to surface new failure classes."""
    a = item("b01", "m01", "original", "https://news.novalab.example/blog/orion-8-preview",
             "NovaLab previews Orion-8 with native video input",
             "NovaLab gave a first look at Orion-8, which accepts video directly and reasons over clips up to "
             "an hour long. A research preview opens to selected partners next month.", "en", critical=True)
    a_para = item("b02", "m01", "paraphrase", "https://ai-briefs.example/orion-8",
                  "Orion-8 will understand hour-long videos, NovaLab says",
                  "In a preview, NovaLab showed its upcoming Orion-8 model watching and answering questions "
                  "about videos as long as sixty minutes. Partners get early access in a month.", "en")
    b = item("b03", "m02", "original", "https://www.xinghe-lab.example/news/lumen-cloud",
             "星河实验室推出 Lumen 托管云服务",
             "星河实验室推出 Lumen 托管云服务，提供按量计费的向量检索，"
             "首批开放华东与华南两个区域，开源版本功能保持不变。", "zh", critical=True)
    c = item("b06", "m03", "original", "https://blog.rust-lang.example/2026/12/rust-2027-edition-call",
             "Call for testing: the Rust 2027 edition",
             "The Rust project invites users to test the 2027 edition on nightly. Planned changes include "
             "reserved syntax for generators and new lint defaults for unsafe operations in unsafe functions.",
             "en", critical=True)
    d = item("b08", "m04", "original", "https://security.example/advisories/CVE-2026-40001",
             "CVE-2026-40001: authentication bypass in GateKeeper SSO",
             "GateKeeper SSO versions before 5.2.2 accept unsigned SAML assertions under certain IdP "
             "configurations, allowing account takeover. Administrators should upgrade immediately.", "en",
             critical=True)
    e = item("b09", "m05", "same-topic-new-info", "https://security.example/advisories/CVE-2026-40001-update",
             "GateKeeper SSO patch for CVE-2026-40001 is incomplete",
             "Researchers found that GateKeeper SSO 5.2.2 still accepts unsigned assertions when metadata is "
             "loaded from a URL. A complete fix ships in 5.2.3.", "en", critical=True)
    f = item("b10", "m06", "original", "https://www.cityauto.example/policy/2026/robotaxi-airport",
             "滨江机场开通无人驾驶接驳线路",
             "滨江机场与两家自动驾驶企业合作开通无人驾驶接驳线路，连接航站楼与地铁站，"
             "首月免费运营，每日服务时间为早七点至晚十点。", "zh", critical=True)
    g = item("b12", "m07", "original", "https://papers.example/en/agent-self-repair",
             "Paper: agents that repair their own tools from failure logs",
             "The authors let a coding agent rewrite its tool wrappers after failed episodes, accepting changes "
             "only when a held-out task suite improves. Gains appeared in four of ten rounds.", "en",
             critical=True)
    h = item("b14", "m08", "original", "https://www.ai-weekly.example/2026/44/open-data",
             "国家数据局发布公共数据开放目录",
             "国家数据局发布首批公共数据开放目录，涵盖交通、气象和企业登记等十二个领域，"
             "开发者可通过统一平台申请接口调用。", "zh", critical=True)
    h_rew = item("b15", "m08", "paraphrase", "https://data-insight.example/p/9921",
                 "十二大领域公共数据将开放",
                 "首批公共数据开放目录近日公布，交通出行、天气以及工商登记等十二类数据纳入其中，"
                 "开发者能够在统一的平台上提交接口申请。", "zh")
    return [
        a, a_para,
        variant(a, "b17", "tracking-params", a["url"] + "?utm_source=linkedin&li_fat_id=8f2a"),
        b,
        variant(b, "b04", "scheme-and-www", "http://xinghe-lab.example/news/lumen-cloud/"),
        reprint_zh(b, "b05", "https://www.kaiyuan.example/news/2026/lumen-cloud", "开源资讯"),
        c,
        reprint_en(c, "b07", "https://devweekly.example/p/rust-2027-edition", "Dev Weekly"),
        d, e, f,
        variant(f, "b11", "tracking-params", f["url"] + "?from=groupmessage&isappinstalled=0"),
        g,
        variant(g, "b13", "exact-url", g["url"]),
        h, h_rew,
    ]


def write(name, rows):
    path = os.path.join(OUT, name + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return {"file": name + ".jsonl", "items": len(rows), "sha256": digest,
            "clusters": len({r["label"]["cluster"] for r in rows})}


def main():
    os.makedirs(OUT, exist_ok=True)
    manifest = {"name": "dedup", "version": "2026-10-25.1",
                "note": "Fictional mechanism-demo data; not a general benchmark.",
                "splits": {"dev": write("dev", dev()), "holdout": write("holdout", holdout()),
                           "batch2": write("batch2", batch2())}}
    with open(os.path.join(OUT, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    # Unlabeled copy of batch2 used as the "daily snapshot" for the collect demo.
    batch_dir = os.path.join(ROOT, "fixtures", "batches")
    os.makedirs(batch_dir, exist_ok=True)
    with open(os.path.join(batch_dir, "daily_2026-10-24.jsonl"), "w", encoding="utf-8") as f:
        for r in batch2():
            f.write(json.dumps({k: v for k, v in r.items() if k != "label"}, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
