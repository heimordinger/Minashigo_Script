"""
pytest 配置 —— tests/
"""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="运行集成测试（截图、PostMessage 等真实操作）",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: 标记为集成测试（需要真实窗口交互）",
    )


def pytest_collection_modifyitems(config, items):
    """默认跳过 integration 标记的测试，除非 --run-integration 指定。"""
    if not config.getoption("--run-integration"):
        skip_integration = pytest.mark.skip(
            reason="需要 --run-integration 选项运行"
        )
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)
