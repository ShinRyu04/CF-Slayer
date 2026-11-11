from __future__ import annotations

from pathlib import Path

import pytest

import solver.pydoll_backend as pydoll模块
from solver.chain import 后端链, 链求解失败
from solver.errors import 求解失败
from solver.pydoll_backend import (
    Chrome主版本,
    Chrome用户代理,
    Pydoll后端,
    查找Chrome,
)


def test_显式Chrome路径优先(tmp_path):
    Chrome路径 = tmp_path / "chrome.exe"
    Chrome路径.write_bytes(b"")
    assert 查找Chrome(str(Chrome路径)) == str(Chrome路径)


def test_显式路径缺失时不回退(tmp_path):
    assert 查找Chrome(str(tmp_path / "missing.exe")) is None


def test_版本从Application目录读取(tmp_path):
    Chrome路径 = tmp_path / "Application" / "157.0.6099.62" / "chrome.exe"
    Chrome路径.parent.mkdir(parents=True)
    Chrome路径.write_bytes(b"")
    assert Chrome主版本(str(Chrome路径)) == "157"
    用户代理 = Chrome用户代理(str(Chrome路径))
    assert 用户代理 is not None
    assert "Chrome/157.0.0.0" in 用户代理
    assert "Headless" not in 用户代理


def test_版本缺失时返回None(tmp_path):
    Chrome路径 = tmp_path / "chrome.exe"
    Chrome路径.write_bytes(b"")
    assert Chrome主版本(str(Chrome路径)) is None
    assert Chrome用户代理(str(Chrome路径)) is None


def test_可用性报告缺少Chrome(tmp_path):
    后端对象 = Pydoll后端(Chrome路径=str(tmp_path / "nope.exe"))
    可用, 原因 = 后端对象.可用()
    assert 可用 is False
    assert "CF_SLAYER_CHROME_PATH" in 原因


def test_可用性报告缺少依赖包(monkeypatch, tmp_path):
    Chrome路径 = tmp_path / "chrome.exe"
    Chrome路径.write_bytes(b"")
    monkeypatch.setattr(pydoll模块.importlib.util, "find_spec", lambda name: None)
    后端对象 = Pydoll后端(Chrome路径=str(Chrome路径))
    可用, 原因 = 后端对象.可用()
    assert 可用 is False
    assert "未安装 pydoll" in 原因


def test_无Chrome时求解抛出求解失败(tmp_path):
    后端对象 = Pydoll后端(Chrome路径=str(tmp_path / "nope.exe"))
    with pytest.raises(求解失败, match="Chrome"):
        后端对象.求解("https://target.example/", 超时=5.0)


def test_链路跳过无Chrome的pydoll():
    pydoll后端 = Pydoll后端(Chrome路径="C:/definitely/missing/chrome.exe")
    链路 = 后端链([pydoll后端])
    with pytest.raises(链求解失败) as 信息:
        链路.求解("https://target.example/", 超时=1.0)
    assert 信息.value.尝试记录[0].后端名 == "pydoll"
    assert "找到 Chrome" in str(信息.value.尝试记录[0].错误)


def test_本机Windows可探测到Chrome():
    # Windows 验收检查：真实安装的 Chrome 必须能被找到。
    路径 = 查找Chrome()
    if 路径 is None:
        pytest.skip("本机未安装 Chrome")
    assert Path(路径).is_file()
