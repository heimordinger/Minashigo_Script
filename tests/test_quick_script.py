"""
快速脚本 —— 录制测试
运行后会在桌面上任意点击，每次点击会自动截取按钮模板并记录步骤。
按 ESC 停止录制，查看录制的步骤列表。
"""
import sys
import time
from pathlib import Path
import numpy as np

# 添加项目根路径
root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from backend.quick_script.recorder import QuickScriptRecorder


def test_with_image():
    """不依赖鼠标的测试：用现有截图模拟点击检测。"""
    print("=" * 50)
    print("快速脚本 —— 元素检测测试")
    print("=" * 50)

    # 找一张完整的游戏截图（跳过按钮级小图）
    import cv2
    img_dir = Path(root) / "assets" / "images"
    # DeepOne 登录页截图作为测试目标（够大，有按钮）
    test_candidates = [
        img_dir / "DeepOne" / "DO登录" / "start1.png",
        img_dir / "minashigo" / "孤儿登录" / "start1.png",
    ]
    test_img = None
    for c in test_candidates:
        if c.exists():
            test_img = c
            break
    if test_img is None:
        print("未找到测试截图文件")
        return
    data = np.fromfile(str(test_img), dtype=np.uint8)
    frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if frame is None:
        print("无法读取截图")
        return

    h, w = frame.shape[:2]
    print(f"截图尺寸: {w}×{h}")

    # 在截图各处测试若干点
    test_points = [
        (w // 2, h // 2),       # 中心
        (w // 3, h // 3),
        (w * 2 // 3, h // 2),
        (w // 2, h * 3 // 4),
    ]
    for px, py in test_points:
        elem = QuickScriptRecorder._detect_element(frame, px, py)
        if elem:
            print(f"  点({px},{py}) → 元素 ({elem['x']},{elem['y']}) {elem['w']}×{elem['h']}")
        else:
            print(f"  点({px},{py}) → 未检测到元素")

    print("\n元素检测测试完成!")


def test_recording():
    """测试录制流程（需要在有显示器的环境下运行）。"""
    print("=" * 50)
    print("快速脚本 —— 录制测试")
    print("=" * 50)
    print("\n启动录制，请用鼠标左键在桌面上点击...")
    print("按 Ctrl+C 停止录制\n")

    output_dir = root / "screenshots" / "quick_script_test"
    recorder = QuickScriptRecorder(output_dir)
    recorder.start()

    try:
        # 录制 10 秒，期间点击会被捕获
        for _ in range(100):
            if not recorder.is_running:
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        recorder.stop()

    print(f"\n共录制 {len(recorder.steps)} 步:")
    for i, step in enumerate(recorder.steps, 1):
        print(f"  {i}. {step.action} → {step.template_name} (坐标: {step.x},{step.y})")

    # 导出测试
    if recorder.steps:
        output_file = output_dir / "test_workflow.json"
        recorder.export_to_taskflow(str(output_file))


if __name__ == "__main__":
    if "--image" in sys.argv:
        test_with_image()
    elif "--record" in sys.argv:
        test_recording()
    else:
        print("用法:")
        print("  python tests/test_quick_script.py --image    # 测试元素检测")
        print("  python tests/test_quick_script.py --record   # 测试录制")
