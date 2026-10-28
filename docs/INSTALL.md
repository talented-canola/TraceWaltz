# 安装

推荐 Ubuntu 24.04、Python 3.12+ 和可用的 Docker Engine / Docker Desktop。WSL 中建议将检出目录放在 Linux 文件系统，减少大量依赖文件经 Windows 挂载目录访问的开销。

## 1. 准备项目内运行环境

先确认 `git`、`curl`、`xz`、`python3`、Python venv 和 `apt download` 可用。安装脚本在项目内下载固定版本的运行环境、Agent 依赖及便携工具，不安装系统 Docker，也不调用模型。

```bash
bash tools/setup-linux.sh
bash tools/prepare-linux-review.sh
bash tools/setup-hitch.sh
```

任务生产组件会检出 `dependencies.lock.json` 指定的上游版本，并应用 `patches/evotrace/integration.patch`。审核 DSH 与编码 DSH 分开安装；`config/review-package.json` 固定审核组件版本。审核安装结束后，会将受管上游源码的 `node_modules` 连接到独立审核依赖，使其插件能够解析对应版本；已有依赖目录不会被覆盖。

若依赖下载源已经不再提供某个固定版本，先保存错误并调整安装来源；不要直接升级编码或审核 DSH 后复用旧评测成绩。

## 2. 配置 Docker

连接本机已有 Docker。例如标准 Linux Docker socket：

```bash
export DOCKER_HOST=unix:///var/run/docker.sock
docker info
```

项目默认 socket 是 `unix:///tmp/tracewaltz-docker.sock`，用于独立运行环境。使用现有 Docker 时在每个运行 shell 中显式设置 `DOCKER_HOST`；写入 `.env` 的模型配置不会自动导出 Docker 环境变量。

准备构建基底及评测需要的 Python 镜像：

```bash
docker build -t tracewaltz-node22-builder:integration harness/container-builder
docker pull python:3.11-slim
```

Hitch 使用项目 `.state/hitch` 管理后台状态。真实任务运行时，编码环境与独立验证环境由其管理，不用本地目录复制替代容器隔离。

## 3. 配置自己的模型连接

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
DEEPSEEK_API_KEY=your-own-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=your-available-model
```

也可以直接设置同名环境变量，或使用 `TRACEWALTZ_ENV_FILE` 指定文件。环境变量优先于文件。真实密钥只供计量网关读取，DSH 和容器使用网关令牌。

## 4. 初始化与检查

```bash
bash tools/linux-env.sh .runtime/linux-python/bin/tracewaltz init
bash tools/linux-env.sh .runtime/linux-python/bin/tracewaltz doctor
```

默认配置在 `.state/bridge.json`。`doctor` 检查基本工具和运行入口；容器与真实模型仍通过后续执行验证。账本在 `.state/api/requests.sqlite`，初始调用上限为 3000，失败和响应未知也保留计数。修改已建立上限须通过显式授权入口，不通过删除数据库或重启绕过。

网关默认监听容器可达地址。使用不同 Docker 网络时，核对 `.state/bridge.json` 的 `api.bind_host`、`advertised_host`、`port`；容器需要能访问该地址。

首次完整安装仍需在目标机器上验收。已有开发环境的组件测试不代表任意平台都已完成安装测试。
