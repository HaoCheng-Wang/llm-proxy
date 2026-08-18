#!/usr/bin/env python3
"""清理完成后的 watchdog：检测 cleanup_raw_body.py 结束后自动执行 OPTIMIZE。

用法
----
nohup python scripts/watchdog_optimize.py > optimize_watchdog.log 2>&1 &
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import create_engine, text

from config import DATABASE_URL

POLL_INTERVAL = 30          # 每 30 秒检查一次
MAX_WAIT_MINUTES = 360      # 最多等待 6 小时（防止 watchdog 永远挂起）


def main() -> int:
    print(f"[{time.strftime('%H:%M:%S')}] watchdog 启动，等待清理脚本结束...", flush=True)
    deadline = time.time() + MAX_WAIT_MINUTES * 60

    while time.time() < deadline:
        # 检查清理脚本进程是否还在
        ret = subprocess.run(
            ["pgrep", "-f", "cleanup_raw_body.py"],
            capture_output=True,
        )
        if ret.returncode != 0:
            # 清理脚本已结束
            print(f"[{time.strftime('%H:%M:%S')}] 清理脚本已结束，开始 OPTIMIZE...", flush=True)
            break
        time.sleep(POLL_INTERVAL)
    else:
        print(f"⚠️  等待超时（{MAX_WAIT_MINUTES} 分钟），清理脚本仍在运行，跳过 OPTIMIZE", flush=True)
        return 1

    engine = create_engine(
        DATABASE_URL,
        connect_args={"connect_timeout": 10, "read_timeout": 7200, "write_timeout": 7200},
        pool_pre_ping=True,
    )
    t0 = time.time()
    try:
        with engine.connect() as conn:
            # 先看清理是否真的完成（剩余冗余）
            redun = conn.execute(
                text(
                    "SELECT COUNT(*) FROM requests "
                    "WHERE reconstruction_error = 0 AND response_body_raw IS NOT NULL"
                )
            ).scalar()
            print(f"清理后剩余冗余: {redun} 行", flush=True)
            if redun > 0:
                print("⚠️  仍有冗余未清理（可能清理中断），跳过 OPTIMIZE 避免误操作", flush=True)
                return 1

            print(f"[{time.strftime('%H:%M:%S')}] 执行 OPTIMIZE TABLE requests（可能耗时数十分钟）...", flush=True)
            conn.execute(text("OPTIMIZE TABLE requests"))
        print(f"✅ OPTIMIZE 完成, 耗时 {time.time()-t0:.0f}s", flush=True)
        return 0
    except Exception as e:
        print(f"❌ OPTIMIZE 失败: {type(e).__name__}: {e}", flush=True)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
