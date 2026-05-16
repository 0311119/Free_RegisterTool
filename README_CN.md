# Free Register Tool

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/Node.js-18%2B-339933?style=for-the-badge&logo=node.js&logoColor=white" alt="Node.js 18+" />
  <img src="https://img.shields.io/badge/FastAPI-后端-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/React-前端-61DAFB?style=for-the-badge&logo=react&logoColor=0A0A0A" alt="React" />
  <img src="https://img.shields.io/badge/License-MIT-F7C948?style=for-the-badge" alt="MIT License" />
</p>

<p align="center">
  <strong>一个可本地部署的账号注册与运维工作台</strong><br />
  集成 FastAPI 后端、React 管理界面、插件化平台接入、邮箱适配、代理管理、定时任务与外部同步能力。
</p>

<p align="center">
  <a href="README.md">English README</a>
</p>

---

## 项目简介

Free Register Tool 适合本地部署、受控自动化、开发调试和研究用途。  
它把后端 API、管理后台、平台插件、邮箱适配器、代理配置、日志与任务流放到同一个工作空间里，方便统一管理。

请仅在符合平台条款、当地法律和你自身风险控制要求的前提下使用。

## 界面预览

### 仪表盘

![Dashboard](docs/images/dashboard.png)

### 配置中心

![Settings Overview](docs/images/settings-overview.png)

## 核心功能

- ◆ 基于 `platforms/` 的多平台插件架构
- ◆ 账号、任务、配置、日志、代理统一管理的 Web 后台
- ◆ 批量注册任务与实时进度跟踪
- ◆ 临时邮箱与自建邮箱的统一适配层
- ◆ 支持验证码处理与浏览器自动化流程
- ◆ 面向 ChatGPT 的 Token 管理、本地探测、Plus 长链获取、Sub2API 同步
- ◆ 定时任务与周期性执行
- ◆ 外部系统同步与扩展接入能力

## 本轮文档与界面改进

- ◆ 管理后台整体改为更亮、更紧凑的风格
- ◆ 账号页操作更集中，详情改为抽屉式展示
- ◆ ChatGPT 配置项更成组，路径更短
- ◆ 任务历史页和定时任务页的交互更清晰
- ◆ 发布前默认加强隐私文件与调试文件的忽略规则

## 技术栈

| 层级 | 技术 |
|---|---|
| 后端 | FastAPI、Uvicorn、SQLModel、APScheduler |
| 前端 | React、TypeScript、Vite、Ant Design |
| 自动化 | Playwright、Camoufox |
| 网络 | `curl_cffi`、`httpx` |
| 存储 | SQLite |

## 目录结构

```text
api/          HTTP API 路由
core/         共享运行时、注册表、调度器、数据库辅助逻辑
frontend/     React 前端
platforms/    平台插件与平台专属逻辑
services/     后台服务与外部集成辅助逻辑
scripts/      项目工具脚本
tests/        自动化测试
tools/        运维辅助工具
docker/       容器入口与部署资源
main.py       后端入口
```

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/0311119/Free_RegisterTool.git
cd Free_RegisterTool
```

### 2. 创建 Python 环境

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.venv\Scripts\Activate.ps1
```

macOS / Linux：

```bash
source .venv/bin/activate
```

### 3. 安装后端依赖

```bash
pip install -r requirements.txt
```

### 4. 安装浏览器自动化依赖

```bash
python -m playwright install chromium
python -m camoufox fetch
```

### 5. 安装前端依赖

```bash
cd frontend
npm install
npm run build
cd ..
```

### 6. 创建本地配置

```bash
cp .env.example .env
```

然后根据你的本地环境、邮箱服务、验证码服务和外部集成需求修改 `.env`。

### 7. 启动后端

```bash
python main.py
```

默认 API 文档地址：

```text
http://localhost:8000/docs
```

## 前端开发

```bash
cd frontend
npm install
npm run dev
```

默认开发地址：

```text
http://localhost:5173
```

## 主要页面说明

- `Dashboard`  
  查看账号总数、平台分布和整体状态概览。

- `Accounts`  
  管理账号列表、详情抽屉、批量操作、状态同步、上传动作和支付长链。

- `Register Task`  
  发起批量注册任务，查看实时进度和日志。

- `Scheduled Tasks`  
  配置周期性注册任务和执行控制。

- `Task History`  
  搜索历史任务日志、筛选结果并清理记录。

- `Settings`  
  集中配置邮箱、验证码、代理、外部同步和平台专项参数。

## 配置说明

项目会同时读取 `.env` 和应用持久化配置。

常见配置类别包括：

- 服务监听地址与端口
- 验证码服务配置
- 代理配置
- 邮箱服务凭据
- Sub2API 等外部同步配置
- 平台专属运行参数

建议从 [`.env.example`](./.env.example) 开始。

## 测试

```bash
pytest tests/
```

部分仓库脚本属于运维辅助工具，不适合作为可重复测试。日常验证建议优先使用 `tests/` 下的自动化测试。

## 隐私与安全发布说明

本仓库已经尽量把本地状态、敏感凭据和调试残留排除在版本控制之外。

默认忽略的本地内容包括：

- `.env`
- `data/`
- `logs/`
- `runtime/`
- `static/`
- `*.db`、`*.sqlite`、`*.sqlite3`
- Gmail OAuth 文件、临时 Token、调试截图、日志输出
- 本地机器专用脚本和临时运维文件

如果你要发布自己的 fork，请再次确认不要提交：

- API Key
- 邮箱服务账号与密码
- OAuth Token 导出文件
- 后端日志
- 含真实账号信息的截图
- 带本地路径、端口、内网地址的界面截图

## Docker

基础 Docker 用法：

```bash
docker-compose up -d
docker-compose logs -f
docker-compose down
```

## 开发说明

- 新平台逻辑放在 `platforms/`
- 通用抽象放在 `core/`
- API 路由放在 `api/`
- 前端页面放在 `frontend/src/pages/`
- UI 基础组件放在 `frontend/src/components/` 和 `frontend/src/components/ui/`

## 许可证

MIT。详见 [LICENSE](./LICENSE) 和 [NOTICE](./NOTICE)。

## 致谢

本仓库建立在同生态里较早的上游和分支项目基础上，并在此基础上继续扩展和整理。  
具体致谢与历史信息请参考仓库提交记录和相关说明文件。
