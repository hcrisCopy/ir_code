# IR 数据集统计项目

本目录放第二阶段的代码和说明；所有数据统一放在同级目录 `../ir_data/`。下面的命令都假定当前工作目录是 `ir_code/`，不使用绝对路径。

## 目录约定

```text
项目根目录/
├── ir_code/  
└── Qwen/                    # 放qwen模型
└── ir_data/                 # 原始数据和下载缓存
    ├── _cache/
    ├── msmarco_passage_v1/
    ├── msmarco_passage_v2/
    ├── msmarco_document_v1/
    ├── hf/
    ├── beir/
    ├── r2med/
    └── medical/
```

不要把同一底层语料重复下载。例如 TREC DL 2019/2020 Passage、PE-Rank、FullRank 等都复用 MS MARCO Passage v1；训练集和测试集在统计时分开，但磁盘上只保留一份语料。

## 0. 下载前准备

以下命令按 Linux/bash 编写。

```bash
mkdir -p ../ir_data/{_cache,hf,beir,r2med,medical}

export HF_HOME=../ir_data/_cache/huggingface
export HF_DATASETS_CACHE=../ir_data/_cache/huggingface/datasets
export IR_DATASETS_HOME=../ir_data/_cache/ir_datasets

python -m pip install -U huggingface_hub datasets ir_datasets mteb gdown
```

服务器还需要 `git`、`git-lfs`、`wget`、`tar`、`unzip` 和 `jq`。Hugging Face 数据均为公开仓库，一般不用登录；出现限流时再执行 `hf auth login`。

## 1. 可直接下载的数据

### 1.1 Hugging Face 数据包

```bash
# PE-Rank 训练集
hf download liuqi6777/pe_rank_data --type dataset \
  --local-dir ../ir_data/hf/pe_rank_data

# RankZephyr 训练集
hf download castorini/rank_zephyr_training_data --type dataset \
  --local-dir ../ir_data/hf/rank_zephyr_training_data

# E2Rank 训练集
hf download Alibaba-NLP/E2Rank_ranking_datasets --type dataset \
  --local-dir ../ir_data/hf/e2rank_ranking_datasets

# FullRank 训练集，公开数据名不是“FullRank training data”，而是下面这个仓库
hf download liuwenhan/msmarco_full_ranking_list --type dataset \
  --local-dir ../ir_data/hf/msmarco_full_ranking_list

# REARANK 12K
hf download le723z/rearank_12k --type dataset \
  --local-dir ../ir_data/hf/rearank_12k

# ReasonRank 13K，包含正文映射，体积较大
hf download liuwenhan/reasonrank_data_13k --type dataset \
  --local-dir ../ir_data/hf/reasonrank_data_13k

# MIRIAD-4.4M；只有 train split
hf download miriad/miriad-4.4M --type dataset \
  --local-dir ../ir_data/hf/miriad_4_4m

# BRIGHT
hf download xlangai/BRIGHT --type dataset \
  --local-dir ../ir_data/hf/bright

# AIME 2024。它是辅助推理评测，不是 IR 测试集
hf download HuggingFaceH4/aime_2024 --type dataset \
  --local-dir ../ir_data/hf/aime_2024
```

说明：表格中的 AMC 没有注明届次和数据版本，无法补齐；不要自行任选一个 AMC 数据集冒充论文版本。

### 1.2 R2MED

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
    --local-dir "../ir_data/r2med/${name}"
done
```

### 1.3 MS MARCO Passage v1

下面四个文件足够提供基础语料、query 和 qrels。暂时不要下载 `top1000.train.tar.gz`；它约 175GB，并不等于各论文实际使用的 Top-20/50/100 候选。

```bash
mkdir -p ../ir_data/msmarco_passage_v1

wget -c https://msmarco.z22.web.core.windows.net/msmarcoranking/collection.tar.gz \
  -P ../ir_data/msmarco_passage_v1
wget -c https://msmarco.z22.web.core.windows.net/msmarcoranking/queries.tar.gz \
  -P ../ir_data/msmarco_passage_v1
wget -c https://msmarco.z22.web.core.windows.net/msmarcoranking/qrels.train.tsv \
  -P ../ir_data/msmarco_passage_v1
wget -c https://msmarco.z22.web.core.windows.net/msmarcoranking/qrels.dev.tsv \
  -P ../ir_data/msmarco_passage_v1

tar -xzf ../ir_data/msmarco_passage_v1/collection.tar.gz \
  -C ../ir_data/msmarco_passage_v1
tar -xzf ../ir_data/msmarco_passage_v1/queries.tar.gz \
  -C ../ir_data/msmarco_passage_v1
```

官方说明：[MS MARCO ranking datasets](https://microsoft.github.io/msmarco/Datasets.html)。

### 1.4 MS MARCO Document v1

该语料供 TREC DL 2019/2020 Document Ranking 使用。压缩包下载后约 22GB，解压需要更多空间。

```bash
mkdir -p ../ir_data/msmarco_document_v1
wget -c https://msmarco.z22.web.core.windows.net/msmarcoranking/msmarco-docs.tsv.gz \
  -P ../ir_data/msmarco_document_v1
```

### 1.5 MS MARCO Passage v2

REARANK 使用 v2。下载语料、训练 query、qrels 和官方 Top-100 候选即可。

```bash
mkdir -p ../ir_data/msmarco_passage_v2

MSMARCO_BASE_URL=https://msmarco.z22.web.core.windows.net/msmarcoranking
for file in \
  msmarco_v2_passage.tar \
  passv2_train_queries.tsv \
  passv2_train_qrels.tsv \
  passv2_train_top100.txt.gz
do
  wget -c --header "X-Ms-Version: 2019-12-12" \
    "${MSMARCO_BASE_URL}/${file}" -P ../ir_data/msmarco_passage_v2
done
```

`msmarco_v2_passage.tar` 约 20.3GB，内部是 70 个 gzip JSONL 文件。官方入口：[TREC Deep Learning / MS MARCO v2](https://github.com/microsoft/msmarco/blob/master/TREC-Deep-Learning.md)。

### 1.6 TREC DL 2019/2020 的 query 和 qrels

语料已经由 MS MARCO v1 提供。用 `ir_datasets` 导出小型评测文件：

```bash
mkdir -p ../ir_data/trec_dl

ir_datasets export msmarco-passage/trec-dl-2019/judged queries \
  > ../ir_data/trec_dl/trec_dl_2019_passage_queries.tsv
ir_datasets export msmarco-passage/trec-dl-2019/judged qrels \
  > ../ir_data/trec_dl/trec_dl_2019_passage_qrels.tsv

ir_datasets export msmarco-passage/trec-dl-2020/judged queries \
  > ../ir_data/trec_dl/trec_dl_2020_passage_queries.tsv
ir_datasets export msmarco-passage/trec-dl-2020/judged qrels \
  > ../ir_data/trec_dl/trec_dl_2020_passage_qrels.tsv

ir_datasets export msmarco-document/trec-dl-2019/judged queries \
  > ../ir_data/trec_dl/trec_dl_2019_document_queries.tsv
ir_datasets export msmarco-document/trec-dl-2019/judged qrels \
  > ../ir_data/trec_dl/trec_dl_2019_document_qrels.tsv

ir_datasets export msmarco-document/trec-dl-2020/judged queries \
  > ../ir_data/trec_dl/trec_dl_2020_document_queries.tsv
ir_datasets export msmarco-document/trec-dl-2020/judged qrels \
  > ../ir_data/trec_dl/trec_dl_2020_document_qrels.tsv
```

这里没有统一下载“论文候选集”，因为各论文分别使用 BM25、Jina、SPLADE-v3 或自有检索器。论文实际 run 文件将在第二阶段按论文补充；没有 run 文件时，候选池只能标星号。

### 1.7 KILT Wikipedia Passages

```bash
mkdir -p ../ir_data/kilt
wget -c http://dl.fbaipublicfiles.com/KILT/kilt_knowledgesource.json \
  -O ../ir_data/kilt/kilt_knowledgesource.json
```

该文件约 34.76GiB。论文抽取的 10M passages 没有单独发布，后续只能根据论文规则重建或标注为“论文子集未公开”。官方说明：[KILT repository](https://github.com/facebookresearch/KILT)。

### 1.8 Echo E5

```bash
mkdir -p ../ir_data/echo_e5
gdown --fuzzy \
  'https://drive.google.com/file/d/1YqgaJIzmBIH37XBxpRPCVzV_CLh6aOI4/view' \
  -O ../ir_data/echo_e5/echo-data.tar
tar -xf ../ir_data/echo_e5/echo-data.tar -C ../ir_data/echo_e5
```

官方发布页：[Echo Embeddings](https://github.com/jakespringer/echo-embeddings)。该数据包含中英文以外的语言；token 可以统一统计，word 统计需按既定语言规则处理。

### 1.9 Atlas Wikipedia 2020-12

Atlas 官方通过脚本下载并解压语料：

```bash
mkdir -p ../ir_data/_tools ../ir_data/atlas
git clone --depth 1 https://github.com/facebookresearch/atlas.git \
  ../ir_data/_tools/atlas

python ../ir_data/_tools/atlas/preprocessing/download_corpus.py \
  --corpus corpora/wiki/enwiki-dec2020 \
  --output_directory ../ir_data/atlas
```

论文使用的是从约 31.5M 文本中抽取的 2M 子集；公开的是完整 `enwiki-dec2020`，2M 子集本身没有单独下载文件。

### 1.10 BEIR 公共子集、NFCorpus 和 TREC-COVID

下面一次下载覆盖表格中的 BEIR-7/8/12公共部分，同时覆盖单独列出的 NFCorpus 和 TREC-COVID。不要再重复下载 NFCorpus。

```bash
mkdir -p ../ir_data/beir

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
    -P ../ir_data/beir
done

for file in ../ir_data/beir/*.zip; do
  unzip -n "$file" -d ../ir_data/beir
done
```

BEIR 中的 `Robust04` 和 `TREC-News` 不在这一段下载，见“需要申请的数据”。

### 1.11 MTEB English v1/v2

MTEB 是任务集合，不是单一压缩包。必须按 E2Rank 论文使用的版本化 benchmark 下载，不能把 2026 年最新任务列表当成论文版本。

```bash
python - <<'PY'
import mteb

for benchmark_name in ("MTEB(eng, v1)", "MTEB(eng, v2)"):
    benchmark = mteb.get_benchmark(benchmark_name)
    print(benchmark_name, len(benchmark.tasks))
    for task in benchmark.tasks:
        task.load_data()
PY
```

数据会进入前面设置的 `../ir_data/_cache/huggingface/`。下一阶段按 task 分开统计，不把 MTEB 汇总成一个虚假的统一 split。

### 1.12 MIRAGE

MIRAGE 的 `benchmark.json` 已包含在官方仓库中：

```bash
git clone --depth 1 https://github.com/gzxiong/MIRAGE.git \
  ../ir_data/medical/mirage
```

论文还提供各检索器的 Top-10K snippet ID 压缩包。它是后续计算真实候选池最有用的文件，可以尝试下载；如果链接再次失效，记录为“候选包未取得”，不要伪造候选池。

```bash
wget -c \
  https://virginia.box.com/shared/static/cxq17th6eisl2pn04vp0x723zczlvlzc.zip \
  -O ../ir_data/medical/mirage/retrieved_snippets_10k.zip
```

官方入口：[MIRAGE](https://github.com/gzxiong/MIRAGE)。

### 1.13 PMC-Patients / ReCDS

Figshare collection 中有三个独立文件：

```bash
mkdir -p ../ir_data/medical/pmc_patients

wget -c https://ndownloader.figshare.com/files/43050394 \
  -O ../ir_data/medical/pmc_patients/PMC-Patients.json.tar.gz
wget -c https://ndownloader.figshare.com/files/43054744 \
  -O ../ir_data/medical/pmc_patients/ReCDS_benchmark.tar.gz
wget -c https://ndownloader.figshare.com/files/43055212 \
  -O ../ir_data/medical/pmc_patients/Meta_data.tar.gz
```

第二阶段至少需要 `PMC-Patients.json.tar.gz` 和 `ReCDS_benchmark.tar.gz`。官方 collection：[PMC-Patients](https://figshare.com/collections/PMC-Patients/6723465)。

## 2. 需要申请的数据

### 2.1 TripClick

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

```text
../ir_data/tripclick/benchmark.tar.gz
../ir_data/tripclick/dlfiles.tar.gz
../ir_data/tripclick/dlfiles_runs_test.tar.gz
```

如果后续使用 `ir_datasets`，将这些文件复制或软链接到：

```text
../ir_data/_cache/ir_datasets/tripclick/
```

官方流程：[TripClick access page](https://tripdatabase.github.io/tripclick/)。

### 2.2 BEIR / Robust04

申请难度：中等。需要单位负责人签署组织协议；NIST 说明通常在 7 个工作日内回复。

1. 打开 [TREC Disks 4 and 5](https://trec.nist.gov/data/cd45/)。
2. 填写并签署组织协议；个人协议由实际使用者签署并由单位留存。
3. 将组织协议扫描件发到 `trec-data-admin@nist.gov`。
4. 邮件主题写：`request for TREC disks 4 and 5`。
5. 获批后下载 Disks 4&5。Robust04 使用时要排除 Congressional Record。

### 2.3 BEIR / TREC-News

申请难度：中等。流程与 Robust04 类似，同样需要单位签字；NIST 说明通常在 7 个工作日内回复。

1. 打开 [TREC Washington Post Corpus](https://trec.nist.gov/data/wapost/)。
2. 填写 [Organization Application](https://trec.nist.gov/data/wapost/Organization%20Application.pdf)。
3. 每位使用者填写 [Individual Application](https://trec.nist.gov/data/wapost/Individual%20Application.pdf)，由单位留存。
4. 将组织协议扫描件发到 `wapo-request@nist.gov`。
5. 邮件主题写：`request for Washington Post corpus`。

## 3. 无法通过申请补齐的数据

以下不是“需要申请”，而是论文没有公开成品数据或完整构造代码：

- LongRanker 10K 训练集；
- ResRank 重标注训练集；
- RRK 训练集；
- REARANK 论文中的 AMC 具体版本。

后续统计只能使用论文给出的汇总数字并标 `*`，或在作者补充数据后更新，不能用相似数据替代。

## 4. 下载后检查

```bash
du -sh ../ir_data/* | sort -h
find ../ir_data -maxdepth 3 -type f | sort > download_file_list.txt
```

`download_file_list.txt` 放在 `ir_code/`，便于后续记录实际取得的文件。对 TripClick 应按官网给出的 MD5 校验；对 Hugging Face 数据，后续在统计结果中记录仓库 revision。

## 5. 第二阶段后续文件规划

后续在本目录继续增加：

```text
ir_code/
├── README.md                  # 本说明和执行顺序
├── configs/                   # 每篇论文实际 split、候选来源、N→K 配置
├── scripts/                   # 统一统计脚本
├── download_file_list.txt     # 实际下载文件清单
└── outputs/                   # CSV/JSON 统计结果，不放原始数据
```

注意：数据下载完成不等于可以精确统计候选池。候选池必须来自“该论文实际使用的 query-candidate run 文件”；只有基础语料总量时，结果必须标 `*`。
