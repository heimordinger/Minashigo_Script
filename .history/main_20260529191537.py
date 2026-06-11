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
            # 检查锁文件是否仍然有效
            try:
                # 尝试读取锁文件中的进程ID
                with open(lock_file, 'r') as f:
                    pid = int(f.read().strip())
                
                # 检查该进程是否还在运行
                if os.name == 'nt':  # Windows
                    import subprocess
                    result = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'], 
                                        capture_output=True, text=True)
                    if str(pid) in result.stdout:
                        return True
                else:  # Linux/Mac
                    try:
                        os.kill(pid, 0)  # 发送信号0检查进程是否存在
                        return True
                    except OSError:
                        pass
                
                # 进程不存在，删除锁文件
                os.remove(lock_file)
            except (ValueError, FileNotFoundError):
                # 锁文件损坏，删除它
                try:
                    os.remove(lock_file)
                except:
                    pass
        
        # 创建新的锁文件
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

    # 检查是否已经在运行
    if is_already_running():
        print("[Main] 程序已经在运行中，退出...")
        input("按任意键退出...")
        return

    ts("启动主程序...")

    from core.loading_animation import LoadingAnimation
    loading = LoadingAnimation()
    loading.start()

    from core.app_startup import AppStartup
    app_startup = AppStartup(t0=_t0)

    ts("开始加载资源")
    app_startup.load_resources()          # 加载模型资源
    ts("加载配置")
    app_startup.load_config()            # 加载配置
    ts("设置端口")
    app_startup.setup_ports()             # 设置端口
    ts("设置控制器")
    app_startup.setup_controller()        # 设置控制器
    ts("设置外观层")
    app_startup.setup_facade()           # 设置外观层
    ts("设置GUI")
    app_startup.setup_gui()              # 设置GUI

    app_startup.setup_quit_handler(loading)  # 退出加载动画
    app_startup.show_main_window(loading)     # 显示主窗口

    ts("启动TaskFlow后台")
    app_startup.start_taskflow_background()  # 主窗口显示后异步启动TaskFlow

    ts("进入事件循环")
    app_startup.run()


if __name__ == '__main__':
    main()
