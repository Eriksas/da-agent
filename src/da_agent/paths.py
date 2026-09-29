"""项目里用到的固定路径，集中放在一处。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"  # 原始下载文件，不进 git
PROCESSED_DIR = ROOT / "data" / "processed"  # 转换后的 parquet，不进 git，可随时重新生成
REPORTS_DIR = ROOT / "reports"  # 报告，进 git
FIXTURES_DIR = ROOT / "fixtures"  # 测试用的小样例
RUNS_DIR = ROOT / "runs"  # 每次 Agent 运行的完整记录，不进 git
