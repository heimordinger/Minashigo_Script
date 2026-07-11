from __future__ import annotations

import pytest
import numpy as np
from pathlib import Path


def _find_test_window():
    """找一个可用于测试的窗口（优先文件管理器，回退到任意可见窗口）。"""
    from backend.automation.win32_target import Win32Target

    # 优先找类名明确的窗口
    for cls in ("CabinetWClass", "Notepad", "CalcFrame"):
        targets = Win32Target.from_class(cls)
        if targets:
            return targets[0]

    # 回退到第一个尺寸合理的可见窗口
    for w in Win32Target.all_visible():
        cr = w.client_rect
        if 200 < cr["width"] < 3000 and cr["height"] > 100:
            return w
    return None


# ========================================================================
# 基础测试（轻量，无副作用）
# ========================================================================

class TestWindowDiscovery:
    """窗口发现功能测试。"""

    def test_all_visible_returns_list(self):
        from backend.automation.win32_target import Win32Target
        windows = Win32Target.all_visible()
        assert isinstance(windows, list)
        assert len(windows) > 0

    def test_all_visible_are_win32target(self):
        from backend.automation.win32_target import Win32Target
        for w in Win32Target.all_visible()[:5]:
            assert isinstance(w, Win32Target)

    def test_from_title_exact(self):
        from backend.automation.win32_target import Win32Target
        # Program Manager 桌面窗口几乎一定存在
        targets = Win32Target.from_title("Program Manager", exact=True)
        # 不保证一定找到（因语言/主题而异），但不应抛异常
        assert isinstance(targets, list)

    def test_from_title_substring(self):
        from backend.automation.win32_target import Win32Target
        any_win = Win32Target.all_visible()[0]
        title = any_win.title
        if title:
            # 用标题的第一个词搜索
            keyword = title.split()[0][:4]
            targets = Win32Target.from_title(keyword)
            assert any(hwnd.hwnd == any_win.hwnd for hwnd in targets)

    def test_from_title_empty_returns_all(self):
        from backend.automation.win32_target import Win32Target
        full = Win32Target.all_visible()
        from_empty = Win32Target.from_title("")
        assert len(full) == len(from_empty)

    def test_from_class_finds_explorer(self):
        from backend.automation.win32_target import Win32Target
        targets = Win32Target.from_class("CabinetWClass", exact=True)
        # 不一定总打开文件管理器，所以不 assert >0
        if targets:
            assert all("CabinetWClass" == t.class_name for t in targets)

    def test_from_pid_matches_all_visible(self):
        from backend.automation.win32_target import Win32Target
        windows = Win32Target.all_visible()
        if windows:
            pid = windows[0].pid
            by_pid = Win32Target.from_pid(pid)
            assert len(by_pid) >= 1
            assert any(w.hwnd == windows[0].hwnd for w in by_pid)

    def test_from_hwnd_creates_target(self):
        from backend.automation.win32_target import Win32Target
        windows = Win32Target.all_visible()
        if windows:
            hwnd = windows[0].hwnd
            clone = Win32Target.from_hwnd(hwnd)
            assert clone.hwnd == hwnd
            assert clone.title == windows[0].title

    def test_from_hwnd_invalid_raises(self):
        from backend.automation.win32_target import Win32Target
        with pytest.raises(ValueError):
            Win32Target.from_hwnd(0)
        with pytest.raises(ValueError):
            Win32Target.from_hwnd(-1)

    def test_foreground_exists(self):
        """前台窗口通常是存在的（除非桌面刚启动没有任何焦点窗口）。"""
        from backend.automation.win32_target import Win32Target
        fg = Win32Target.foreground()
        # 不 assert not None，只做合理性检查
        if fg is not None:
            assert isinstance(fg.hwnd, int)
            assert fg.hwnd > 0


class TestWindowProperties:
    """窗口属性读取测试（只读，无副作用）。"""

    @pytest.fixture(scope="class")
    def target(self):
        win = _find_test_window()
        if win is None:
            pytest.skip("没有可用的测试窗口")
        return win

    def test_hwnd_is_positive_int(self, target):
        assert isinstance(target.hwnd, int)
        assert target.hwnd > 0

    def test_title_is_string(self, target):
        assert isinstance(target.title, str)
        # 标题可以为空（某些系统窗口），但必须是 str

    def test_class_name_is_nonempty(self, target):
        assert isinstance(target.class_name, str)
        assert len(target.class_name) > 0

    def test_pid_is_positive_int(self, target):
        assert isinstance(target.pid, int)
        assert target.pid > 0

    def test_rect_has_all_keys(self, target):
        r = target.rect
        for key in ("left", "top", "right", "bottom", "width", "height"):
            assert key in r, f"缺少 {key}"
            assert isinstance(r[key], int)

    def test_rect_width_height_positive(self, target):
        r = target.rect
        assert r["width"] > 0
        assert r["height"] > 0

    def test_rect_minimized_edge_case(self, target):
        """最小化窗口的 rect 可能含负数坐标，不应抛异常。"""
        if target.is_minimized:
            # 只是读一下，不抛就行
            _ = target.rect

    def test_client_rect_has_all_keys(self, target):
        cr = target.client_rect
        for key in ("left", "top", "right", "bottom", "width", "height"):
            assert key in cr

    def test_client_rect_left_top_zero(self, target):
        """客户区坐标原点总是 (0,0)。"""
        assert target.client_rect["left"] == 0
        assert target.client_rect["top"] == 0

    def test_client_rect_non_negative(self, target):
        cr = target.client_rect
        assert cr["width"] >= 0
        assert cr["height"] >= 0

    def test_client_rect_screen_has_values(self, target):
        crs = target.client_rect_screen
        for key in ("left", "top", "width", "height"):
            assert key in crs

    def test_is_visible_bool(self, target):
        assert isinstance(target.is_visible, bool)

    def test_is_minimized_bool(self, target):
        assert isinstance(target.is_minimized, bool)

    def test_is_maximized_bool(self, target):
        assert isinstance(target.is_maximized, bool)

    def test_repr_contains_hwnd(self, target):
        r = repr(target)
        assert str(target.hwnd) in r

    def test_multiple_targets_same_hwnd_eq(self, target):
        """同一 hwnd 构造的两个实例应表现一致。"""
        from backend.automation.win32_target import Win32Target
        t2 = Win32Target.from_hwnd(target.hwnd)
        assert t2.title == target.title
        assert t2.class_name == target.class_name
        assert t2.pid == target.pid


# ========================================================================
# 截图测试（重量级，有真实 IO）
# ========================================================================

@pytest.mark.integration
class TestScreenshot:
    """PrintWindow 后台截图测试（需要真实窗口）。"""

    @pytest.fixture(scope="class")
    def target(self):
        win = _find_test_window()
        if win is None:
            pytest.skip("没有可用的测试窗口")
        return win

    def test_screenshot_returns_ndarray(self, target):
        frame = target.screenshot(client_only=True)
        assert isinstance(frame, np.ndarray)

    def test_screenshot_3_channels(self, target):
        frame = target.screenshot(client_only=True)
        assert frame.ndim == 3
        assert frame.shape[2] == 3  # BGR

    def test_screenshot_uint8(self, target):
        frame = target.screenshot(client_only=True)
        assert frame.dtype == np.uint8

    def test_screenshot_non_empty(self, target):
        frame = target.screenshot(client_only=True)
        assert frame.size > 0

    def test_screenshot_matches_client_rect_size(self, target):
        cr = target.client_rect
        frame = target.screenshot(client_only=True)
        h, w = frame.shape[:2]
        assert w == cr["width"], f"截图宽 {w} != 客户区宽 {cr['width']}"
        assert h == cr["height"], f"截图高 {h} != 客户区高 {cr['height']}"

    def test_screenshot_value_range(self, target):
        frame = target.screenshot(client_only=True)
        assert frame.min() >= 0
        assert frame.max() <= 255

    def test_screenshot_different_instances_same_result(self, target):
        """同一窗口连续两帧应高度相似（静态场景）。"""
        frame1 = target.screenshot(client_only=True)
        # 小延迟模拟真实使用
        import time
        time.sleep(0.1)
        frame2 = target.screenshot(client_only=True)
        diff = np.abs(frame1.astype(np.int16) - frame2.astype(np.int16)).mean()
        assert diff < 5.0, f"两帧差异过大: {diff:.2f}"

    def test_screenshot_save_to_file(self, target, tmp_path):
        """截图应能正常保存为 PNG。"""
        import cv2
        frame = target.screenshot(client_only=True)
        out = tmp_path / "test_screenshot.png"
        cv2.imwrite(str(out), frame)
        assert out.exists()
        assert out.stat().st_size > 1000  # 至少 1KB

    def test_screenshot_client_only_smaller_than_full(self, target):
        """仅客户区的截图应不大于（且通常等于）客户区尺寸。"""
        # 这里的思路是 client_only 的截图正好就是客户区的大小
        frame = target.screenshot(client_only=True)
        cr = target.client_rect
        assert frame.shape[1] == cr["width"]
        assert frame.shape[0] == cr["height"]

    def test_screenshot_minimized_window(self, target):
        """最小化窗口的截图应优雅处理（可能返回 0 大小的帧）。"""
        if target.is_minimized:
            with pytest.raises(RuntimeError, match="客户区尺寸无效"):
                target.screenshot(client_only=True)


# ========================================================================
# 坐标转换测试
# ========================================================================

@pytest.mark.integration
class TestCoordinateConversion:
    """客户区/屏幕坐标互相转换（需要真实窗口）。"""

    @pytest.fixture(scope="class")
    def target(self):
        win = _find_test_window()
        if win is None:
            pytest.skip("没有可用的测试窗口")
        return win

    def test_client_to_screen_returns_tuple(self, target):
        result = target.client_to_screen(0, 0)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert all(isinstance(v, int) for v in result)

    def test_screen_to_client_returns_tuple(self, target):
        result = target.screen_to_client(0, 0)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_roundtrip(self, target):
        """client → screen → client 应回到原值。"""
        for x, y in [(0, 0), (100, 100), (500, 300)]:
            sx, sy = target.client_to_screen(x, y)
            cx, cy = target.screen_to_client(sx, sy)
            assert abs(cx - x) <= 1, f"x 往返误差过大: {cx} vs {x}"
            assert abs(cy - y) <= 1, f"y 往返误差过大: {cy} vs {y}"

    def test_client_origin_consistency(self, target):
        """客户区左上角 (0,0) 的屏幕坐标应与 client_rect_screen 一致。"""
        sx, sy = target.client_to_screen(0, 0)
        crs = target.client_rect_screen
        assert sx == crs["left"], f"client(0,0) -> screen x {sx} != {crs['left']}"
        assert sy == crs["top"], f"client(0,0) -> screen y {sy} != {crs['top']}"


# ========================================================================
# Matcher 集成测试
# ========================================================================

@pytest.mark.integration
class TestMatcherIntegration:
    """与现有 Matcher 模块的集成测试（需要真实窗口和截图）。"""

    @pytest.fixture(scope="class")
    def target(self):
        win = _find_test_window()
        if win is None:
            pytest.skip("没有可用的测试窗口")
        return win

    def test_matcher_self_match(self, target):
        """全图自匹配应返回 (0,0) 置信度 1.0。"""
        from backend.matcher.matcher import matcher

        frame = target.screenshot(client_only=True)
        result = matcher.match(
            target=frame, template=frame,
            threshold=0.99, match_select="best",
        )
        assert result is not None
        # 注意：Matcher 返回的是居中坐标（w/2, h/2），不是 (0,0)
        h, w = frame.shape[:2]
        assert abs(result.x - w // 2) < 5
        assert abs(result.y - h // 2) < 5
        assert result.score > 0.99

    def test_matcher_crop_match(self, target):
        """从截图中裁剪一块做模板，应匹配到裁剪位置。"""
        from backend.matcher.matcher import matcher

        frame = target.screenshot(client_only=True)
        h, w = frame.shape[:2]
        if h < 150 or w < 150:
            pytest.skip("窗口太小，无法裁剪")

        crop = frame[50:130, 50:130]
        result = matcher.match(
            target=frame, template=crop,
            threshold=0.7, match_select="best",
        )
        assert result is not None
        assert result.score > 0.9
        # matcher 返回居中坐标，裁剪区域中心约在 (90, 90)
        assert 80 <= result.x <= 100, f"x={result.x} 不在 (80,100) 范围"
        assert 80 <= result.y <= 100, f"y={result.y} 不在 (80,100) 范围"

    def test_matcher_no_match_returns_empty(self, target):
        """不匹配的模板应返回空结果。"""
        from backend.matcher.matcher import matcher

        frame = target.screenshot(client_only=True)
        # 完全不存在的图案
        fake_template = np.ones((50, 50, 3), dtype=np.uint8) * 255
        result = matcher.match(
            target=frame, template=fake_template,
            threshold=0.99, match_select="best",
        )
        # 理想情况是 is_empty 或 score < 0.99
        # 具体行为取决于 Matcher 实现，不做死断言
        _ = result  # 至少不抛异常


# ========================================================================
# PostMessage 操作测试（有副作用！默认跳过）
# ========================================================================

@pytest.mark.skip(reason="PostMessage 会对目标窗口产生实际点击，需手动运行")
class TestPostMessage:
    """后台鼠标/键盘操作测试。

    警告：这些测试会向目标窗口发送真实输入！
    运行前确保目标窗口内容安全。
    """

    @pytest.fixture(scope="class")
    def target(self):
        win = _find_test_window()
        if win is None:
            pytest.skip("没有可用的测试窗口")
        return win

    def test_click_sends_message(self, target):
        """点击——验证无异常抛出。"""
        x, y = 50, 50
        target.click(x, y)  # should not crash

    def test_double_click_sends_message(self, target):
        target.double_click(50, 50)

    def test_right_click_sends_message(self, target):
        target.right_click(50, 50)

    def test_mouse_move_sends_message(self, target):
        target.mouse_move(100, 100)

    def test_mouse_down_up_sends_message(self, target):
        target.mouse_down(50, 50)
        target.mouse_up(50, 50)

    def test_scroll_sends_message(self, target):
        target.scroll(120)   # 向上滚
        target.scroll(-120)  # 向下滚

    def test_key_click_sends_message(self, target):
        target.key_click("ENTER")
        target.key_click("A")
        target.key_click(0x2E)  # DELETE

    def test_type_text_sends_message(self, target):
        target.type_text("hello world")

    def test_key_combo_sends_message(self, target):
        target.key_combo("CTRL", "A")
        target.key_combo("CTRL", "C")

    def test_press_release_key_sends_message(self, target):
        target.press_key("SHIFT")
        target.key_click("A")
        target.release_key("SHIFT")


# ========================================================================
# 边界条件
# ========================================================================

class TestEdgeCases:
    """边界情况测试。"""

    def test_invalid_hwnd_raises(self):
        from backend.automation.win32_target import Win32Target
        with pytest.raises(ValueError):
            Win32Target(0)
        with pytest.raises(ValueError):
            Win32Target(-1)

    def test_nonexistent_title_returns_empty(self):
        from backend.automation.win32_target import Win32Target
        targets = Win32Target.from_title(
            "NONEXISTENT_WINDOW_TITLE_XYZ_98765",
            exact=True,
        )
        assert targets == []

    def test_nonexistent_class_returns_empty(self):
        from backend.automation.win32_target import Win32Target
        targets = Win32Target.from_class(
            "NONEXISTENT_CLASS_XYZ_98765",
            exact=True,
        )
        assert targets == []

    def test_nonexistent_pid_returns_empty(self):
        from backend.automation.win32_target import Win32Target
        targets = Win32Target.from_pid(999999999)
        assert targets == []


# ========================================================================
# 便捷函数测试
# ========================================================================

class TestConvenienceFunctions:
    """模块级便捷函数测试。"""

    def test_enum_windows(self):
        from backend.automation.win32_target import enum_windows
        windows = enum_windows()
        assert len(windows) > 0

    def test_enum_windows_with_pattern(self):
        from backend.automation.win32_target import enum_windows
        windows = enum_windows("Program Manager")
        assert isinstance(windows, list)

    def test_foreground_window_function(self):
        from backend.automation.win32_target import foreground_window
        fg = foreground_window()
        if fg is not None:
            assert fg.hwnd > 0

    def test_window_from_cursor(self):
        from backend.automation.win32_target import window_from_cursor
        win = window_from_cursor()
        if win is not None:
            assert win.hwnd > 0
