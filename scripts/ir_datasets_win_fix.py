#!/usr/bin/env python
"""ir_datasets 在 Windows 上的临时文件 bug —— 补丁 + CLI 直通。

为什么需要这个文件
------------------
ir_datasets 0.6.3 的 ``ir_datasets/util/download.py`` 第 264 行：

    tmpfile = tempfile.NamedTemporaryFile(delete=False, dir=util.tmp_path())
    atexit.register(_cleanup_tmp, tmpfile)
    download_path = tmpfile.name
    ...
    with util.finialized_file(download_path, 'wb') as f:   # 第 282 行
        ...                                                # 写完执行 os.replace

问题：Windows 上 ``NamedTemporaryFile`` 打开的句柄**不允许其他操作重命名/删除**
（没有 FILE_SHARE_DELETE）。而 ``tmpfile`` 被 ``atexit`` 强引用着，句柄一直开着，
于是第 282 行里的 ``os.replace(f'{path}.tmp', path)`` 撞上 **WinError 5 拒绝访问**；
随后 ``finialized_file`` 的兜底 ``os.remove`` 又撞上 **WinError 32 文件被占用**，
把刚下好的数据删掉，只在临时目录里留下一个 0 字节文件。

表现（实测）：
    [INFO] [starting] .../msmarco-test2019-queries.tsv.gz
    [INFO] [finished] .../msmarco-test2019-queries
    [WARNING] Download failed: [WinError 5] 拒绝访问。: '...tmpXXXX.tmp' -> '...tmpXXXX'
    PermissionError: [WinError 32] 另一个程序正在使用此文件

只在 ``Download`` 没有 ``cache_path`` 时才会走到这条分支（MS MARCO 的
trec-dl-* 系列正是如此），所以不是所有数据集都会触发。

修法
----
把 ``ir_datasets.util.download`` 模块里那个 ``tempfile`` 名字替换成一个
"用 ``mkstemp`` 建名后立刻 ``close``" 的替身，得到一个**没有打开句柄**的临时文件，
其余行为完全不变。只在 ``nt`` 上打补丁，其它平台直接透传。

注意：只替换该模块内的引用，不动全局 ``tempfile``，所以不影响 requests / tqdm 等。

用法
----
    python scripts/ir_datasets_win_fix.py export msmarco-passage/trec-dl-2019/judged queries

即：把 ``ir_datasets`` 命令换成 ``python scripts/ir_datasets_win_fix.py``，其余参数原样。
"""

from __future__ import annotations

import os
import sys
import tempfile


def install() -> bool:
    """给 ir_datasets 打补丁。返回是否真的打了（非 Windows 返回 False）。"""
    if os.name != "nt":
        return False

    try:
        import ir_datasets.util.download as download_module
    except ImportError:  # ir_datasets 没装
        return False

    class _Handle:
        """替身返回值：只提供 .name，不持有任何文件句柄。"""

        __slots__ = ("name",)

        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            pass

        def __enter__(self) -> "_Handle":
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

    class _TempfileShim:
        """只实现 download.py 用到的那一个入口。"""

        @staticmethod
        def NamedTemporaryFile(**kwargs: object) -> _Handle:
            fd, name = tempfile.mkstemp(
                suffix=kwargs.get("suffix") or "",
                prefix=kwargs.get("prefix") or "tmp",
                dir=kwargs.get("dir"),
            )
            os.close(fd)  # 关键：立刻关掉，别留句柄
            return _Handle(name)

    download_module.tempfile = _TempfileShim  # type: ignore[assignment]
    return True


def main() -> None:
    import ir_datasets

    if install():
        print(
            "[win-fix] 已给 ir_datasets.util.download 打好 Windows 临时文件补丁",
            file=sys.stderr,
        )
    sys.argv = ["ir_datasets", *sys.argv[1:]]
    ir_datasets.main_cli()


if __name__ == "__main__":
    main()
