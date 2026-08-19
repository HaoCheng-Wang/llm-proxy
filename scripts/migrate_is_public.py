#!/usr/bin/env python3
"""为 ports 表添加 is_public 列（一次性迁移脚本，手动执行）。

背景
----
新版本支持「代理公开（public）」：用户可在创建/编辑代理时把代理设为
所有用户可见（只读）。此变更需要在 ports 表新增 is_public 列。

本脚本是**唯一**的迁移入口——有意不放进 setup_schema()/启动自动迁移，
因为数据库结构变更要求显式执行、人工确认（用户明确要求不做向前兼容）。

策略（幂等、可重复执行）
----
- 查 INFORMATION_SCHEMA.COLUMNS 确认列是否已存在，存在则跳过 ALTER
- 不存在 → `ALTER TABLE ports ADD COLUMN is_public TINYINT(1) NOT NULL DEFAULT 0`
  （存量行自动取默认值 0 = 非 public）
- 显式执行 UPDATE 兜底，把历史数据统一置为非 public（正常情况为 no-op，
  满足「已有代理全部按非 public 处理」的要求）
- 打印验证结果：总行数 / public 数（应为 0）与逐列信息

用法
----
部署顺序：**先执行本脚本，再部署/重启新代码**（新代码的模型直接依赖该列，
不做兼容）。

    python scripts/migrate_is_public.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import create_engine, text

from config import DATABASE_URL

TABLE = "ports"
COLUMN = "is_public"
DDL = "ALTER TABLE ports ADD COLUMN is_public TINYINT(1) NOT NULL DEFAULT 0"


def main() -> int:
    engine = create_engine(
        DATABASE_URL,
        connect_args={"connect_timeout": 10, "read_timeout": 300, "write_timeout": 300},
        pool_pre_ping=True,
    )

    with engine.connect() as conn:
        # 1) 列是否已存在
        exists = conn.execute(
            text(
                "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
            ),
            {"t": TABLE, "c": COLUMN},
        ).first()
        if exists:
            print(f"[跳过] 列 {TABLE}.{COLUMN} 已存在，无需 ALTER", flush=True)
        else:
            print(f"[执行] {DDL}", flush=True)
            conn.execute(text(DDL))
            conn.commit()
            print("[完成] 列已添加（存量行自动取默认值 0 = 非 public）", flush=True)

        # 2) 兜底：历史数据统一置为非 public（正常为 no-op）
        result = conn.execute(
            text(
                f"UPDATE {TABLE} SET {COLUMN} = 0 "
                f"WHERE {COLUMN} IS NULL OR {COLUMN} <> 0"
            )
        )
        conn.commit()
        print(f"[兜底] UPDATE 影响行数 = {result.rowcount}（>0 说明修正了历史脏数据）", flush=True)

        # 3) 验证
        total, public_cnt = conn.execute(
            text(f"SELECT COUNT(*), COALESCE(SUM({COLUMN}), 0) FROM {TABLE}")
        ).first()
        print(f"[验证] ports 总行数 = {total}, public 数 = {public_cnt} "
              f"({'符合预期' if (public_cnt or 0) == 0 else '异常——存在 public 代理！'})",
              flush=True)

    engine.dispose()
    print("\n迁移完成。现在可以部署/重启新代码了。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
