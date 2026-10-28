# 上游来源与复用

| 项目 | 使用部分 | 来源 |
| --- | --- | --- |
| EvoTrace | 原会话整理、四角色审核、任务恢复与构建、验证、标定、加难、经验处理 | https://github.com/jinzijian/EvoTrace |
| DeepSeek Harness | 编码和改进 Agent 的模型／工具循环、插件、配置与 Skills | https://github.com/deepseek-ai/deepseek-harness |
| Gear | 原生搜索、候选快照、评测调度、留出和采用规则 | https://github.com/rsi-gear/gear |
| Hitch | 执行环境、容器与评测证据管理 | https://github.com/rsi-gear/agent-hitch |
| Harbor | 任务环境和验证协议 | https://github.com/laude-institute/harbor |

版本由 `dependencies.lock.json` 固定。安装时取得各上游项目，保留各自许可证和声明。

`patches/evotrace/integration.patch` 是针对固定 EvoTrace 版本的本地集成改动，包含公开集成入口、来源与状态约束、记录读取及恢复等修改。它不包含完整上游仓库；相邻 `LICENSE` 为 EvoTrace 原 Apache-2.0 许可证。其余上游源码通过安装脚本获取。

TraceWaltz 集成层负责数据转换、公开与私有材料的分发、完整 DSH Meta 进程连接、运行状态对应和配置发布；不把上游完整能力描述为本项目原创。
