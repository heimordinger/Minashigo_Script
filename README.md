# Minashigo Script

基于图像识别的页游自动化框架，支持浏览器和桌面窗口双模式控制。内置 AI Script Generator，可根据自然语言描述自动生成自动化脚本。

## 功能

- **🤖 AI 脚本生成**：配置 LLM API（Claude / GPT / DeepSeek），写一段文字描述 + 提供截图，自动生成可执行的 Python 脚本
- **🎮 双模式控制**：浏览器自动化（Playwright）或桌面窗口控制（Win32 API）
- **🔍 图像匹配**：OpenCV 多尺度模板匹配 + ORB 特征匹配，支持多实例贪婪匹配
- **📜 自研 FSM 脚本引擎**：有限状态机 + GlobalGuard 守卫模式，状态独立、弹窗统一处理
- **🔧 可视化工作流**：内置 TaskFlow 节点编辑器，无需编程即可编排流程
- **🛡️ 看门狗保护**：5 分钟无操作自动终止，防止脚本卡死
- **🔔 托盘通知**：任务完成/异常时系统托盘弹窗提醒
- **👥 多账号管理**：多账号独立配置、独立浏览器实例

## 快速开始

### 方式一：下载 Releases（推荐）

从 [Releases](https://github.com/heimordinger/Minashigo_Script/releases) 下载最新 zip，解压后运行 `Minashigo_Script.exe`。

### 方式二：源码运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 安装 Playwright 浏览器
playwright install chromium

# 3. 运行
python main.py
```

## 使用

### 账号管理
1. 启动后进入「账号管理」tab 添加账号
2. 切回「开始」tab 选择账号并点击「开始」

### 脚本目标选择
- **浏览器模式**：点击「启动浏览器」→ 自动打开控制表盘
- **窗口模式**：点击「选择窗口」→ 选取已打开的桌面窗口

### TaskFlow 工作流
1. 启动后在账号界面点击「TaskFlow」按钮
2. 在可视化编辑器中拖拽节点编排流程图
3. 支持：点击、图片匹配、条件判断、循环、计数器等

### AI 脚本生成（Script Generator）
1. 进入「脚本生成」tab
2. 选择 AI 提供商并填写 API Key
3. 编写或加载脚本描述（`.txt`）
4. 提供参考截图（可选）
5. 点击「生成脚本」→ AI 自动生成完整脚本
6. 支持流式输出（逐字显示）、token 消耗统计、一键保存

## 项目结构

```
Minashigo_Script/
├── main.py                  # 入口
├── core/                    # 核心框架
│   ├── path.py              # 路径管理
│   ├── app_startup.py       # 启动流程
│   ├── taskflow_manager.py  # TaskFlow 管理器
│   └── http_api_server.py   # HTTP API 服务器
├── controller/              # 控制层
│   ├── ctrl.py              # 主控制器
│   └── task_controller.py   # 任务控制器
├── gui/                     # 图形界面
│   ├── window/              # 主窗口
│   ├── panels/              # 面板组件
│   └── tabs/                # Tab 页面
├── backend/                 # 后端
│   ├── browser/             # Playwright 浏览器控制
│   ├── automation/          # Win32 窗口控制
│   └── matcher/             # 图像匹配引擎
├── taskflow/                # 可视化工作流编辑器
    └── matcher/             # 图像匹配引擎
    └── script_generator/    # AI 脚本生成（agent + config.json）
│   └── nodes/               # 节点定义
├── scripts/                 # 用户脚本
├── assets/                  # 图片资源
└── models/                  # OCR 模型（自动下载）
```

## 构建

```bash
# 打包 exe
python build.py

# 打包发布 zip
python compress_zip.py
```

## 技术栈

- **GUI**: PySide6 (Qt)
- **浏览器自动化**: Playwright
- **桌面控制**: Win32 API (ctypes)
- **图像匹配**: OpenCV（多尺度模板匹配、ORB 特征匹配、多实例贪婪匹配）
- **AI 提供商**: Anthropic Claude / OpenAI / DeepSeek / Google Gemini / Groq
- **OCR**: Tesseract
- **工作流**: LiteGraph（自建 TaskFlow 编辑器）
- **凭据存储**: keyring（系统级加密）

## 许可证

[LICENSE](LICENSE)
