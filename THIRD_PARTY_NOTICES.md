# 第三方组件与许可

本项目的源码不内嵌下面的 Python 库：它们由 `安装依赖.command` 或 `pip install -r requirements-community.txt` 从 PyPI 安装，各自遵循自己的许可证。列出的许可信息来自各库发布的元数据，以各库官方发布为准。

## 直接依赖（requirements-community.txt）

| 组件 | 许可证 | 用途 |
| --- | --- | --- |
| PyMuPDF | AGPL-3.0（或 Artifex 商业许可） | PDF 读取、渲染、页面截取 |
| ReportLab | BSD | 生成来源 PDF |
| python-docx | MIT | 读取和生成 Word 教案 |
| python-pptx | MIT | 检查 PPTX 课件 |
| Pillow | MIT-CMU（HPND） | 图片处理 |
| rapidocr-onnxruntime | Apache-2.0 | 教材图片文字识别 |
| Playwright | Apache-2.0 | 在 Chrome 中自动操作 Gemini Notebook |

## 主要间接依赖

| 组件 | 许可证 |
| --- | --- |
| onnxruntime | MIT |
| opencv-python | Apache-2.0 |
| NumPy | BSD-3-Clause 等 |
| Shapely | BSD-3-Clause |
| pyclipper | MIT |
| PyYAML | MIT |
| lxml | BSD-3-Clause |
| greenlet | MIT AND PSF-2.0 |
| pyee | MIT |
| protobuf | BSD-3-Clause |
| flatbuffers | Apache-2.0 |
| XlsxWriter | BSD-2-Clause |
| typing_extensions | PSF-2.0 |
| six | MIT |

## 随项目发布的字体

| 字体 | 许可证 | 文件 |
| --- | --- | --- |
| Long Cang（龙藏体） | SIL Open Font License 1.1 | `web/fonts/LongCang.ttf`，许可全文 `web/fonts/OFL-longcang.txt` |
| ZCOOL KuaiLe（站酷快乐体） | SIL Open Font License 1.1 | `web/fonts/ZCOOLKuaiLe.ttf`，许可全文 `web/fonts/OFL-zcool.txt` |

界面中的其他字体（如 Chalkduster、Marker Felt、Menlo、苹方）由 macOS 系统提供，不随本项目发布。

## 外部服务

DeepSeek、Gemini Notebook（Google）、Canva 是第三方服务，由使用者用自己的账号接入，遵循各自的服务条款。本项目不附带这些服务的任何使用权或额度。
