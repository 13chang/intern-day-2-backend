"""``app/envfile.py`` 的测试。

要守住的两件事：

1. 解析够用且不越界：注释、空行、``export`` 前缀、成对引号、值里带 ``=`` 都要正确；
   键名不合法就跳过，不能把垃圾塞进环境变量。
2. **真实环境变量优先**。这条一旦反过来，容器里传了 ``MODEL_MODE=mock``
   会被开发机上的 ``.env`` 悄悄改回 ``real`` —— 属于"看起来一切正常但账单很吓人"的那类事故。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app import envfile

REPO_ROOT = Path(__file__).resolve().parents[1]


def write_env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_plain_key_value(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DEMO_ALPHA", raising=False)
    path = write_env(tmp_path, "DEMO_ALPHA=one\n")

    loaded = envfile.load_env_file(path)

    assert loaded == ["DEMO_ALPHA"]
    assert os.environ["DEMO_ALPHA"] == "one"


def test_skips_comments_blank_lines_and_bad_keys(tmp_path, monkeypatch) -> None:
    for key in ("DEMO_OK", "9BAD", "BAD_KEY"):
        monkeypatch.delenv(key, raising=False)
    path = write_env(
        tmp_path,
        """
# 注释
DEMO_OK=yes

9BAD=开头是数字
BAD KEY=键名里有空格
没有等号的一行
""",
    )

    loaded = envfile.load_env_file(path)

    assert loaded == ["DEMO_OK"]
    assert os.environ["DEMO_OK"] == "yes"
    assert "9BAD" not in os.environ
    assert "BAD KEY" not in os.environ


def test_handles_export_prefix_and_quotes(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DEMO_EXPORT", raising=False)
    monkeypatch.delenv("DEMO_QUOTED", raising=False)
    monkeypatch.delenv("DEMO_EQUALS", raising=False)
    path = write_env(
        tmp_path,
        """export DEMO_EXPORT=with-prefix
DEMO_QUOTED="hello world"
DEMO_EQUALS=a=b=c
""",
    )

    envfile.load_env_file(path)

    assert os.environ["DEMO_EXPORT"] == "with-prefix"
    # 引号要去掉，中间的空格要保留
    assert os.environ["DEMO_QUOTED"] == "hello world"
    # 只按第一个 = 切分，值里的 = 原样保留（base_url 里常见）
    assert os.environ["DEMO_EQUALS"] == "a=b=c"


def test_existing_environment_wins(tmp_path, monkeypatch) -> None:
    """容器 / CI 注入的值不能被 .env 覆盖。"""
    monkeypatch.setenv("DEMO_MODE", "mock")
    path = write_env(tmp_path, "DEMO_MODE=real\n")

    loaded = envfile.load_env_file(path)

    assert loaded == []
    assert os.environ["DEMO_MODE"] == "mock"


def test_empty_value_in_environment_is_treated_as_unset(tmp_path, monkeypatch) -> None:
    """``MODEL_MODE=`` 这种空值应当允许被 .env 补上。

    compose 里写的正是 ``${MODEL_MODE:-}`，没设置时会传成空串。
    如果把空串当成"已设置"，本地跑起来就永远是 Mock，而且看不出原因。
    """
    monkeypatch.setenv("DEMO_EMPTY", "")
    path = write_env(tmp_path, "DEMO_EMPTY=from-file\n")

    loaded = envfile.load_env_file(path)

    assert loaded == ["DEMO_EMPTY"]
    assert os.environ["DEMO_EMPTY"] == "from-file"


def test_missing_file_is_not_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DEMO_NONE", raising=False)
    assert envfile.load_env_file(tmp_path / "nope.env") == []


def test_skip_flag_disables_implicit_lookup(tmp_path, monkeypatch) -> None:
    """``APP_SKIP_ENV_FILE=1`` 只挡自动查找（conftest 靠它保证测试与开发机 .env 无关）。"""
    monkeypatch.setenv("APP_SKIP_ENV_FILE", "1")
    monkeypatch.delenv("DEMO_SKIP", raising=False)
    monkeypatch.chdir(tmp_path)
    write_env(tmp_path, "DEMO_SKIP=1\n")

    assert envfile.load_env_file() == []
    assert "DEMO_SKIP" not in os.environ


def test_explicit_path_ignores_skip_flag(tmp_path, monkeypatch) -> None:
    """显式给路径时，跳过标记不该生效 —— 那是调用方明确指定要读的文件。"""
    monkeypatch.setenv("APP_SKIP_ENV_FILE", "1")
    monkeypatch.delenv("DEMO_EXPLICIT", raising=False)
    path = write_env(tmp_path, "DEMO_EXPLICIT=loaded\n")

    assert envfile.load_env_file(path) == ["DEMO_EXPLICIT"]


def test_package_init_loads_dotenv_before_config_reads_it(tmp_path) -> None:
    """端到端：只 ``import app``，环境里就应该出现 .env 里那个值。

    这条盯的是"把加载放在 ``app/__init__.py``"这个决定本身 ——
    如果哪天有人把这段挪进 ``main.py``，Agent 已经在 ``main`` 导入时就按 Mock
    构建完了，再补环境也来不及，而这个用例会立刻红。
    """
    (tmp_path / ".env").write_text(
        "MODEL_MODE=real\nOPENAI_MODEL=demo-model\n", encoding="utf-8"
    )
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"MODEL_MODE", "OPENAI_MODEL", "APP_SKIP_ENV_FILE", "PYTHONPATH"}
    }
    child_env["PYTHONPATH"] = str(REPO_ROOT)

    result = subprocess.run(
        [sys.executable, "-c", "import app, os; print(os.environ.get('MODEL_MODE'), os.environ.get('OPENAI_MODEL'))"],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "real demo-model"
