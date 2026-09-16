#!/usr/bin/env bash
# =============================================================================
# push_to_remote.sh — 把本地 ir_data 增量同步到 AutoDL 实例
#
# 为什么不用 XFTP：
#   1. XFTP 是单线程 SFTP，且不压缩。这里 pe_rank(7G jsonl)、reasonrank(3.2G json)、
#      passage_v1(3G tsv) 都是可压文本，-z 后线上字节数只剩 1/3 左右。
#   2. rsync 有块级增量：已传过的部分不重传（远端 pe_rank 已有 1GB 前缀，能直接复用）。
#   3. 本脚本按数据集起多个并发 rsync，把单条 TCP 流的带宽瓶颈摊开。
#
# -----------------------------------------------------------------------------
# 用法（在 bash 里执行，任意目录都可以；PowerShell 用户先看下面那节）：
#
#   # 0) 一次性：装公钥，之后免密（推荐）
#   bash ir_code/scripts/push_to_remote.sh --setup-key
#
#   # 1) 先看要传什么，不实际传（强烈建议第一次先跑这个）
#   bash ir_code/scripts/push_to_remote.sh --plan     # 只看本地队列，不连远端
#   bash ir_code/scripts/push_to_remote.sh --list     # 连远端对比，算出真正要传的量
#
#   # 2) 正式传（默认 3 并发）
#   bash ir_code/scripts/push_to_remote.sh
#
#   # 3) 只传某几个数据集
#   bash ir_code/scripts/push_to_remote.sh --only miriad_4_4m,pe_rank_training_data
#
#   # 4) 别的花样
#   bash ir_code/scripts/push_to_remote.sh -j 5              # 5 并发
#   bash ir_code/scripts/push_to_remote.sh --no-compress     # 关压缩（内网/已压缩数据）
#   bash ir_code/scripts/push_to_remote.sh --slim            # 额外跳过 *.tar.gz / *.zip 原始包
#   bash ir_code/scripts/push_to_remote.sh --extras-only     # 只同步 DATASET_INDEX.md / application / outputs
#   bash ir_code/scripts/push_to_remote.sh --checksum        # 逐字节比对（一次性审计，慢）
#
# 【默认只比大小，不比修改时间】原因：XFTP 不保留 mtime，远端文件的修改时间是
# "传过去的时刻"。若按 rsync 默认的「大小+mtime」判断，远端已有的文件也会被当成变过了，
# 待传清单会虚高（实测：51.84 GiB 里有 6.2 GiB 属于纯误报，内容其实一致）。所以默认
# 加 --size-only。想改回 rsync 原生行为用 --mtime；想逐字节核对用 --checksum。
#
# 没装公钥时需要给密码。用 --pass，或者用环境变量 AUTODL_PASS：
#   bash ir_code/scripts/push_to_remote.sh --pass '你的密码'
#   AUTODL_PASS='你的密码' bash ir_code/scripts/push_to_remote.sh
#
# -----------------------------------------------------------------------------
# 【PowerShell 用户看这里】
#   PowerShell 里 `bash` 会解析到 C:\Windows\system32\bash.exe（WSL），那个环境没有 rsync。
#   必须显式用 msys64 的 bash，并且 PowerShell 不支持 VAR=value 前缀写法：
#
#     & "C:\ProgrammingEnv\msys64\usr\bin\bash.exe" ir_code/scripts/push_to_remote.sh --pass '你的密码'
#
#   如果嫌长，先开一个新的 Git Bash 窗口，在那边用上面的 bash 写法最省事。
# -----------------------------------------------------------------------------
# 传完后到远端核验：
#   ssh -p 33949 root@connect.cqa1.seetacloud.com
#   cd ~/autodl-tmp/ir_code && python scripts/check_data.py --all
# =============================================================================

# 本机 PATH 里 Windows 的 find/sort 会盖住 msys 的，先纠正
export PATH="/usr/bin:$PATH"
# 关掉 msys 的路径转换，否则 /root/... 会被改写成 C:/Program Files/Git/root/...
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

set -uo pipefail

# ============================== 配置区 ======================================
REMOTE_HOST="connect.cqa1.seetacloud.com"
REMOTE_PORT="33949"
REMOTE_USER="root"
REMOTE_BASE="/root/autodl-tmp/ir_data"     # 远端目标（绝对路径，不加尾斜杠）

JOBS=3                    # 默认并发数
COMPRESS=1                # 1=开 -z，0=关
COMPRESS_LEVEL=6
SLIM=0                    # 1=额外跳过可再下载的原始压缩包
DO_EXTRAS=1               # 1=同步 extras（DATASET_INDEX.md / application / outputs）
DO_SCRATCH=0              # 1=同步 _cache / _tools
MODE="push"               # push | list | plan
COMPARE="size"            # size(默认) | checksum | mtime —— 见下面「比什么」的说明
ONLY=""                   # 逗号分隔，空=全部
SKIP=""                   # 逗号分隔，空=不跳过
# =============================================================================

# ---------------------------- 路径解析（相对脚本位置） -----------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# scripts/ -> ir_code/ -> Medical/
LOCAL_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOCAL_DATA="$LOCAL_ROOT/ir_data"
LOCAL_DATASETS="$LOCAL_DATA/datasets"
LOG_DIR="$LOCAL_DATA/outputs/cache/upload_logs"

if [ ! -d "$LOCAL_DATASETS" ]; then
  echo "找不到本地数据目录：$LOCAL_DATASETS" >&2
  exit 1
fi
mkdir -p "$LOG_DIR"

# ---------------------------- 参数解析 --------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    -j|--jobs)         JOBS="${2:-3}"; shift 2 ;;
    --only)            ONLY="${2:-}"; shift 2 ;;
    --skip)            SKIP="${2:-}"; shift 2 ;;
    --pass)            AUTODL_PASS="${2:-}"; shift 2 ;;
    --slim)            SLIM=1; shift ;;
    --no-compress)     COMPRESS=0; shift ;;
    --compress-level)  COMPRESS_LEVEL="${2:-6}"; shift 2 ;;
    --no-extras)       DO_EXTRAS=0; shift ;;
    --extras-only)     ONLY="__extras__"; shift ;;
    --with-scratch)    DO_SCRATCH=1; shift ;;
    --list|--dry-run)  MODE="list"; shift ;;
    --plan)            MODE="plan"; shift ;;
    --selfcheck)       MODE="selfcheck"; shift ;;
    --size-only)       COMPARE="size"; shift ;;
    --checksum)        COMPARE="checksum"; shift ;;
    --mtime)           COMPARE="mtime"; shift ;;
    --setup-key)       MODE="setup-key"; shift ;;
    -h|--help)         sed -n '2,55p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（看 --help）" >&2; exit 2 ;;
  esac
done

# ---------------------------- 找 rsync / ssh ---------------------------------
# 关键：必须用和 rsync 同一个发行版的 ssh。Windows 自带的 OpenSSH 和 msys rsync
# 混用会在路径和密钥上出问题。
RSYNC_BIN="$(command -v rsync || true)"
if [ -z "$RSYNC_BIN" ]; then
  # Git Bash 不自带 rsync，PATH 里没有时去常见位置找一遍
  for cand in \
    "/c/ProgrammingEnv/msys64/usr/bin/rsync" \
    "/c/msys64/usr/bin/rsync" \
    "/d/msys64/usr/bin/rsync" \
    "/d/Git/usr/bin/rsync" \
    "/c/Program Files/Git/usr/bin/rsync"; do
    if [ -x "$cand" ]; then RSYNC_BIN="$cand"; break; fi
  done
fi
if [ -z "$RSYNC_BIN" ] || [ ! -x "$RSYNC_BIN" ]; then
  echo "本机没有 rsync。" >&2
  echo "  - 当前 bash 是：$BASH ($BASH_VERSION)" >&2
  echo "  - 如果你是 PowerShell 里敲的 bash，它可能指向了 WSL 的 C:\\Windows\\system32\\bash.exe，" >&2
  echo "    那个环境没有 rsync。请改用 msys64 的 bash：" >&2
  echo "    在 PowerShell 里执行： & \"C:\\ProgrammingEnv\\msys64\\usr\\bin\\bash.exe\" ir_code/scripts/push_to_remote.sh --help" >&2
  echo "  - 或装一个：msys2 下 pacman -S rsync" >&2
  exit 1
fi
RSYNC_DIR="$(cd "$(dirname "$RSYNC_BIN")" && pwd)"
SSH_BIN="$RSYNC_DIR/ssh"
[ -x "$SSH_BIN" ] || SSH_BIN="$(command -v ssh)"

SSHPASS_BIN="$(command -v sshpass || true)"
if [ -n "${AUTODL_PASS:-}" ]; then
  if [ -z "$SSHPASS_BIN" ]; then
    echo "设了 AUTODL_PASS 但本机没有 sshpass；请改用 --setup-key 装公钥。" >&2
    exit 1
  fi
  # 注意：这里必须用 -p 而不是 -e。msys 版的 sshpass 在本机会读不到 SSHPASS
  # 环境变量（实测报 "not set"），只有 -p 能正常工作。
  case "$AUTODL_PASS" in
    *[[:space:]]*)
      echo "密码里含空格，rsync 的 -e 参数无法正确切分；请用 --setup-key 装公钥。" >&2
      exit 1 ;;
  esac
  RSH_PREFIX="$SSHPASS_BIN -p $AUTODL_PASS"
else
  RSH_PREFIX=""
fi

# ssh 选项：-z 由 rsync 负责，ssh 本身别重复压
RSH_OPTS="-p $REMOTE_PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
-o ServerAliveInterval=30 -o ServerAliveCountMax=10 -o Compression=no -o LogLevel=ERROR"
RSH="$RSH_PREFIX $SSH_BIN $RSH_OPTS"
REMOTE_SPEC="$REMOTE_USER@$REMOTE_HOST"

# 已经压过、再压等于白烧 CPU 的后缀。
# 这是从 rsync 二进制里核对出来的**内置默认名单**（顺序：*.7z 起、*.zst 止），
# 末尾追加 parquet（columnar 已压缩，但不在 rsync 默认名单里）。
# 注意：--skip-compress 是**整体替换**默认名单，不是追加，所以必须写全。
SKIP_COMPRESS="7z,aac,ace,apk,avi,bz2,deb,dmg,ear,f4v,flac,flv,gpg,gz,iso,jar,jpeg,jpg,lrz,lz,lz4,lzma,lzo,m1a,m1v,m2a,m2ts,m2v,m4a,m4b,m4p,m4r,m4v,mka,mkv,mov,mp1,mp2,mp3,mp4,mpa,mpeg,mpg,mpv,mts,odb,odf,odg,odi,odm,odp,ods,odt,oga,ogg,ogm,ogv,ogx,opus,otg,oth,otp,ots,ott,oxt,png,parquet,qt,rar,rpm,rz,rzip,spx,squashfs,sxc,sxd,sxg,sxm,sxw,sz,tbz,tbz2,tgz,tlz,ts,txz,tzo,vob,war,webm,webp,xz,z,zip,zst"
# 特意**不**包含 tar：msmarco_v2_passage.tar 是未压缩纯 tar（魔数 6d736d61 = "msma"），
# 压缩率约 3 倍，是本批数据最大的压缩红利。

# ---------------------------- 排除规则 --------------------------------------
# 最重要的一条：下载中间件绝不能传。*.dl.* / *.part / *.aria2 是分片下载的碎片，
# 只有完整的下完之后它们才会被拼回去、自己消失。历史教训：msmarco_passage_v2
# 未下完时目录里有 16.8 GiB 这种碎片，传上去纯属浪费。
# .cache 是 HuggingFace 的本地下载缓存，远端不需要。
EXCL_GLOBS=(
  '*.dl.*'
  '*.part'
  '*.aria2'
  '*.before-aria2'
  '.cache'
  '__pycache__'
  '.rsync-partial'
  '.DS_Store'
  'Thumbs.db'
  'desktop.ini'
  '*.log'
)
if [ "$SLIM" = "1" ]; then
  # 已经解压出正文的原始压缩包，省 1G 左右；但这是"取舍"，只在 --slim 时生效
  EXCL_GLOBS+=( '*.tar.gz' '*.tgz' '*.zip' '*.tar' )
fi

EXCL_ARGS=()
for g in "${EXCL_GLOBS[@]}"; do EXCL_ARGS+=( "--exclude=$g" ); done

# 给 find 用的同一套规则，用来算"排除后要传多少字节"
FIND_PRUNE=( \( )
for i in "${!EXCL_GLOBS[@]}"; do
  [ "$i" -gt 0 ] && FIND_PRUNE+=( -o )
  FIND_PRUNE+=( -name "${EXCL_GLOBS[$i]}" )
done
FIND_PRUNE+=( \) -prune -o )

# ---------------------------- 小工具 ----------------------------------------
fmt_bytes() {
  awk -v b="${1:-0}" 'BEGIN{
    split("B KiB MiB GiB TiB",u," ");
    i=1; while (b>=1024 && i<5) { b/=1024; i++ }
    if (i==1) printf "%d B", b; else printf "%.2f %s", b, u[i]
  }'
}

# 按排除规则统计目录字节数
dir_bytes() {
  find "$1" "${FIND_PRUNE[@]}" -type f -printf '%s\n' 2>/dev/null \
    | awk '{s+=$1} END {printf "%d", s+0}'
}

now_ts() { date +%s; }

is_selected() {
  local n="$1"
  if [ -n "$ONLY" ]; then
    case ",$ONLY," in *",$n,"*) ;; *) return 1 ;; esac
  fi
  if [ -n "$SKIP" ]; then
    case ",$SKIP," in *",$n,"*) return 1 ;; esac
  fi
  return 0
}

human_dur() {
  local s="${1:-0}"
  printf '%02d:%02d:%02d' $((s/3600)) $(((s%3600)/60)) $((s%60))
}

# 参数自检：拿两个本地临时目录跑一遍完整的 rsync 参数表。
# 纯本地也会做参数解析，所以像 --contimeout 这种"只有 daemon 能用"的非法参数
# 会在这里立刻暴露，不用等连上远端、更不会传出去半个文件。
# 临时目录放在 LOG_DIR 下而不是 /tmp，避开 msys 对 /tmp 的路径映射差异。
preflight_rsync_flags() {
  local tmpd rc out
  tmpd="$(mktemp -d "$LOG_DIR/preflight.XXXXXX")" || return 0
  mkdir -p "$tmpd/src" "$tmpd/dst"
  : > "$tmpd/src/.probe"
  out="$("$RSYNC_BIN" "${RSYNC_FLAGS[@]}" "${EXCL_ARGS[@]}" \
        "$tmpd/src/" "$tmpd/dst/" 2>&1)"
  rc=$?
  rm -rf "$tmpd"
  if [ "$rc" -ne 0 ]; then
    echo "rsync 参数自检失败（退出码 $rc）：" >&2
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    return 1
  fi
  return 0
}

# ---------------------------- --setup-key -----------------------------------
if [ "$MODE" = "setup-key" ]; then
  echo "== 安装公钥到 $REMOTE_SPEC =="
  KEY="$HOME/.ssh/id_ed25519"
  [ -f "$KEY.pub" ] || ssh-keygen -t ed25519 -N '' -f "$KEY"
  # ssh-copy-id 在 msys 下也能用；失败就手动追加
  cat "$KEY.pub" | $RSH "$REMOTE_SPEC" \
    'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && echo OK'
  echo
  echo "完成。之后不带 AUTODL_PASS 直接跑即可。"
  exit $?
fi

# ---------------------------- 组装任务队列 ----------------------------------
QUEUE=()
QUEUE_BYTES=()

for d in "$LOCAL_DATASETS"/*/; do
  [ -d "$d" ] || continue
  name="$(basename "$d")"
  is_selected "$name" || continue
  n="$(find "$d" -type f | wc -l)"
  if [ "$n" -eq 0 ]; then
    echo "跳过 $name：本地没有文件（还没下载）"
    continue
  fi
  b="$(dir_bytes "$d")"
  if [ "$b" -eq 0 ]; then
    echo "跳过 $name：排除下载中间件后没有可传内容（比如刚下到一个 0 字节的 .part）"
    continue
  fi
  QUEUE+=( "$name" )
  QUEUE_BYTES+=( "$b" )
done

if [ "${#QUEUE[@]}" -eq 0 ] && [ "$ONLY" != "__extras__" ]; then
  echo "没有可同步的数据集。" >&2
  exit 1
fi

# 大的排前面：先把长尾任务铺开，并发效率最高
if [ "${#QUEUE[@]}" -gt 1 ]; then
  mapfile -t SORTED < <(
    for i in "${!QUEUE[@]}"; do printf '%s\t%s\n' "${QUEUE_BYTES[$i]}" "${QUEUE[$i]}"; done \
      | sort -rn
  )
  QUEUE=(); QUEUE_BYTES=()
  for line in "${SORTED[@]}"; do
    QUEUE+=( "${line#*$'\t'}" )
    QUEUE_BYTES+=( "${line%%$'\t'*}" )
  done
fi

TOTAL_BYTES=0
for b in "${QUEUE_BYTES[@]:-}"; do [ -n "$b" ] && TOTAL_BYTES=$(( TOTAL_BYTES + b )); done

# 本地还在下载中的数据集：只看"活动下载"标记（*.dl.* / *.part / *.aria2）。
# 不算 *.before-aria2 —— 那是 aria2 重下时留下的历史残留，不代表正在下。
DOWNLOADING=()
for name in "${QUEUE[@]:-}"; do
  [ -z "$name" ] && continue
  if [ -n "$(find "$LOCAL_DATASETS/$name" \
        \( -name '*.dl.*' -o -name '*.part' -o -name '*.aria2' \) \
        -print -quit 2>/dev/null)" ]; then
    DOWNLOADING+=( "$name" )
  fi
done

print_downloading_warning() {
  [ "${#DOWNLOADING[@]}" -eq 0 ] && return
  echo "提醒：以下数据集本地还在下载中（目录里有 aria2 分片，已自动排除，不会上传）："
  local n
  for n in "${DOWNLOADING[@]}"; do echo "  - $n"; done
  echo "      未下载完就别急着传，可以加 --skip <名字> 先跳过。"
  echo
}

# ---------------------------- "比什么" 决定重不重传 ---------------------------
# 【关键】rsync 默认的 quick check 是「大小 + 修改时间」。而 XFTP 这类工具**不保留
# mtime**，远端文件的 mtime 是"传过去的时刻"，于是本地每个文件都会被判定成"变过了"
# —— 明明内容一模一样，也会被列进待传清单（随后走 delta 算法实际只发很少字节，
# 但仍要读两端全部数据算校验和，白白浪费几十分钟）。
# 实测证据（2026-09-16）：e2rank/train.jsonl 两端都是 1,831,839,221 字节，
# 本地 mtime 13:17:56、远端 18:30:50 → 默认模式下会被判为需要重传。
#
# size     : 只比大小，忽略 mtime。**这种情况下的正确选择**，秒跳过已传文件。
#            代价：万一有个"大小相同但内容不同"的文件会漏掉（正常下载/传输不会出现）。
# checksum : 逐字节内容比对，最准，但要读两端全部数据，慢。适合一次性审计。
# mtime    : rsync 默认行为。只在两端都保留 mtime（比如一直用 rsync 传的）时才合适。
COMPARE_ARGS=()
case "$COMPARE" in
  size)     COMPARE_ARGS=( --size-only ) ;;
  checksum) COMPARE_ARGS=( --checksum ) ;;
  mtime)    COMPARE_ARGS=() ;;
  *)        echo "未知的比对方式：$COMPARE" >&2; exit 2 ;;
esac

# ---------------------------- RSYNC 基础参数 --------------------------------
RSYNC_FLAGS=(
  -a
  --partial                      # 中断保留半成品
  --partial-dir=.rsync-partial   # 半成品放这里，目录看起来始终是"完整文件"
  --no-inc-recursive             # 先列完整文件表，progress2 的总量才准
  --human-readable
  --info=progress2
  --stats
  --no-motd
)
# 注意：**不要**加 --contimeout。它只能用于 rsync daemon（rsync://）连接，
# 走 -e ssh 时 rsync 会直接以 code 1 退出：
#   The --contimeout option may only be used when connecting to an rsync daemon.
#   rsync error: syntax or usage error (code 1) at main.c(1552)
# 实测踩过：16 个数据集会在 28 秒内全部"秒失败"，而 --list 模式因为重建了参数表
# 反而不含它，所以预演是绿的、真实传输全红。超时交给 ssh 的 ServerAliveInterval。
RSYNC_FLAGS+=( "${COMPARE_ARGS[@]}" )
if [ "$COMPRESS" = "1" ]; then
  RSYNC_FLAGS+=( --compress "--compress-level=$COMPRESS_LEVEL" "--skip-compress=$SKIP_COMPRESS" )
fi
if [ "$MODE" = "list" ]; then
  RSYNC_FLAGS=( -rn --no-motd "${COMPARE_ARGS[@]}" "${EXCL_ARGS[@]}" -e "$RSH" )
fi

# ---------------------------- --selfcheck：只验参数 --------------------------
if [ "$MODE" = "selfcheck" ]; then
  echo "== rsync 参数自检（纯本地，不连远端、不传文件）=="
  echo "参数表：${RSYNC_FLAGS[*]}"
  echo "排除项：${EXCL_ARGS[*]}"
  echo
  if preflight_rsync_flags; then
    echo "通过：参数表合法，可以放心开传。"
    exit 0
  fi
  echo "不通过：上面就是原始报错。修好再传。" >&2
  exit 2
fi

# ---------------------------- --plan：只看本地队列 ---------------------------
if [ "$MODE" = "plan" ]; then
  echo "== 本地队列（不连远端）=="
  echo "源：$LOCAL_DATA/datasets"
  echo
  printf '%-30s %10s %12s\n' "数据集" "文件数" "可传体积"
  printf '%-30s %10s %12s\n' "------------------------------" "--------" "----------"
  for i in "${!QUEUE[@]}"; do
    name="${QUEUE[$i]}"
    printf '%-30s %10d %12s\n' "$name" "$(find "$LOCAL_DATASETS/$name" -type f | wc -l)" "$(fmt_bytes "${QUEUE_BYTES[$i]}")"
  done
  printf '%-30s %10s %12s\n' "------------------------------" "--------" "----------"
  printf '%-30s %10s %12s\n' "合计（已扣排除项）" "" "$(fmt_bytes "$TOTAL_BYTES")"
  echo
  echo "排除的规则：${EXCL_GLOBS[*]}"
  echo
  print_downloading_warning
  echo "下一步：加 --list 连远端对比，算出「真正还需要传」的量。"
  exit 0
fi

# ---------------------------- --list：只报数 ---------------------------------
if [ "$MODE" = "list" ]; then
  echo "== 预演：本地 ir_data -> $REMOTE_SPEC:$REMOTE_BASE =="
  echo "（排除规则已生效；远端已有的文件不会重复计算）"
  echo
  printf '%-30s %10s %10s %12s\n' "数据集" "本地文件" "待传文件" "待传体积"
  printf '%-30s %10s %10s %12s\n' "------------------------------" "--------" "--------" "----------"
  SUM_F=0; SUM_B=0
  for i in "${!QUEUE[@]}"; do
    name="${QUEUE[$i]}"
    src="$LOCAL_DATASETS/$name"
    dst="$REMOTE_BASE/datasets/$name"
    local_n="$(find "$src" -type f | wc -l)"
    tmp="$LOG_DIR/$name.dryrun.log"
    need_n=0; need_b=0
    if $RSYNC_BIN "${RSYNC_FLAGS[@]}" --out-format='%l %n' "$src/" "$REMOTE_SPEC:$dst/" > "$tmp" 2>&1; then
      read -r need_n need_b < <(awk '{ if ($2 !~ /\/$/) { n++; s+=$1 } } END { print n+0, s+0 }' "$tmp")
    else
      # 不吞错误：预演失败就把原因打出来，别让人以为"待传 0"
      printf '%-30s %10d %10s %12s\n' "$name" "$local_n" "?" "预演失败"
      echo "     原因：$(tail -n 2 "$tmp" | tr '\n' ' ')" >&2
      continue
    fi
    printf '%-30s %10d %10d %12s\n' "$name" "$local_n" "$need_n" "$(fmt_bytes "$need_b")"
    SUM_F=$(( SUM_F + need_n )); SUM_B=$(( SUM_B + need_b ))
  done
  printf '%-30s %10s %10s %12s\n' "------------------------------" "--------" "--------" "----------"
  printf '%-30s %10s %10d %12s\n' "合计" "" "$SUM_F" "$(fmt_bytes "$SUM_B")"
  echo
  echo "（--slim 未开启时，已解压出的 *.tar.gz 原始包也会被算进去）"
  exit 0
fi

# ---------------------------- 单个数据集同步 --------------------------------
run_one() {
  local name="$1" idx="$2"
  local src="$LOCAL_DATASETS/$name"
  local dst="$REMOTE_BASE/datasets/$name"
  local log="$LOG_DIR/$name.log"
  local st="$LOG_DIR/$name.state"
  local t0 t1 rc=0

  printf '=== %s -> %s ===\n' "$name" "$dst" > "$log"
  echo "RUNNING" > "$st"
  t0=$(now_ts)

  $RSYNC_BIN "${RSYNC_FLAGS[@]}" "${EXCL_ARGS[@]}" -e "$RSH" \
    "$src/" "$REMOTE_SPEC:$dst/" >> "$log" 2>&1 || rc=$?

  t1=$(now_ts)
  # run_one 跑在后台子进程里，数组改动不会回传，所以耗时写文件
  echo "$(( t1 - t0 ))" > "$LOG_DIR/$name.sec"
  if [ "$rc" -eq 0 ]; then echo "DONE" > "$st"; else echo "FAIL:$rc" > "$st"; fi
  return $rc
}

# ---------------------------- 进度屏 ----------------------------------------
# 取日志里最后一条"像进度"的行（rsync 的尾行是 --stats 统计，不能当进度用）
read_last_progress() {
  [ -s "$1" ] || { echo ""; return; }
  tr '\r' '\n' < "$1" 2>/dev/null | awk '/[0-9]%[ \t]/ { last = $0 } END { print last }'
}

# "12.34MB/s" -> 字节/秒（注意 rsync 打印的是小写 kB/s）
speed_to_bps() {
  awk -v s="${1:-}" 'BEGIN{
    if (s == "") { print 0; exit }
    v = s + 0
    if      (s ~ /[Tt]B\/s/) m = 1099511627776
    else if (s ~ /[Gg]B\/s/) m = 1073741824
    else if (s ~ /[Mm]B\/s/) m = 1048576
    else if (s ~ /[Kk]B\/s/) m = 1024
    else                     m = 1
    printf "%d", v * m
  }'
}

# 读某个数据集此刻的进度，回填全局量：STATE / PCT / SPD / ETA
# 只认本次运行产生的文件（比 $STAMP 新），否则会把上一次运行的残留状态/日志当成本次的，
# 让"排队中"的数据集显示成上次的"失败"或上次的进度。
probe_one() {
  local name="$1" st log line
  st="$LOG_DIR/$name.state"
  log="$LOG_DIR/$name.log"
  if [ -f "$st" ] && [ "$st" -nt "$STAMP" ]; then
    STATE="$(cat "$st")"
  else
    STATE="PENDING"
  fi
  PCT=0; SPD="-"; ETA="-"
  if [ "$STATE" = "DONE" ]; then
    PCT=100
  elif [ -f "$log" ] && [ "$log" -nt "$STAMP" ]; then
    # RUNNING / FAIL 都读一次进度（失败的也让人看到传到了哪）
    line="$(read_last_progress "$log")"
    if [ -n "$line" ]; then
      PCT="$(printf '%s' "$line" | grep -oE '[0-9]{1,3}%' | head -1 | tr -d '%')"
      SPD="$(printf '%s' "$line" | grep -oE '[0-9.]+[kKMGTP]?B/s' | head -1)"
      ETA="$(printf '%s' "$line" | grep -oE '[0-9]+:[0-9]{2}(:[0-9]{2})?' | tail -1)"
    fi
    PCT="${PCT:-0}"; SPD="${SPD:--}"; ETA="${ETA:--}"
  fi
}

mark_of() {
  case "$1" in
    DONE)    echo "完成" ;;
    RUNNING) echo "传输" ;;
    FAIL:*)  echo "失败" ;;
    PENDING) echo "排队" ;;
    *)       echo "$1" ;;
  esac
}

draw_screen() {
  local elapsed=$(( $(now_ts) - START_TS ))
  local total=0 moved=0 spd_sum=0 idx name bytes b_moved

  printf '\033[H\033[2J'
  printf '  \033[1mir_data  ->  %s:%s\033[0m\n' "$REMOTE_SPEC" "$REMOTE_BASE"
  printf '  并发 %s   已用 %s   日志 %s\n' "$JOBS" "$(human_dur $elapsed)" "$LOG_DIR"
  printf '  %s\n\n' "---------------------------------------------------------------"

  for idx in "${!QUEUE[@]}"; do
    name="${QUEUE[$idx]}"
    bytes="${QUEUE_BYTES[$idx]}"
    total=$(( total + bytes ))
    probe_one "$name"
    b_moved=$(( bytes * PCT / 100 ))
    moved=$(( moved + b_moved ))
    [ "$STATE" = "RUNNING" ] && spd_sum=$(( spd_sum + $(speed_to_bps "$SPD") ))
    # 注意：SPD 本身已经带 "/s"，这里不要再拼一个
    printf '  %-27s %-5s %4s%% %11s %9s  %s\n' \
      "$name" "$(mark_of "$STATE")" "$PCT" "$SPD" "$ETA" "$(fmt_bytes "$bytes")"
  done

  local all_pct=0
  [ "$total" -gt 0 ] && all_pct=$(( moved * 100 / total ))

  # 总进度条
  local width=44 filled i bar=""
  filled=$(( all_pct * width / 100 ))
  for (( i=0; i<width; i++ )); do
    if [ "$i" -lt "$filled" ]; then bar+='#'; else bar+='.'; fi
  done

  local eta_all=""
  if [ "$spd_sum" -gt 0 ] && [ "$total" -gt "$moved" ]; then
    eta_all="  预计剩余 $(human_dur $(( (total - moved) / spd_sum )))"
  fi

  printf '\n  [%s] %3s%%  %s / %s   瞬时 %s/s%s\n' \
    "$bar" "$all_pct" "$(fmt_bytes "$moved")" "$(fmt_bytes "$total")" \
    "$(fmt_bytes "$spd_sum")" "$eta_all"
  printf '\n  按 Ctrl-C 可中断；已传部分会保留，重跑自动续传。\n'
}

# 输出不是终端（PowerShell / cmd 里跑 bash）时用这个：纯文本、不重绘、不带 ANSI 码。
# 清屏码在这种场合等于把输出全吃掉，所以只能老老实实追加几行。
draw_screen_plain() {
  local elapsed=$(( $(now_ts) - START_TS ))
  local total=0 moved=0 spd_sum=0 idx name bytes
  local -a PPCT=() PSPD=() PST=() PETA=()

  for idx in "${!QUEUE[@]}"; do
    name="${QUEUE[$idx]}"
    bytes="${QUEUE_BYTES[$idx]}"
    total=$(( total + bytes ))
    probe_one "$name"
    PPCT[$idx]="$PCT"; PSPD[$idx]="$SPD"; PST[$idx]="$STATE"; PETA[$idx]="$ETA"
    moved=$(( moved + bytes * PCT / 100 ))
    [ "$STATE" = "RUNNING" ] && spd_sum=$(( spd_sum + $(speed_to_bps "$SPD") ))
  done

  local all_pct=0
  [ "$total" -gt 0 ] && all_pct=$(( moved * 100 / total ))
  local eta_all=""
  if [ "$spd_sum" -gt 0 ] && [ "$total" -gt "$moved" ]; then
    eta_all="  预计剩余 $(human_dur $(( (total - moved) / spd_sum )))"
  fi

  local out
  out="$(printf '[%s] 总 %3s%%  %s / %s   瞬时 %s/s%s' \
    "$(human_dur $elapsed)" "$all_pct" "$(fmt_bytes "$moved")" \
    "$(fmt_bytes "$total")" "$(fmt_bytes "$spd_sum")" "$eta_all")"

  local j
  for j in "${!QUEUE[@]}"; do
    [ "${PST[$j]}" = "RUNNING" ] || continue
    out+="$(printf '\n      %-26s %4s%%  %10s  剩 %s' \
      "${QUEUE[$j]}" "${PPCT[$j]}" "${PSPD[$j]}" "${PETA[$j]}")"
  done

  printf '%s\n' "$out"
  # 同时落一份到文件：万一某个终端就是吞输出，用 tail / Get-Content -Wait 也能看
  printf '%s\n' "$out" >> "$LOG_DIR/progress.log" 2>/dev/null || true
}

# ---------------------------- 中断处理 --------------------------------------
PIDS=()
MON_PID=""
cleanup() {
  trap - INT TERM
  [ -n "$MON_PID" ] && kill "$MON_PID" 2>/dev/null
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done
  echo
  echo "已中断。半成品保留在远端各数据集的 .rsync-partial/ 下，重跑会自动续传。"
  exit 130
}
trap cleanup INT TERM

# ---------------------------- 主流程 ----------------------------------------
START_TS=$(now_ts)
# 本次运行的"起跑线"标记：只有比它更新的 state/log 才算本次的进度，
# 否则排队中的数据集会显示上一次运行留下的"失败"或旧进度。
STAMP="$LOG_DIR/.run-start"
: > "$STAMP"
# 进度快照（纯文本），万一行里显示的都被终端吞了，还能 tail 这个文件
: > "$LOG_DIR/progress.log"

echo
echo "本地：$LOCAL_DATA"
echo "远端：$REMOTE_SPEC:$REMOTE_BASE"
echo "本地候选 ${#QUEUE[@]} 个数据集，本地体积合计 $(fmt_bytes "$TOTAL_BYTES")（已扣除下载中间件）"
echo "  注意：这是【本地总量】，不是要传的量。远端已有的文件会由 rsync 逐个跳过，"
echo "        所以这个数字明显偏大。想知道真正要传多少，先跑 --list（会连远端核对）。"
echo
# 立刻告诉用户进度会以什么形式出现，别让人对着空白等
if [ ! -t 1 ]; then
  echo "输出不是终端（从 PowerShell / cmd 调 bash.exe 就是这样），"
  echo "进度会用纯文本每 20 秒打一行；也可以另开窗口跑 scripts/upload_status.sh 看详情。"
  echo
fi
echo
print_downloading_warning
if [ -z "${AUTODL_PASS:-}" ]; then
  echo "提示：未设置 AUTODL_PASS，将使用公钥/交互式密码。"
  echo "      每个并发任务都需要一次密码输入，建议先跑 --setup-key。"
  echo
fi

# 开传之前先自检参数，避免"每个数据集都秒失败、却不知道差在哪"
if ! preflight_rsync_flags; then
  echo
  echo "已中止：rsync 参数有问题（没有连远端、没有传任何文件）。" >&2
  exit 2
fi

sleep 1

# 并发闸门：始终保持不超过 JOBS 个 rsync 在跑
i=0
for name in "${QUEUE[@]}"; do
  while :; do
    running=0
    for p in "${PIDS[@]:-}"; do
      [ -n "$p" ] && kill -0 "$p" 2>/dev/null && running=$(( running + 1 ))
    done
    [ "$running" -lt "$JOBS" ] && break
    sleep 2
  done
  run_one "$name" "$i" &
  PIDS[$i]=$!
  i=$(( i + 1 ))
done

# 进度屏。注意 stdout 是不是终端，两种渲染方式差别很大：
#   - 是终端 → 每 5 秒原地重绘一张表（ANSI 清屏）
#   - 不是终端 → bash.exe 从 PowerShell / cmd 启动时 stdout 是**管道**，
#     实测 [ -t 1 ] 为假。这时改印纯文本行，每 20 秒追加几行。
#
# 【为什么每个 draw 都套一层 ( ... )】
# bash 在 stdout 是管道时会用**全缓冲**，只在 fork 外部命令等特定时刻才 flush，
# 于是横幅之后的进度行会一直卡在缓冲区里，用户看起来"什么都没有"。
# 放进一个用完即退出的子 shell，退出时会 flush 自己的 stdio，进度就能实时出现。
if [ -t 1 ]; then
  ( while :; do ( draw_screen ); sleep 5; done ) &
else
  ( while :; do ( draw_screen_plain ); sleep 20; done ) &
fi
MON_PID=$!

# 只等 worker，不等进度屏
FAILED=0
for p in "${PIDS[@]:-}"; do
  [ -z "$p" ] && continue
  wait "$p" || FAILED=$(( FAILED + 1 ))
done

[ -n "$MON_PID" ] && kill "$MON_PID" 2>/dev/null
MON_PID=""
trap - INT TERM

# ---------------------------- extras ----------------------------------------
if [ "$DO_EXTRAS" = "1" ] && [ "${#QUEUE[@]}" -gt 0 ]; then
  echo
  echo "== 同步 extras（索引 / 申请材料 / 统计产物）=="
  XFLAGS=( -a --no-motd --info=progress2 --human-readable --compress --compress-level=6
           --exclude=upload_logs )
  for x in "DATASET_INDEX.md" "application/" "outputs/"; do
    [ -e "$LOCAL_DATA/$x" ] || continue
    $RSYNC_BIN "${XFLAGS[@]}" -e "$RSH" \
      "$LOCAL_DATA/$x" "$REMOTE_SPEC:$REMOTE_BASE/" || FAILED=$(( FAILED + 1 ))
    echo "  ok: $x"
  done
fi

if [ "$DO_SCRATCH" = "1" ]; then
  echo
  echo "== 同步 _cache / _tools =="
  for x in "_cache/" "_tools/"; do
    [ -e "$LOCAL_DATA/$x" ] || continue
    $RSYNC_BIN -a --no-motd --info=progress2 -e "$RSH" \
      "$LOCAL_DATA/$x" "$REMOTE_SPEC:$REMOTE_BASE/" || FAILED=$(( FAILED + 1 ))
    echo "  ok: $x"
  done
fi

# ---------------------------- 汇总 ------------------------------------------
ELAPSED=$(( $(now_ts) - START_TS ))
echo
echo "==================== 汇总 ===================="
printf '%-30s %-8s %10s %10s\n' "数据集" "状态" "体积" "耗时"
printf '%-30s %-8s %10s %10s\n' "------------------------------" "------" "--------" "--------"
for idx in "${!QUEUE[@]}"; do
  name="${QUEUE[$idx]}"
  st="$(cat "$LOG_DIR/$name.state" 2>/dev/null || echo UNKNOWN)"
  case "$st" in
    DONE)   mark="完成" ;;
    FAIL:*) mark="失败" ;;
    *)      mark="$st" ;;
  esac
  printf '%-30s %-8s %10s %10s\n' "$name" "$mark" "$(fmt_bytes "${QUEUE_BYTES[$idx]}")" "$(human_dur "$(cat "$LOG_DIR/$name.sec" 2>/dev/null || echo 0)")"
done
echo "---------------------------------------------"
echo "总计耗时 $(human_dur $ELAPSED)，失败 $FAILED 项"
echo
echo "远端核验："
echo "  ssh -p $REMOTE_PORT $REMOTE_SPEC"
echo "  cd ~/autodl-tmp/ir_code && python scripts/check_data.py --all"
echo
echo "清理远端续传残留（确认无失败项后再执行）："
echo "  find $REMOTE_BASE -type d -name .rsync-partial -exec rm -rf {} +"

exit $(( FAILED > 0 ? 1 : 0 ))
