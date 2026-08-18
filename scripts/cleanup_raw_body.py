#!/usr/bin/env python3
"""批量清理 requests 表中冗余的 response_body_raw 字段。

背景
----
旧版本无论 SSE 重建是否成功都会把原始流式文本存入 response_body_raw，
而解析成功时 response_body 已含完整响应 JSON——原始文本是纯冗余
（实测可达 5–70 倍体积），是 requests 表膨胀（52GB）的大头。
新版本已改为"仅重建失败时保存"，本脚本用于清理存量数据。

策略（id 区间推进，全程单次表扫描）
----
- 每批按主键 id 区间推进（WHERE id BETWEEN :lo AND :hi），每批 5000 行
- 直接对区间内所有行尝试 UPDATE（reconstruction_error=0 AND raw NOT NULL），
  已 NULL 的行匹配 0 行、开销极小——避免每批 SELECT 全表扫描（O(n²) 陷阱）
- 每批 commit，可随时中断、幂等重跑（已清的行直接跳过）
- 严格只清 reconstruction_error=0 的记录（解析失败的原样保留）
- 每 20 批 sleep 0.5s 节流，避免压垮 MySQL
- 每 50 批输出进度日志

用法
----
python scripts/cleanup_raw_body.py              # 只清理数据
python scripts/cleanup_raw_body.py --optimize   # 清理完成后执行 OPTIMIZE TABLE
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import create_engine, text

from config import DATABASE_URL

BATCH_SIZE = 2000
SLEEP_EVERY_BATCHES = 10
SLEEP_SECONDS = 0.5
LOG_EVERY_BATCHES = 100


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--optimize", action="store_true",
        help="清理完成后执行 OPTIMIZE TABLE requests 回收物理空间",
    )
    args = parser.parse_args()

    engine = create_engine(
        DATABASE_URL,
        connect_args={"connect_timeout": 10, "read_timeout": 3600, "write_timeout": 3600},
        pool_pre_ping=True,
    )

    total_cleared = 0
    batch = 0
    start = time.time()
    try:
        with engine.connect() as conn:
            # 单次取表的最大 id（走主键索引，秒级）
            max_id = conn.execute(text("SELECT MAX(id) FROM requests")).scalar() or 0
            print(f"表最大 id = {max_id}，开始按区间推进...", flush=True)

            lo = 0
            while lo <= max_id:
                hi = lo + BATCH_SIZE - 1
                result = conn.execute(
                    text(
                        "UPDATE requests SET response_body_raw = NULL "
                        "WHERE id BETWEEN :lo AND :hi "
                        "AND reconstruction_error = 0 "
                        "AND response_body_raw IS NOT NULL"
                    ),
                    {"lo": lo, "hi": hi},
                )
                affected = result.rowcount
                conn.commit()
                total_cleared += affected
                batch += 1
                if affected > 0 and batch % LOG_EVERY_BATCHES == 0:
                    elapsed = time.time() - start
                    rate = total_cleared / elapsed if elapsed > 0 else 0
                    print(
                        f"[{time.strftime('%H:%M:%S')}] 已清理 {total_cleared} 条 "
                        f"(区间 id={lo}-{hi}, {rate:.0f} 条/秒, 批 {batch})",
                        flush=True,
                    )
                if batch % SLEEP_EVERY_BATCHES == 0:
                    time.sleep(SLEEP_SECONDS)
                lo = hi + 1

        print(f"✅ 数据清理完成: 共清理 {total_cleared} 条冗余记录, 耗时 {time.time()-start:.1f}s")
    finally:
        engine.dispose()

    if args.optimize:
        print("🔧 执行 OPTIMIZE TABLE requests（MySQL 8 在线重建，耗时可能较长）...")
        opt_start = time.time()
        engine2 = create_engine(
            DATABASE_URL,
            connect_args={"connect_timeout": 10, "read_timeout": 7200, "write_timeout": 7200},
        )
        try:
            with engine2.connect() as conn:
                conn.execute(text("OPTIMIZE TABLE requests"))
            print(f"✅ OPTIMIZE 完成, 耗时 {time.time()-opt_start:.1f}s")
        finally:
            engine2.dispose()

    return 0


if __name__ == "__main__":
    sys.exit(main())
