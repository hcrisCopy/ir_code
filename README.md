# IR 数据集第二阶段：准备、统计与核验

本目录承接完整的第二阶段工作。数据下载只是准备环节；后续还要按“论文 × 数据集 × split”确认实际使用方式，统一解析数据，并统计 query、候选池、正样本和样本长度。

下面的命令都假定当前工作目录是 `ir_code/`，所有项目文件使用相对路径。

## 第二阶段总流程

```text
A 数据准备    下载 / 申请 / 解压            check_data.py + extract_data.py
B 使用配置    configs/manifest.json        人工维护，脚本只读
C 查看样本    inspect_data.py              打印一条真实数据，确认样本单位
D 指标统计    count_basic.py（第 1-4 项） + count_lengths.py（第 5-7 项）
E 汇总核验    merge_results.py             ../ir_data/outputs/final_stats.json
F 结果交付    回填 record.xlsx
```

当前阶段：数据上传与逐数据集统计并行进行。文件齐全、配置范围正确且结果状态 complete，才可回填。

## 目录约定

```text
项目根目录/
├── ir_code/                   # 代码，独立 git 仓库，所有命令在这里执行
│   ├── README.md              # 本文件
│   ├── requirements.txt
│   ├── download_datasets.py   # A2.3-A2.14 下载
│   ├── configs/manifest.json  # 配置中心，人工维护
│   ├── scripts/               # 统计代码
│   └── reference/             # 论文官方仓库（只作证据，不参与统计）
├── Qwen/Qwen3-1.7B/           # tokenizer
└── ir_data/                   # 数据与统计产物，整包上传服务器就用这一份
    ├── DATASET_INDEX.md       # record.xlsx 名称与实际目录的对照表
    ├── datasets/              # 真正的数据；每套底层数据只保存一份
    ├── outputs/               # 统计结果，全部产物都在这里
    ├── application/           # 申请表、填写说明和邮件模板
    ├── _cache/                # Hugging Face、ir_datasets 等工具缓存
    └── _tools/                # 仅放下载或预处理工具，不放数据
```

代码和数据分开：`ir_code/` 可以单独打包传服务器，`ir_data/` 整包上传，两边都不用改路径。

所有命令都在 `ir_code/` 目录下执行，路径一律相对项目根目录。

数据目录统一使用 `../ir_data/datasets/<规范名称>/`。规范名称和表格行的对应关系见 [`../ir_data/DATASET_INDEX.md`](../ir_data/DATASET_INDEX.md)。

如果同一个官方数据包同时包含 train、dev、test（例如 NFCorpus、TripClick），只保存一套数据目录，但其中各 split 的文件不能混合，统计结果也必须分开。不同数据版本或不同语料仍建立独立目录，例如 MS MARCO Passage v1、Passage v2 和 Document v1 分开保存。TREC DL、PE-Rank、FullRank 等可以引用同一份 MS MARCO Passage v1 正文，不再复制正文文件。

## A. 数据准备

### A1. 下载前准备

以下命令按 Linux/bash 编写。

#### A1.1 创建 Conda 环境

环境统一命名为 `ir_stats`，使用 Python 3.12。这里采用 3.12 是因为当前固定的 Pyserini 2.4.0 官方以 Python 3.12 和 Java 21 为基准环境：

```bash
conda create -n ir_stats python=3.12 -y
conda activate ir_stats

python -m pip install -U pip
python -m pip install -r requirements.txt

conda install -c conda-forge -y git git-lfs wget jq openjdk=21
git lfs install
```

Windows 上 `unzip` 和 `aria2` 在 conda-forge 的 win-64 平台没有包，直接不装：`unzip` 用不到（下载脚本自带解压），`aria2` 按 A2.0 单独装。

这里不配置 GPU 版 PyTorch。Pyserini 的依赖可能会带入 CPU 版 PyTorch，但第二阶段的文本统计不加载模型权重；如果后面要运行模型，再按服务器 CUDA 版本单独配置 GPU 版 PyTorch。

每次执行下载或统计代码前先运行：

```bash
conda activate ir_stats
```

#### A1.2 创建数据目录并设置缓存

```bash
mkdir -p ../ir_data/{_cache,datasets,application,_tools}

export HF_HOME=../ir_data/_cache/huggingface
export HF_DATASETS_CACHE=../ir_data/_cache/huggingface/datasets
export IR_DATASETS_HOME=../ir_data/_cache/ir_datasets
```

`tar` 通常由 Linux 系统自带。Hugging Face 数据均为公开仓库，一般不用登录；出现限流时再执行 `hf auth login`。

#### A1.3 ModelScope 配置与 Qwen3-1.7B 下载

```text
python -m pip install modelscope
modelscope download --model Qwen/Qwen3-1.7B --local_dir ../Qwen/Qwen3-1.7B
```

### A2. 可直接下载的数据

下载统一用 `download_datasets.py`：默认**多连接分片 + 断点续传 + 下载后自动校验**，不需要装任何外部下载器。

```bash
python download_datasets.py --list                      # 看有哪些任务
python download_datasets.py A2.3                        # 下单个
python download_datasets.py --all --yes --no-extract    # 全下，先不解压
```

`--no-extract` 建议留着，解压交给 ② `extract_data.py` 统一做（它会校验通过才删压缩包）。

#### 下载速度怎么调

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--connections N` | 16 | 每个文件切成 N 个分片并行下。慢的时候先加这个，最大 64 |
| `--files-in-parallel N` | 4 | 同一个任务里同时下几个文件（例如 BEIR 的 12 个 zip），最大 16 |
| `--use-aria2` | 关 | 改用 aria2c。只有在装了 aria2、且这个地址能连通时才更快 |

```bash
# 单文件慢，就加连接数
python download_datasets.py A2.5 --connections 32

# 一堆小文件慢，就加并发文件数
python download_datasets.py A2.10 --files-in-parallel 8
```

中断后**重跑同一条命令即可续传**，连分片级别都不用重下。只有文件明确损坏时才加 `--repair`（它会删掉该文件的所有分片和 `.part`）。

#### A2.0 aria2 是可选项

aria2 现在不是必需的。Windows 版 aria2 连 MS MARCO 的 Azure 地址会 TLS 握手失败，而内置的多连接下载器没有这个问题，所以默认走内置。确实要用的话（例如某些 HF 镜像）：

```powershell
$ToolDir = "../ir_data/_tools/aria2"
New-Item -ItemType Directory -Force -Path $ToolDir | Out-Null
curl.exe -L --fail -o "$ToolDir/aria2.zip" "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
Expand-Archive -LiteralPath "$ToolDir/aria2.zip" -DestinationPath $ToolDir -Force
$Exe = Get-ChildItem -Path $ToolDir -Filter "aria2c.exe" -Recurse | Select-Object -First 1
Copy-Item -LiteralPath $Exe.FullName -Destination "$env:CONDA_PREFIX/Scripts/aria2c.exe" -Force
aria2c --version
```

装好后加 `--use-aria2` 才会用；aria2 失败仍会自动回退到内置下载器。

#### A2.1 Hugging Face 数据包

```bash
# PE-Rank 训练集
hf download liuqi6777/pe_rank_data --type dataset \
  --local-dir ../ir_data/datasets/pe_rank_training_data

# RankZephyr 训练集
hf download castorini/rank_zephyr_training_data --type dataset \
  --local-dir ../ir_data/datasets/rank_zephyr_training_data

# E2Rank 训练集
hf download Alibaba-NLP/E2Rank_ranking_datasets --type dataset \
  --local-dir ../ir_data/datasets/e2rank_ranking_datasets

# FullRank 训练集，公开数据名不是“FullRank training data”，而是下面这个仓库
hf download liuwenhan/msmarco_full_ranking_list --type dataset \
  --local-dir ../ir_data/datasets/fullrank_training_data

# REARANK 12K
hf download le723z/rearank_12k --type dataset \
  --local-dir ../ir_data/datasets/rearank_12k

# ReasonRank 13K，包含正文映射，体积较大
hf download liuwenhan/reasonrank_data_13k --type dataset \
  --local-dir ../ir_data/datasets/reasonrank_data_13k

# MIRIAD-4.4M；只有 train split
hf download miriad/miriad-4.4M --type dataset \
  --local-dir ../ir_data/datasets/miriad_4_4m

# BRIGHT
hf download xlangai/BRIGHT --type dataset \
  --local-dir ../ir_data/datasets/bright

# ReaRank 的辅助数学推理评测：AIME 2024（30题）
hf download math-ai/aime24 --type dataset \
  --local-dir ../ir_data/datasets/auxiliary_math/aime24

# ReaRank 论文写作 AMC；根据其明确引用的 LIMO 设置，对应 AMC 2023（40题）
hf download math-ai/amc23 --type dataset \
  --local-dir ../ir_data/datasets/auxiliary_math/amc23
```

说明：AIME24 和 AMC23 只用于 ReaRank 的 reasoning-transfer 辅助实验，不属于 IR/reranking 主测试集。ReaRank 正文把第二项简称为 `AMC`，但该实验明确沿用 LIMO；LIMO 的评测设置明确为 `AMC23`。

#### A2.2 R2MED

`R2MED/R2MED` 是入口页，不是一个可以完整下载的单仓库。8 个子集需要分别下载：

```bash
for name in \
  Biology \
  Bioinformatics \
  Medical-Sciences \
  MedXpertQA-Exam \
  MedQA-Diag \
  PMC-Treatment \
  PMC-Clinical \
  IIYi-Clinical
do
  hf download "R2MED/${name}" --type dataset \
    --local-dir "../ir_data/datasets/r2med/${name}"
done
```

#### A2.3 MS MARCO Passage v1

下面四个文件只提供基础语料、全部 query 和 train/dev qrels，可用于统计 query、正样本和完整语料的长度；它们不包含各论文实际使用的候选排序，因此不能单独用于统计“多少进多少出”、候选池并集或论文实际输入样本的长度。

微软官方的 `top1000.train.tar.gz` 确实约为 175GB，包含约 4.78 亿行，格式为 `qid、pid、query、passage`，对应官方提供的 Top-1000 初始排序。当前仍暂不下载：它体积大、重复存放 query 和 passage 文本，而且不等于所有论文采用的 Top-20/50/100 候选。后续先逐篇确认候选来源；优先使用论文公开的候选文件或较小的 ID-only run。只有确认某篇论文使用这套官方 Top-1000、且没有更小的等价候选文件时，再补下载。

```bash
mkdir -p ../ir_data/datasets/msmarco_passage_v1

wget -c https://msmarco.z22.web.core.windows.net/msmarcoranking/collection.tar.gz \
  -P ../ir_data/datasets/msmarco_passage_v1
wget -c https://msmarco.z22.web.core.windows.net/msmarcoranking/queries.tar.gz \
  -P ../ir_data/datasets/msmarco_passage_v1
wget -c https://msmarco.z22.web.core.windows.net/msmarcoranking/qrels.train.tsv \
  -P ../ir_data/datasets/msmarco_passage_v1
wget -c https://msmarco.z22.web.core.windows.net/msmarcoranking/qrels.dev.tsv \
  -P ../ir_data/datasets/msmarco_passage_v1

tar -xzf ../ir_data/datasets/msmarco_passage_v1/collection.tar.gz \
  -C ../ir_data/datasets/msmarco_passage_v1
tar -xzf ../ir_data/datasets/msmarco_passage_v1/queries.tar.gz \
  -C ../ir_data/datasets/msmarco_passage_v1
```

官方说明：[MS MARCO ranking datasets](https://microsoft.github.io/msmarco/Datasets.html)。

#### A2.4 MS MARCO Document v1

该语料供 TREC DL 2019/2020 Document Ranking 使用。下载文件约 8.45GB（7.87GiB），解压后约 22GB。

```bash
mkdir -p ../ir_data/datasets/msmarco_document_v1
wget -c https://msmarco.z22.web.core.windows.net/msmarcoranking/msmarco-docs.tsv.gz \
  -P ../ir_data/datasets/msmarco_document_v1
```

#### A2.5 MS MARCO Passage v2

REARANK 使用 v2。下载语料、训练 query、qrels 和官方 Top-100 候选即可。

```bash
mkdir -p ../ir_data/datasets/msmarco_passage_v2

MSMARCO_BASE_URL=https://msmarco.z22.web.core.windows.net/msmarcoranking
for file in \
  msmarco_v2_passage.tar \
  passv2_train_queries.tsv \
  passv2_train_qrels.tsv \
  passv2_train_top100.txt.gz
do
  wget -c --header "X-Ms-Version: 2019-12-12" \
    "${MSMARCO_BASE_URL}/${file}" -P ../ir_data/datasets/msmarco_passage_v2
done
```

`msmarco_v2_passage.tar` 约 20.3GB，内部是 70 个 gzip JSONL 文件。官方入口：[TREC Deep Learning / MS MARCO v2](https://github.com/microsoft/msmarco/blob/master/TREC-Deep-Learning.md)。

#### A2.6 TREC DL 2019/2020 的 query 和 qrels

语料已经由 MS MARCO v1 提供。用 `ir_datasets` 导出小型评测文件：

```bash
mkdir -p ../ir_data/datasets/trec_dl

ir_datasets export msmarco-passage/trec-dl-2019/judged queries \
  > ../ir_data/datasets/trec_dl/trec_dl_2019_passage_queries.tsv
ir_datasets export msmarco-passage/trec-dl-2019/judged qrels \
  > ../ir_data/datasets/trec_dl/trec_dl_2019_passage_qrels.tsv

ir_datasets export msmarco-passage/trec-dl-2020/judged queries \
  > ../ir_data/datasets/trec_dl/trec_dl_2020_passage_queries.tsv
ir_datasets export msmarco-passage/trec-dl-2020/judged qrels \
  > ../ir_data/datasets/trec_dl/trec_dl_2020_passage_qrels.tsv

ir_datasets export msmarco-document/trec-dl-2019/judged queries \
  > ../ir_data/datasets/trec_dl/trec_dl_2019_document_queries.tsv
ir_datasets export msmarco-document/trec-dl-2019/judged qrels \
  > ../ir_data/datasets/trec_dl/trec_dl_2019_document_qrels.tsv

ir_datasets export msmarco-document/trec-dl-2020/judged queries \
  > ../ir_data/datasets/trec_dl/trec_dl_2020_document_queries.tsv
ir_datasets export msmarco-document/trec-dl-2020/judged qrels \
  > ../ir_data/datasets/trec_dl/trec_dl_2020_document_qrels.tsv
```

这里没有统一下载“论文候选集”，因为各论文分别使用 BM25、Jina、SPLADE-v3 或自有检索器。论文实际 run 文件将在第二阶段按论文补充；没有 run 文件时，候选池只能标星号。

#### A2.7 KILT Wikipedia Passages

```bash
mkdir -p ../ir_data/datasets/kilt_wikipedia_passages
wget -c http://dl.fbaipublicfiles.com/KILT/kilt_knowledgesource.json \
  -O ../ir_data/datasets/kilt_wikipedia_passages/kilt_knowledgesource.json
```

该文件约 34.76GiB。论文抽取的 10M passages 没有单独发布，后续只能根据论文规则重建或标注为“论文子集未公开”。官方说明：[KILT repository](https://github.com/facebookresearch/KILT)。

#### A2.8 Echo E5

```bash
mkdir -p ../ir_data/datasets/echo_e5_training_data
gdown --fuzzy \
  'https://drive.google.com/file/d/1YqgaJIzmBIH37XBxpRPCVzV_CLh6aOI4/view' \
  -O ../ir_data/datasets/echo_e5_training_data/echo-data.tar
tar -xf ../ir_data/datasets/echo_e5_training_data/echo-data.tar \
  -C ../ir_data/datasets/echo_e5_training_data
```

官方发布页：[Echo Embeddings](https://github.com/jakespringer/echo-embeddings)。该数据包含中英文以外的语言；token 可以统一统计，word 统计需按既定语言规则处理。

#### A2.9 Atlas Wikipedia 2020-12

Atlas 官方通过脚本下载并解压语料：

```bash
mkdir -p ../ir_data/_tools ../ir_data/datasets/atlas_wikipedia_2020_12
git clone --depth 1 https://github.com/facebookresearch/atlas.git \
  ../ir_data/_tools/atlas

python ../ir_data/_tools/atlas/preprocessing/download_corpus.py \
  --corpus corpora/wiki/enwiki-dec2020 \
  --output_directory ../ir_data/datasets/atlas_wikipedia_2020_12
```

论文使用的是从约 31.5M 文本中抽取的 2M 子集；公开的是完整 `enwiki-dec2020`，2M 子集本身没有单独下载文件。

#### A2.10 BEIR 公共子集、NFCorpus 和 TREC-COVID

下面一次下载覆盖表格中的 BEIR-7/8/12公共部分，同时覆盖单独列出的 NFCorpus 和 TREC-COVID。不要再重复下载 NFCorpus。

```bash
mkdir -p ../ir_data/datasets/beir/public

for name in \
  trec-covid \
  nfcorpus \
  webis-touche2020 \
  dbpedia-entity \
  scifact \
  nq \
  hotpotqa \
  fiqa \
  quora \
  scidocs \
  fever \
  climate-fever
do
  wget -c \
    "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/${name}.zip" \
    -P ../ir_data/datasets/beir/public
done

for file in ../ir_data/datasets/beir/public/*.zip; do
  unzip -n "$file" -d ../ir_data/datasets/beir/public
done
```

BEIR 中的 `Signal-1M`、`Robust04` 和 `TREC-News` 都没有当前可用的普通 BEIR ZIP。`signal1m.zip` 的旧地址现已返回 404，不能放在上面的公共 ZIP 循环中。这三项可先下载包含 `raw` 正文的 Castorini/Pyserini 预构建索引，见下一节。Robust04 和 TREC-News 的原始语料许可仍按“需要申请的数据”办理。

#### A2.11 BEIR / Signal-1M、Robust04 与 TREC-News 的公开索引

Castorini/Pyserini 公开了这三个数据集的 BEIR `flat` Lucene 索引。索引构建时使用了 `-storeRaw`，每篇文档的原始 BEIR JSON（文档 ID、标题和正文）保存在索引中，因此后续可以导出正文并统计 token、word 和长度分位数。

这三份文件不是数据集发布方提供的原始压缩包，但更接近论文实际使用的 BEIR 处理版本：

| 数据集 | 压缩包大小 | 索引文档数 | MD5 |
| --- | ---: | ---: | --- |
| Signal-1M | 473.6 MiB | 约 2.86M | 未提供，下载后记录 SHA-256 |
| Robust04 | 1.61 GiB | 528,036 | `d508fc770002a99a5dc3da3d0fa001b7` |
| TREC-News | 2.44 GiB | 594,589 | `22e7752c3d0122c28013b33e5e2134ae` |

下载：

```bash
mkdir -p ../ir_data/datasets/beir/prebuilt_indexes/archives
mkdir -p ../ir_data/datasets/beir/prebuilt_indexes/indexes

wget -c \
  https://huggingface.co/datasets/castorini/prebuilt-indexes-beir/resolve/main/lucene-inverted/flat/lucene-inverted.beir-v1.0.0-signal1m.flat.20221116.505594.tar.gz \
  -O ../ir_data/datasets/beir/prebuilt_indexes/archives/signal1m_beir_flat.tar.gz

wget -c \
  https://huggingface.co/datasets/castorini/prebuilt-indexes-beir/resolve/main/lucene-inverted/flat/lucene-inverted.beir-v1.0.0-robust04.flat.20221116.505594.tar.gz \
  -O ../ir_data/datasets/beir/prebuilt_indexes/archives/robust04_beir_flat.tar.gz

wget -c \
  https://huggingface.co/datasets/castorini/prebuilt-indexes-beir/resolve/main/lucene-inverted/flat/lucene-inverted.beir-v1.0.0-trec-news.flat.20221116.505594.tar.gz \
  -O ../ir_data/datasets/beir/prebuilt_indexes/archives/trec-news_beir_flat.tar.gz
```

校验：

```bash
cd ../ir_data/datasets/beir/prebuilt_indexes/archives

sha256sum signal1m_beir_flat.tar.gz > signal1m_beir_flat.tar.gz.sha256
echo "d508fc770002a99a5dc3da3d0fa001b7  robust04_beir_flat.tar.gz" | md5sum -c -
echo "22e7752c3d0122c28013b33e5e2134ae  trec-news_beir_flat.tar.gz" | md5sum -c -

cd ../../../../../ir_code
```

解压：

```bash
tar -xzf ../ir_data/datasets/beir/prebuilt_indexes/archives/signal1m_beir_flat.tar.gz \
  -C ../ir_data/datasets/beir/prebuilt_indexes/indexes

tar -xzf ../ir_data/datasets/beir/prebuilt_indexes/archives/robust04_beir_flat.tar.gz \
  -C ../ir_data/datasets/beir/prebuilt_indexes/indexes

tar -xzf ../ir_data/datasets/beir/prebuilt_indexes/archives/trec-news_beir_flat.tar.gz \
  -C ../ir_data/datasets/beir/prebuilt_indexes/indexes

find ../ir_data/datasets/beir/prebuilt_indexes/indexes -maxdepth 2 -type d | sort
```

下载与这些索引版本配套的 test queries 和 qrels。这里固定 Pyserini 2.4.0 所使用的 `castorini/eval` commit，避免资源随上游更新：

```bash
mkdir -p ../ir_data/datasets/beir/prebuilt_indexes/metadata

EVAL_COMMIT=0b4acbd929edd11edfd16250457fb70ff69e9b4f
EVAL_BASE=https://raw.githubusercontent.com/castorini/eval/${EVAL_COMMIT}

for name in signal1m robust04 trec-news; do
  wget -c \
    "${EVAL_BASE}/topics/topics.beir-v1.0.0-${name}.test.tsv.gz" \
    -P ../ir_data/datasets/beir/prebuilt_indexes/metadata
  wget -c \
    "${EVAL_BASE}/qrels/qrels.beir-v1.0.0-${name}.test.txt" \
    -P ../ir_data/datasets/beir/prebuilt_indexes/metadata
done

gunzip -kf ../ir_data/datasets/beir/prebuilt_indexes/metadata/topics.*.tsv.gz
```

后续读取时必须使用 `flat` 索引；dense、BGE 或 SPLADE 索引通常不保存正文。Pyserini 可以按 Lucene 内部文档编号遍历，并通过 `doc.raw()` 取得原始 JSON。正文导出脚本将在第二阶段统计代码中统一编写，不要现在把索引当普通文本文件读取。

上述索引主要补齐 corpus 正文。queries 和 qrels 不在 Lucene 索引内，后续通过 Pyserini 对应的 BEIR topic/qrels 资源导出，不能把索引文档当作 qrels。公开可下载不代表原始版权许可被取消，正式研究记录仍应保留 NIST 申请和授权信息。

来源：[Pyserini BEIR 索引构建记录](https://github.com/castorini/pyserini/blob/master/pyserini/resources/index-metadata/lucene-inverted.beir-v1.0.0-flat.20221116.505594.README.md)、[Pyserini 文档读取说明](https://github.com/castorini/pyserini/blob/master/docs/usage-fetch.md)。

#### A2.12 MTEB English v1/v2

MTEB 是任务集合，不是单一压缩包。必须按 E2Rank 论文使用的版本化 benchmark 下载，不能把 2026 年最新任务列表当成论文版本。

```bash
mkdir -p ../ir_data/datasets/mteb_english

export HF_DATASETS_CACHE=../ir_data/datasets/mteb_english

python - <<'PY'
import mteb

expected = {"MTEB(eng, v1)": 56, "MTEB(eng, v2)": 41}
for benchmark_name, expected_count in expected.items():
    benchmark = mteb.get_benchmark(benchmark_name)
    assert len(benchmark.tasks) == expected_count, (
        benchmark_name, len(benchmark.tasks), expected_count
    )
    print(benchmark_name, len(benchmark.tasks))
    for task in benchmark.tasks:
        task.load_data()
PY
```

项目把 `mteb` 固定为 `2.20.10`，并在下载前校验 v1 为 56 个 task、v2 为 41 个 task，避免以后 registry 更新后静默下载不同范围。MTEB 的 task 数据会进入 `../ir_data/datasets/mteb_english/`。执行完本节后，如需继续运行其他下载命令，可重新执行 A1.2 节的缓存变量设置。下一阶段按 task 分开统计，不把 MTEB 汇总成一个虚假的统一 split。

#### A2.13 MIRAGE

MIRAGE 的 `benchmark.json` 已包含在官方仓库中：

```bash
git clone --depth 1 https://github.com/gzxiong/MIRAGE.git \
  ../ir_data/datasets/mirage
```

论文曾提供各检索器的 Top-10K snippet ID 压缩包，但官方 Box 链接目前返回 404，不能再作为下载命令执行。当前只下载 `benchmark.json`。候选包记为“未取得”；后续若必须统计论文原始候选池，再使用 MedRAG 代码和相应语料、检索器重建，并把重建结果标为复现数据，不能冒充官方候选包。

官方入口：[MIRAGE](https://github.com/gzxiong/MIRAGE)。

#### A2.14 PMC-Patients / ReCDS

Figshare collection 中有三个独立文件：

```bash
mkdir -p ../ir_data/datasets/pmc_patients_recds

wget -c https://ndownloader.figshare.com/files/43050394 \
  -O ../ir_data/datasets/pmc_patients_recds/PMC-Patients.json.tar.gz
wget -c https://ndownloader.figshare.com/files/43054744 \
  -O ../ir_data/datasets/pmc_patients_recds/ReCDS_benchmark.tar.gz
wget -c https://ndownloader.figshare.com/files/43055212 \
  -O ../ir_data/datasets/pmc_patients_recds/Meta_data.tar.gz
```

第二阶段至少需要 `PMC-Patients.json.tar.gz` 和 `ReCDS_benchmark.tar.gz`。官方 collection：[PMC-Patients](https://figshare.com/collections/PMC-Patients/6723465)。

### A3. 需要申请的数据

#### A3.1 TripClick

申请难度：中等。需要填写用途并由数据所有者人工发放，限非商业研究，不能再次公开原始数据。

1. 打开并填写 [TripClick request form](https://docs.google.com/document/d/1RHVxVnZsPBDDZMDcSvbB8VyNZDl2cn6KpeeSvIu6g_c/edit?usp=sharing)。
2. 在表中勾选：
   - `TripClick IR Benchmark`：必须，包含 docs、queries、qrels；
   - `Training Package for Deep Learning Models`：训练统计需要；
   - `dlfiles_runs_test`：测试候选统计需要；
   - `Logs Dataset`：本任务通常不需要。
3. 写清用途：学术研究；统计 query、候选池、正样本数及文本长度；不会重新分发数据。
4. 将表格发给 `jon.brassey@tripdatabase.com`。
5. 获批后把文件放到：

```bash
mkdir -p ../ir_data/datasets/tripclick
```

```text
../ir_data/datasets/tripclick/benchmark.tar.gz
../ir_data/datasets/tripclick/dlfiles.tar.gz
../ir_data/datasets/tripclick/dlfiles_runs_test.tar.gz
```

如果后续使用 `ir_datasets`，将这些文件复制或软链接到：

```text
../ir_data/_cache/ir_datasets/tripclick/
```

官方流程：[TripClick access page](https://tripdatabase.github.io/tripclick/)。

#### A3.2 BEIR / Robust04

申请难度：中等。需要单位负责人签署组织协议；NIST 说明通常在 7 个工作日内回复。

若只为推进第二阶段统计，可先使用 A2.11 的公开 BEIR Lucene 索引；该索引含正文，但不替代本节的官方数据许可。

1. 打开 [TREC Disks 4 and 5](https://trec.nist.gov/data/cd45/)。
2. 填写并签署组织协议；个人协议由实际使用者签署并由单位留存。
3. 将组织协议扫描件发到 `trec-data-admin@nist.gov`。
4. 邮件主题写：`request for TREC disks 4 and 5`。
5. 获批后下载 Disks 4&5。Robust04 使用时要排除 Congressional Record。

#### A3.3 BEIR / TREC-News

申请难度：中等。流程与 Robust04 类似，同样需要单位签字；NIST 说明通常在 7 个工作日内回复。

若只为推进第二阶段统计，可先使用 A2.11 的公开 BEIR Lucene 索引；该索引含正文，但不是官方 `WashingtonPost.v2.tar.gz`，也不替代本节的数据许可。

1. 打开 [TREC Washington Post Corpus](https://trec.nist.gov/data/wapost/)。
2. 填写 [Organization Application](https://trec.nist.gov/data/wapost/Organization%20Application.pdf)。
3. 每位使用者填写 [Individual Application](https://trec.nist.gov/data/wapost/Individual%20Application.pdf)，由单位留存。
4. 将组织协议扫描件发到 `wapo-request@nist.gov`。
5. 邮件主题写：`request for Washington Post corpus`。

### A4. 无法通过申请补齐的数据

以下不是“需要申请”，而是论文没有公开成品数据或完整构造代码：

- LongRanker 10K 训练集；
- ResRank 重标注训练集；
- RRK 训练集。

后续统计只能使用论文给出的汇总数字并标 `*`，或在作者补充数据后更新，不能用相似数据替代。

### A5. 下载后检查

```bash
cd ir_code
python scripts/check_data.py --all          # 对照 manifest 的期望文件逐项核对
du -sh ../ir_data/datasets/* | sort -h      # 只看体积
```

`check_data.py` 会比对每个数据集的期望文件、大小和 MD5（期望值写在 `configs/manifest.json` 的 `files` 里），把明细写入 `../ir_data/outputs/inventory.csv`，并在结尾分出「齐全」和「还缺」两栏。`../ir_data/DATASET_INDEX.md` 的状态列以它的输出为准。

## B. 总配置 JSON

`configs/manifest.json` 人工维护，脚本只读。每个数据集保存一份底层数据，其下的 `configs` 分别记录论文、split、子集、候选来源、N→K、正例字段和回填行。同一数据集的不同用法不能因候选数量相同而合并。

- `usage_scope=unknown_subset`：缺论文实际子集，只保留声明，不拿全集替代。
- `usage_scope=public_release`：公开文件参考结果，和论文抽样/训练阶段结果分开。
- `pool_scope=candidate_union/full_corpus/author_pool/na`：明确池范围；全语料替代候选并集时标星，即使数值是实测。
- `record_type/record_name`：明确对应工作簿哪一行，防止 train/test 和不同年份混写。
- 正例字段可为独立 qrels、`relevant_docids` 或指定的 `pos_index`，不能把排序第一名或 BM25 分数当相关性标注。

每项保留 measured / declared / estimate / unknown / na。未公开、正在上传、缺文件和不适用分别记录，不能都写零。

## C. 服务器执行

在 `ir_code/` 下执行；`../ir_data/` 和 `../Qwen/` 与代码目录同级。只加载 tokenizer，不加载模型权重，不需要 GPU。

```bash
conda activate ir_stats
python -m pip install -r requirements-stats.txt

# 第一次先核对文件；缺文件会返回非零，明细保存在 inventory.csv
python scripts/check_data.py --all

# 逐数据集解压，完成清单核对通过才删除原包
python scripts/extract_data.py --dataset rearank_12k --dry-run
python scripts/extract_data.py --dataset rearank_12k
# 加 --keep-archive 保留原包；无需解压的 parquet/jsonl 或 keep 包会跳过

# 看一条原始记录和解析后的候选
python scripts/inspect_data.py --dataset rearank_12k --config rearank-12k

# 分别计算第 1–4 项、第 5–7 项，再汇总
python scripts/count_basic.py --dataset rearank_12k
python scripts/count_lengths.py --dataset rearank_12k
python scripts/merge_results.py

# 一键续跑：发现数据 → 盘点进度 → 跳过已完成 → 继续未完成 → 汇总
python scripts/run_available.py --plan      # ①先只看计划：哪些已完成、哪些要跑、哪些被阻塞
python scripts/run_available.py             # ②正式跑：自动跳过已完成的配置
# 计划表里会列出每条待跑配置的动作：数量+长度 / 长度 / 登记（只记录论文声明）/ 阻塞（缺文件）

# 常用开关
python scripts/run_available.py --dataset r2med beir    # 只处理这些数据集
python scripts/run_available.py --config r2med-test     # 只处理这些 config
python scripts/run_available.py --lengths-only          # 只补长度（数量已完成的）
python scripts/run_available.py --force                 # 忽略已有结果全部重跑
python scripts/run_available.py --limit 200             # 冒烟：每条只跑 200 个样本，写 debug/
python scripts/run_available.py --no-token              # 只算 word，不加载 tokenizer
python scripts/run_available.py --extract               # 先解压（默认不解压，见下）

# 中断后重新执行同一条命令即可续上：已完成且输入指纹未变的配置会被跳过。
# 每条的结果、状态与耗时都会写进 ../ir_data/outputs/run_status.json。

# 指纹按文件内容（总大小 + 首尾各 64 KiB 的 SHA256）计算，刻意不含 mtime：
# 服务器算完的 experiments/*.json 拿回本地汇总时不会因时间戳不同被判过期。
# 代价与兜底见 scripts/stage2.py 里 content_signature 的注释。

# ⚠ 默认不解压：extract_data.py 核对通过后会删掉原包，而有的包解开是几十 GB 量级
#   （如 tripclick 的 dlfiles.tar.gz 28.7 GiB）。计划表会提示哪些数据集可能需要解压。
# ⚠ 若 --plan 显示大量"阻塞"，先补数据/等上传完成再跑；空跑的配置不会产生正式结果。
```

数量和长度入口都支持 `--dataset`、`--config`、`--all`、`--list`。长度入口支持 `--tokenizer ../Qwen/Qwen3-1.7B` 和 `--limit 100`；limit 结果只写 debug 目录。`--no-token` 仅算 word。解压入口支持 `--dataset/--all/--dry-run/--keep-archive`；汇总入口用 `--preview` 控制预览。

```bash
# 本地推送后，服务器只同步代码，不改数据目录
source /etc/network_turbo
git pull --ff-only
```

参考仓库保存在 `reference/`，索引见 `reference/REPOS.md`。需要时运行 `python scripts/clone_reference.py --all`；克隆代码不提交 GitHub。

## D. 统计口径

1. Query 按当前设置的实际 query 集去重。论文报告数、公开文件实测数、增强记录数另存；文本去重/记录代理会注明。没有子集名单，不用论文报告规模作全集统计的分母。
2. N→M 分别记录输入候选数与要求输出数，有输出依据才写 N→N；只知道 top-N 时写“输入 N 个，输出数量待核实”。`n_to_k.output_evidence` 记录依据，`final_keep` 是排序后最终保留数，`single_call` 是单次模型调用；评价 @10、生成 token 数、窗口大小都不能当作 M。多篇论文的不同形式见 `per_paper`。
3. 候选池是所有 query 候选 ID 的并集。缺原始 ID 时使用正文 SHA256 去重计算长度，正文并集规模作为替代值标星。缺实际 run 时，全 corpus 结果标明替代范围。
4. 正例按唯一 query–doc 关联计数，阈值按配置执行；分母为同一实际 query 集。指定正例、候选内正例与完整标注池正例分别注明。无标注写未知，不写零。
5. Token 固定 Qwen3-1.7B，`add_special_tokens=False`、不截断，统计压缩前候选原文；不含 query、指令、编号和输出。标题/正文按配置组合，结果保留 tokenizer 指纹。
6. Word 仅计中英文：中文每汉字计 1；英文 A–Z 单词保留内部连字符/撇号；数字串计 1；标点不计。优先使用来源语言标签；其他/未知语言排除并报告样本数，token 与 word 各自记录有效分母。
7. P25/P50/P75/P90 用最近秩法：第 `ceil(p × N)` 个排序长度，不插值。每个子集独立一行；整体分位数不能用某个子集的值或子集分位数平均替代。

主分布按候选 ID 或明确标记的正文哈希去重；对照分布按候选在数据记录中出现的次数加权。SQLite 在磁盘保存候选与长度频数，重复运行按配置更新输出，同一正文可复用长度缓存。编码显示进度条，文件上传中/缺正文/冲突 ID 会产生未就绪或部分结果。

## E. 输出与对接

全部输出在 `../ir_data/outputs/`：

| 文件 | 内容 |
|---|---|
| `final_stats.json` | **主汇总，给老师看这个**。按数据集 → 论文方法/设定 → split/子集组织，七项指标同级保存；token、word 各含四个分位数。数字、声明、参考值和缺项原因分开说明 |
| `inventory.csv` | 所有目录的缺失、分片数量、大小与残片检查 |
| `experiments/*.json` | 每个 config/子集完整结果、状态、样本预览和统计指纹 |
| `experiment_stats.csv` / `length_stats.csv` | 数量 / 长度明细，重跑按 config/子集更新 |
| `final_stats.csv` | 辅助表格导出，不能代替总 JSON 中的论文设定和口径说明 |
| `by_record_row.csv` | 每个数值带 config/子集标签，便于回填工作簿 |
| `issues.csv` | 未说明、标星、缺长度、部分统计和未就绪项 |
| `run_status.json` | 自动流程当前已处理的配置及结果状态 |
| `cache/*.sqlite` | 磁盘候选、正文、跨配置长度缓存 |
| `debug/` | limit 调试结果，不覆盖正式汇总 |

`configs/manifest.json` 继续维护原始设定，脚本不改它；`final_stats.json` 合并设定和服务器实测结果。每篇论文单列处理方式，未轮到的配置也保留，解释为何没有数字。数据集自身不规定 N→K、对齐语料没有 query、数学解题没有候选池，分别写清楚原因。

每个配置完成后自动更新总 JSON；也可运行 `python scripts/merge_results.py --preview 0` 仅重新整理，不重跑 token。全语料候选池和参考长度标 `*`；正文不全或论文抽样名单未公开时，不能当论文精确统计。脚本不自动修改 `record.xlsx`。

需要先展示一部分：`python scripts/export_preview.py --dataset r2med rearank_12k`，只提取已完成配置，不重新编码；展示 JSON 和简表在 `../ir_data/outputs/previews/ready_results.json`、`ready_results.md`。后续可在命令中加入已完成的其他数据集。

服务器边界验证（不读取真实数据集）：

```bash
python -m unittest discover -s tests -v
```
