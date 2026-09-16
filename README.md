# IR 数据集第二阶段：准备、统计与核验

本目录承接完整的第二阶段工作。数据下载只是准备环节；后续还要按“论文 × 数据集 × split”确认实际使用方式，统一解析数据，并统计 query、候选池、正样本和样本长度。

下面的命令都假定当前工作目录是 `ir_code/`，所有项目文件使用相对路径。

## 第二阶段总流程

1. **数据准备**：下载公开数据、办理申请、登记未公开数据。
2. **使用配置**：逐篇记录实际 split、候选来源、输入数量和输出数量。
3. **数据解析**：把不同格式统一映射为 query、candidate、qrels 和 run。
4. **指标统计**：计算老师要求的 7 类指标。
5. **结果核验**：检查数量、抽样内容、缺失字段和带 `*` 的估算值。
6. **结果交付**：输出可回填表格的 CSV/JSON，并保留统计口径和运行记录。

当前先完成第 1 步。后续代码和结果按本文后半部分的接口继续添加，不另起一套目录。

## 目录约定

```text
项目根目录/
├── ir_code/                 # 下载说明、统计代码和输出
├── Qwen/                    # Qwen tokenizer / 模型
└── ir_data/
    ├── DATASET_INDEX.md     # record.xlsx 名称与实际目录的对照表
    ├── datasets/            # 真正的数据；每套底层数据只保存一份
    ├── application/         # 申请表、填写说明和邮件模板
    ├── _cache/              # Hugging Face、ir_datasets 等工具缓存
    └── _tools/              # 仅放下载或预处理工具，不放数据
```

数据目录统一使用 `../ir_data/datasets/<规范名称>/`。规范名称和表格行的对应关系见 [`../ir_data/DATASET_INDEX.md`](../ir_data/DATASET_INDEX.md)。

如果同一个官方数据包同时包含 train、dev、test（例如 NFCorpus、TripClick），只保存一套数据目录，但其中各 split 的文件不能混合，统计结果也必须分开。不同数据版本或不同语料仍建立独立目录，例如 MS MARCO Passage v1、Passage v2 和 Document v1 分开保存。TREC DL、PE-Rank、FullRank 等可以引用同一份 MS MARCO Passage v1 正文，不再复制正文文件。

## A. 数据准备

### A1. 下载前准备

以下命令按 Linux/bash 编写。

#### A1.1 创建 Conda 环境

环境统一命名为 `ir_stats`，使用 Python 3.11：

```bash
conda create -n ir_stats python=3.11 -y
conda activate ir_stats

python -m pip install -U pip
python -m pip install -r requirements.txt

conda install -c conda-forge -y git git-lfs wget jq unzip
git lfs install
```

这里暂不安装 GPU 版 PyTorch。第二阶段只使用 Qwen3-1.7B 的 tokenizer 统计 token 数，不需要加载模型权重；如果后面要运行模型，再按服务器 CUDA 版本单独安装 PyTorch。

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

### A2. 可直接下载的数据

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

# AIME 2024。它是辅助推理评测，不是 IR 测试集
hf download HuggingFaceH4/aime_2024 --type dataset \
  --local-dir ../ir_data/datasets/aime_2024
```

说明：表格中的 AMC 没有注明届次和数据版本，无法补齐；不要自行任选一个 AMC 数据集冒充论文版本。

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

下面四个文件足够提供基础语料、query 和 qrels。暂时不要下载 `top1000.train.tar.gz`；它约 175GB，并不等于各论文实际使用的 Top-20/50/100 候选。

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

该语料供 TREC DL 2019/2020 Document Ranking 使用。压缩包下载后约 22GB，解压需要更多空间。

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
  signal1m \
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

BEIR 中的 `Robust04` 和 `TREC-News` 没有公开的普通 BEIR ZIP；可先下载包含 `raw` 正文的 Castorini/Pyserini 预构建索引，见下一节。原始语料许可仍按“需要申请的数据”办理。

#### A2.11 BEIR / Robust04 与 TREC-News 的公开索引

Castorini/Pyserini 公开了这两个数据集的 BEIR `flat` Lucene 索引。索引构建时使用了 `-storeRaw`，每篇文档的原始 BEIR JSON（文档 ID、标题和正文）保存在索引中，因此后续可以导出正文并统计 token、word 和长度分位数。

这两份文件不是 NIST 官方原始压缩包，但更接近论文实际使用的 BEIR 处理版本：

| 数据集 | 压缩包大小 | 索引文档数 | MD5 |
| --- | ---: | ---: | --- |
| Robust04 | 1.61 GiB | 528,036 | `d508fc770002a99a5dc3da3d0fa001b7` |
| TREC-News | 2.44 GiB | 594,589 | `22e7752c3d0122c28013b33e5e2134ae` |

下载：

```bash
mkdir -p ../ir_data/datasets/beir/prebuilt_indexes/archives
mkdir -p ../ir_data/datasets/beir/prebuilt_indexes/indexes

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

echo "d508fc770002a99a5dc3da3d0fa001b7  robust04_beir_flat.tar.gz" | md5sum -c -
echo "22e7752c3d0122c28013b33e5e2134ae  trec-news_beir_flat.tar.gz" | md5sum -c -

cd ../../../../../ir_code
```

解压：

```bash
tar -xzf ../ir_data/datasets/beir/prebuilt_indexes/archives/robust04_beir_flat.tar.gz \
  -C ../ir_data/datasets/beir/prebuilt_indexes/indexes

tar -xzf ../ir_data/datasets/beir/prebuilt_indexes/archives/trec-news_beir_flat.tar.gz \
  -C ../ir_data/datasets/beir/prebuilt_indexes/indexes

find ../ir_data/datasets/beir/prebuilt_indexes/indexes -maxdepth 2 -type d | sort
```

后续读取时必须使用 `flat` 索引；dense、BGE 或 SPLADE 索引通常不保存正文。Pyserini 可以按 Lucene 内部文档编号遍历，并通过 `doc.raw()` 取得原始 JSON。正文导出脚本将在第二阶段统计代码中统一编写，不要现在把索引当普通文本文件读取。

queries 和 qrels 仍从 BEIR/TREC 官方公开文件读取；上述索引主要补齐受限的 corpus 正文。公开可下载不代表原始版权许可被取消，正式研究记录仍应保留 NIST 申请和授权信息。

来源：[Pyserini BEIR 索引构建记录](https://github.com/castorini/pyserini/blob/master/pyserini/resources/index-metadata/lucene-inverted.beir-v1.0.0-flat.20221116.505594.README.md)、[Pyserini 文档读取说明](https://github.com/castorini/pyserini/blob/master/docs/usage-fetch.md)。

#### A2.12 MTEB English v1/v2

MTEB 是任务集合，不是单一压缩包。必须按 E2Rank 论文使用的版本化 benchmark 下载，不能把 2026 年最新任务列表当成论文版本。

```bash
mkdir -p ../ir_data/datasets/mteb_english

export HF_DATASETS_CACHE=../ir_data/datasets/mteb_english

python - <<'PY'
import mteb

for benchmark_name in ("MTEB(eng, v1)", "MTEB(eng, v2)"):
    benchmark = mteb.get_benchmark(benchmark_name)
    print(benchmark_name, len(benchmark.tasks))
    for task in benchmark.tasks:
        task.load_data()
PY
```

MTEB 的 task 数据会进入 `../ir_data/datasets/mteb_english/`。执行完本节后，如需继续运行其他下载命令，可重新执行 0.2 节的缓存变量设置。下一阶段按 task 分开统计，不把 MTEB 汇总成一个虚假的统一 split。

#### A2.13 MIRAGE

MIRAGE 的 `benchmark.json` 已包含在官方仓库中：

```bash
git clone --depth 1 https://github.com/gzxiong/MIRAGE.git \
  ../ir_data/datasets/mirage
```

论文还提供各检索器的 Top-10K snippet ID 压缩包。它是后续计算真实候选池最有用的文件，可以尝试下载；如果链接再次失效，记录为“候选包未取得”，不要伪造候选池。

```bash
wget -c \
  https://virginia.box.com/shared/static/cxq17th6eisl2pn04vp0x723zczlvlzc.zip \
  -O ../ir_data/datasets/mirage/retrieved_snippets_10k.zip
```

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

若只为推进第二阶段统计，可先使用 1.11 节的公开 BEIR Lucene 索引；该索引含正文，但不替代本节的官方数据许可。

1. 打开 [TREC Disks 4 and 5](https://trec.nist.gov/data/cd45/)。
2. 填写并签署组织协议；个人协议由实际使用者签署并由单位留存。
3. 将组织协议扫描件发到 `trec-data-admin@nist.gov`。
4. 邮件主题写：`request for TREC disks 4 and 5`。
5. 获批后下载 Disks 4&5。Robust04 使用时要排除 Congressional Record。

#### A3.3 BEIR / TREC-News

申请难度：中等。流程与 Robust04 类似，同样需要单位签字；NIST 说明通常在 7 个工作日内回复。

若只为推进第二阶段统计，可先使用 1.11 节的公开 BEIR Lucene 索引；该索引含正文，但不是官方 `WashingtonPost.v2.tar.gz`，也不替代本节的数据许可。

1. 打开 [TREC Washington Post Corpus](https://trec.nist.gov/data/wapost/)。
2. 填写 [Organization Application](https://trec.nist.gov/data/wapost/Organization%20Application.pdf)。
3. 每位使用者填写 [Individual Application](https://trec.nist.gov/data/wapost/Individual%20Application.pdf)，由单位留存。
4. 将组织协议扫描件发到 `wapo-request@nist.gov`。
5. 邮件主题写：`request for Washington Post corpus`。

### A4. 无法通过申请补齐的数据

以下不是“需要申请”，而是论文没有公开成品数据或完整构造代码：

- LongRanker 10K 训练集；
- ResRank 重标注训练集；
- RRK 训练集；
- REARANK 论文中的 AMC 具体版本。

后续统计只能使用论文给出的汇总数字并标 `*`，或在作者补充数据后更新，不能用相似数据替代。

### A5. 下载后检查

```bash
du -sh ../ir_data/datasets/* | sort -h
find ../ir_data/datasets -maxdepth 4 -type f | sort > download_file_list.txt
```

`download_file_list.txt` 放在 `ir_code/`，便于后续记录实际取得的文件。对 TripClick 应按官网给出的 MD5 校验；对 Hugging Face 数据，后续在统计结果中记录仓库 revision。

## B. 论文使用配置

统计单位不是“数据集名称”本身，而是“论文 × 数据集 × split”。同一个数据集被不同论文使用时，split、候选检索器、输入数量和输出数量可能不同，不能共用一条配置。

后续建立两个配置文件：

```text
configs/
├── datasets.yaml       # 数据物理路径、字段、可用 split、语言
└── experiments.yaml    # 论文实际使用的 split、候选 run、N→K 和样本单位
```

`datasets.yaml` 每项至少记录：

- 表格中的正式名称和 `DATASET_INDEX.md` 中的规范路径；
- query、corpus、qrels、run 文件位置及字段映射；
- 可用 split、数据语言、是否需要解压或从 Lucene 导出；
- 数据版本、下载日期和 revision/checksum。

`experiments.yaml` 每项至少记录：

- 论文、数据集、split 和论文实际使用的 query 子集；
- 候选来源及候选 run 文件；
- 每个 query 输入多少个样本、最终输出多少个；
- 样本单位是 passage、document、paper 还是其他文本单元；
- 正样本判定规则，例如 `relevance > 0`；
- 论文未说明或文件未公开的字段，明确写 `unknown`，不自行补值。

这里的“样本单位”以**使用该数据集的论文实际送入排序模型的单位**为准，不以数据集原论文的存储单位为准。

## C. 数据解析与统一视图

不同数据保持原文件不动。解析脚本只读取 `../ir_data/datasets/`，在运行时统一为下面四类记录：

```text
query:     query_id, query_text, split, language
candidate: doc_id, text, title, language
qrel:      query_id, doc_id, relevance
run:       paper_id, dataset_id, query_id, doc_id, rank, score
```

计划脚本：

```text
scripts/
├── inventory.py          # 检查下载、版本、文件数量和大小
├── validate_config.py    # 检查路径、split、字段和重复配置
├── build_views.py        # 读取各数据格式，生成统一迭代视图
├── compute_counts.py     # query、N→K、候选池和正样本统计
├── compute_lengths.py    # token、word 和分位数统计
└── merge_results.py      # 合并结果并生成表格文件
```

大语料采用流式读取，不复制完整 corpus，也不生成另一份统一大文件。需要缓存的中间结果放 `outputs/cache/`，不能写回原始数据目录。

## D. 指标统计

最终对每条“论文 × 数据集 × split”配置统计：

1. **Query 数量**：论文实际使用的 query 数，不默认使用数据集全部 query。
2. **输入 N → 输出 K**：以论文实验设置为准，例如 `400 → 10`。若只是对 N 个候选全排序，则写 `N → N`。
3. **候选池数量**：优先计算该配置下所有 query 候选文档 ID 的并集。只有整个语料库或作者处理的大池子、没有实际 run 时，记录该数值并加 `*`。
4. **平均正样本数/query**：根据该配置的 qrels 和正样本阈值计算，同时保留无正样本 query 的数量。
5. **样本 token 数**：使用项目固定的 Qwen3-1.7B tokenizer，对论文实际送入排序模型的文本单元编码。
6. **样本 word 数**：只统计中文和英文。中文汉字每字计 1 个；英文按 Unicode 单词计数；标点不计，连续数字计 1 个。Echo E5、E2Rank 中已知的其他语言不混入 word 均值和分位数，单独报告语言、样本数和排除原因。
7. **长度分布**：token 和 word 分别输出 mean、P25、P50、P75、P90。

长度统计默认针对该配置实际进入排序的候选样本；如果候选 run 未公开，只能统计整个 corpus 或公开子集，结果需注明统计范围，不能写成论文实际输入。

## E. 核验与结果文件

结果统一放在 `outputs/`：

```text
outputs/
├── inventory.csv              # 数据文件、版本、大小和下载状态
├── experiment_stats.csv       # 每条论文 × 数据集 × split 的最终统计
├── length_stats.csv           # token/word 的均值和各分位数
├── issues.csv                 # 缺失 run、未知 split、估算值等问题
├── run_metadata.json          # tokenizer、代码版本和运行时间
└── cache/                     # 可重新生成的中间缓存
```

每次正式统计至少检查：

- query ID 能否在 qrels 和 run 中对应；
- candidate ID 能否在 corpus 中找到；
- 配置中的 N 与实际每个 query 的候选数是否一致；
- 随机抽查若干 query、正样本和文本内容；
- 所有 `*`、`unknown` 和缺失数据是否进入 `issues.csv`；
- 多语言数据是否记录语言，不把所有文本直接当英文分词。

## F. 执行顺序

后续按以下顺序推进：

```text
A 数据准备
  → B 填写论文使用配置
  → C 校验配置并建立统一视图
  → D 先统计数量，再统计 token/word
  → E 抽样核验并生成结果文件
  → 回填 record.xlsx
```

数据下载完成不代表可以直接统计。若论文实际候选 run 不存在，候选池和长度统计只能按现有范围计算并明确标 `*`；不能拿整个 corpus 冒充论文实际输入。
