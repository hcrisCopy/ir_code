# 参考仓库索引

论文官方仓库的浅克隆，只用来核对 split、候选构造和字段含义，**不参与统计**。
重新克隆或更新：`python scripts/clone_reference.py --all`（加 `--refresh` 会 git pull）。

| 仓库 | 用途 | 核对哪些配置 | commit |
| --- | --- | --- | --- |
| [fullrank](https://github.com/RUC-NLPIR/fullrank) | 训练数据只有 qid 和 doc id 列表，构造说明和脚本在这里 | fullrank_training_data / fullrank-train-1k | `5e74c79` |
| [bergen](https://github.com/naver/bergen) | RRK 的训练与评测配置，以及 PISCO compressor 的实现 | msmarco_passage_v1 / rrk 相关配置；beir / beir-12 | `eb8ec04` |
| [ResRank](https://github.com/Quark-Medical/ResRank) | 两阶段训练数据未发布，只能从项目页确认口径 | unpublished / ResRank 训练集 | `d661809` |
| [MIRAGE](https://github.com/gzxiong/MIRAGE) | 医学 RAG benchmark 与 MedRAG 检索代码，Top-10K 候选包缺失时的替代路径 | mirage / mirage-test | `392943af` |
| [tripclick](https://github.com/tripdatabase/tripclick) | 从 click log 构造 benchmark 的代码，用于确认 HEAD/TORSO/TAIL 与 RAW/DCTR 的定义 | tripclick / tripclick-raw-train；tripclick / tripclick-raw-test | `297717c` |
| [beir](https://github.com/beir-cellar/beir) | Robust04 与 TREC-News 的 BEIR 处理方式，说明子集如何从原始语料生成 | beir / beir-7；beir / beir-8；beir / beir-12 | `ef83d29` |
| [echo-embeddings](https://github.com/jakespringer/echo-embeddings) | Echo E5 训练数据的组成与语言分布 | echo_e5_training_data / e2rank-stage1-1509697 | `c752c46` |
| [KILT](https://github.com/facebookresearch/KILT) | KILT 语料的下载与切分脚本，确认 passage 的存储单位 | kilt_wikipedia_passages / c2r-compressor-pretrain | `2664322` |
| [atlas](https://github.com/facebookresearch/atlas) | Atlas 语料下载脚本；实际下载已经用到这个仓库 | atlas_wikipedia_2020_12 / perank-alignment-atlas-2m | `0ec8889` |
| [pmc-patients](https://github.com/pmc-patients/pmc-patients) | ReCDS 两类任务的字段和评测方式说明 | pmc_patients_recds / recds-test | `baf069a` |

记录 commit 是为了以后能回到同一份代码核对，避免仓库更新后结论对不上。
