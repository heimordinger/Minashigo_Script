# main.py
import sys
import os
import tempfile
import time


def is_already_running():
    """使用锁文件检查程序是否已经在运行"""
    try:
        # 创建临时锁文件
        lock_file = os.path.join(tempfile.gettempdir(), "minashigo_script.lock")

        if os.path.exists(lock_file):
            try:
                with open(lock_file, 'r') as f:
                    pid = int(f.read().strip())

                if os.name == 'nt':  # Windows
                    import subprocess
                    result = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'],
                                        capture_output=True, text=True)
                    if str(pid) in result.stdout:
                        return True
                else:
                    try:
                        os.kill(pid, 0)
                        return True
                    except OSError:
                        pass

                os.remove(lock_file)
            except (ValueError, FileNotFoundError):
                try:
                    os.remove(lock_file)
                except:
                    pass

        with open(lock_file, 'w') as f:
            f.write(str(os.getpid()))

        return False

    except Exception as e:
        print(f"[Main] 检查运行状态失败: {e}")
        return False


def main():
    _t0 = time.time()
    def ts(msg):
        print(f"[{time.time()-_t0:7.3f}] {msg}")

    if is_already_running():
        print("[Main] 程序已经在运行中，退出...")
        input("按任意键退出...")
        return

    ts("启动主程序...")


    from core.loading_animation import LoadingAnimation
    loading = LoadingAnimation()
    loading.start()

    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    from core.path import ICON_PATH
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MinashigoScript.1.0")
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(ICON_PATH)))

    from core.app_startup import AppStartup
    startup = AppStartup(t0=_t0)
    startup.schedule_init(loading=loading)

    ts("进入事件循环")
    app.exec()


if __name__ == '__main__':
    main()
