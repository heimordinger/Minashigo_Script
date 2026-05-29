"""
加载动画模块
简单稳定的独立进程实现
"""
import subprocess
import sys
import os
import json
import tempfile


class LoadingAnimation:
    def __init__(self):
        self.process = None
        self.control_file = "loading_control.json"
        self.is_running = False
        self._script_path = None

    def _create_animation_script(self):
        """创建独立进程的动画脚本"""
        script = '''
import tkinter as tk
from PIL import Image, ImageTk
import json
import time
import os

class LoadingWindow:
    def __init__(self):
        self.root = None
        self.label = None
        self.frames = []
        self.current_frame = 0
        self.is_running = True
        self.control_file = "loading_control.json"
        self.topmost = self._load_topmost_setting()

    def _load_topmost_setting(self):
        """加载置顶设置"""
        try:
            config_file = "config/config.json"
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    loading_cfg = config.get("loading", {})
                    return loading_cfg.get("topmost", True)
            return True
        except Exception as e:
            print(f"[Loading Window] 读取置顶设置失败: {e}")
            return True

    def load_gif_frames(self, gif_path):
        """预解码GIF帧"""
        try:
            with Image.open(gif_path) as img:
                window_width, window_height = img.size

                screen_width = self.root.winfo_screenwidth()
                screen_height = self.root.winfo_screenheight()
                x = (screen_width - window_width) // 2
                y = (screen_height - window_height) // 2
                self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")

                for i in range(img.n_frames):
                    img.seek(i)
                    frame = ImageTk.PhotoImage(img.copy())
                    self.frames.append(frame)

                print(f"[Loading Window] 预解码完成: {len(self.frames)} 帧")

                self.label = tk.Label(self.root, bg='black', bd=0, highlightthickness=0)
                self.label.pack()

                return True
        except Exception as e:
            print(f"[Loading Window] GIF预解码失败: {e}")
            return False

    def update_animation(self):
        """定时播放动画"""
        if not self.is_running or not self.frames:
            return

        try:
            self.label.config(image=self.frames[self.current_frame])
            self.current_frame = (self.current_frame + 1) % len(self.frames)
            self.root.after(33, self.update_animation)
        except Exception as e:
            print(f"[Loading Window] 动画更新失败: {e}")

    def check_control(self):
        """检查控制文件"""
        try:
            if os.path.exists(self.control_file):
                with open(self.control_file, 'r') as f:
                    data = json.load(f)
                    if data.get('stop', False):
                        self.is_running = False
                        return False
        except:
            pass
        return True

    def run(self):
        """运行加载窗口"""
        try:
            self.root = tk.Tk()
            self.root.overrideredirect(True)

            if self.topmost:
                self.root.attributes('-topmost', True)
                print("[Loading Window] 置顶模式已开启")
            else:
                print("[Loading Window] 置顶模式已关闭")

            self.root.attributes('-transparentcolor', 'black')
            self.root.configure(bg='black')

            gif_path = "icon/loading.gif"
            if not os.path.exists(gif_path):
                print(f"[Loading Window] GIF文件不存在: {gif_path}")
                self.label = tk.Label(self.root, bg='black')
                self.label.pack(expand=True, fill="both")
            else:
                print(f"[Loading Window] 开始预解码GIF: {gif_path}")
                if not self.load_gif_frames(gif_path):
                    self.label = tk.Label(self.root, bg='black')
                    self.label.pack(expand=True, fill="both")

            self.root.update()
            print("[Loading Window] 窗口已显示")

            if self.frames:
                self.update_animation()
                print("[Loading Window] 定时播放开始 (30 FPS)")

            while self.is_running:
                try:
                    self.root.update()
                    time.sleep(0.01)
                    if not self.check_control():
                        break
                except Exception as e:
                    print(f"[Loading Window] 主循环错误: {e}")
                    break

            print("[Loading Window] 正在关闭...")
            self.root.destroy()

        except Exception as e:
            print(f"[Loading Window] 运行失败: {e}")

        try:
            if os.path.exists(self.control_file):
                os.remove(self.control_file)
        except:
            pass

if __name__ == "__main__":
    window = LoadingWindow()
    window.run()
'''
        return script

    def _find_python(self):
        """找一个可用的 Python 解释器（打包后 sys.executable 是 exe，不能用）"""
        if not getattr(sys, 'frozen', False):
            return sys.executable
        candidates = [
            os.path.join(os.path.dirname(sys.executable), "python", "python.exe"),
            os.path.join(os.path.dirname(sys.executable), "python.exe"),
            "python",
            "python3",
        ]
        for c in candidates:
            try:
                subprocess.run([c, "--version"], capture_output=True, timeout=2)
                return c
            except Exception:
                continue
        return None

    def start(self):
        """启动加载动画"""
        print("[Loading] 启动独立进程加载动画...")

        with open(self.control_file, 'w') as f:
            json.dump({'stop': False}, f)

        script = self._create_animation_script()

        python_exe = self._find_python()
        if python_exe is None:
            print("[Loading] 找不到 Python 解释器，跳过加载动画")
            return

        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8')
        tmp.write(script)
        self._script_path = tmp.name
        tmp.close()

        try:
            # 清除 PyInstaller 的 Tcl/Tk 环境变量，防止被子进程继承导致版本冲突
            clean_env = os.environ.copy()
            for key in ("TCL_LIBRARY", "TK_LIBRARY", "TKPATH"):
                clean_env.pop(key, None)

            self.process = subprocess.Popen(
                [python_exe, self._script_path],
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                env=clean_env,
            )
            self.is_running = True
            print("[Loading] 独立进程已启动")
        except Exception as e:
            print(f"[Loading] 启动进程失败: {e}")

    def stop(self):
        """停止加载动画"""
        if not self.is_running:
            return

        print("[Main] 停止加载动画...")

        try:
            with open(self.control_file, 'w') as f:
                json.dump({'stop': True}, f)
            print("[Main] 已发送停止信号")
        except Exception as e:
            print(f"[Main] 发送信号失败: {e}")

        if self.process:
            try:
                self.process.wait(timeout=3.0)
                print("[Main] 进程已结束")
            except subprocess.TimeoutExpired:
                print("[Main] 强制终止进程")
                self.process.terminate()
                try:
                    self.process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.process.kill()

        if self._script_path:
            try:
                os.unlink(self._script_path)
            except Exception:
                pass

        try:
            if os.path.exists(self.control_file):
                os.remove(self.control_file)
        except:
            pass

        self.is_running = False
        print("[Main] 加载动画已停止")
