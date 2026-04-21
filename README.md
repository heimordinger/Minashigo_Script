# Minashigo_Script

基于 Python 的自动化执行框架，提供浏览器控制、图像匹配识别、OCR 识别以及任务调度能力，主要用于流程化脚本执行与自动化操作开发。

---

## 功能组成

- 图像模板匹配（OpenCV）
- 浏览器自动化控制（自定义 Browser 封装）
- OCR 文字识别（Tesseract / 自定义 OCR Engine）
- 任务调度与状态管理（Controller）
- GUI 界面（PySide6）
- 多模块解耦结构，支持功能扩展
- 模型资源通过 GitHub Releases 管理与分发

---

## 项目结构

- backend/ 浏览器控制、OCR、图像匹配等核心模块
- core/ 配置管理、路径管理、状态系统
- controller/ 任务执行与调度控制
- gui/ PySide6 图形界面
- models/ 模型资源
- scripts/ 业务脚本与自动化流程
- assets/ 图片资源与模板


---

