import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from caliber.synth import generate  # noqa: E402


@pytest.fixture(scope="session")
def synth_db(tmp_path_factory) -> sqlite3.Connection:
    """生成一次合成库，整个测试会话复用。"""
    path = tmp_path_factory.mktemp("caliber") / "synth.db"
    generate(path)
    con = sqlite3.connect(path)
    yield con
    con.close()


@pytest.fixture()
def mem_db() -> sqlite3.Connection:
    """迷你内存库，用于给评分器做单测。"""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (g TEXT, v INTEGER)")
    con.executemany("INSERT INTO t VALUES (?,?)",
                    [("a", 1), ("a", 2), ("b", 3)])
    return con
