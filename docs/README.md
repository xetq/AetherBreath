# docs — 设计与记录

| 文档 | 内容 |
|---|---|
| [`MCP设计.md`](MCP设计.md) | MCP 集成设计（v2 station 形态：一个 server = 一个文件夹；注册表只报"有什么"，工具 schema 按需取） |
| [`MCP-GitHub接入.md`](MCP-GitHub接入.md) | GitHub MCP server 从零接入教程（装二进制 → 申请 token → 配置 → 真机验证） |
| [`审批器-规格与链路-v1截止20260910.md`](审批器-规格与链路-v1截止20260910.md) | 审批引擎规格与端到端链路（v1 截止快照） |
| [`审批器-重构v2-动词目标绑定与后果分区.md`](审批器-重构v2-动词目标绑定与后果分区.md) | 审批引擎重构 v2：动词-目标绑定、后果分区 |
| [`审批器-ponytail审计-20260910.md`](审批器-ponytail审计-20260910.md) | 审批引擎审计报告（最小化 / 冗余 / 边界视角，含 P0 漏判项） |
| [`固定测试用例.txt`](固定测试用例.txt) | 每次加功能或修 bug 后可跑的人工验证清单（提示词注入 / 编排器 / 知识库…） |
| [`待优化项目.txt`](待优化项目.txt) | 项目 TODO 与路线图 |
| [`建设说明.txt`](建设说明.txt) | 项目缘起与设计愿景（为什么做"由用户定义的垂直型 agent"） |
| [`AB自维护文档/`](AB自维护文档/) | agent 自己维护自己产生的记录，含 [`严重事故记录/`](AB自维护文档/严重事故记录/) |

## 其它工程文档

| 文档 | 位置 |
|---|---|
| 项目总览与快速开始 | 仓库根 [`README.md`](../README.md) |
| WebUI 使用文档（启动/停止/开发模式/环境变量） | [`agent_webui/README.md`](../agent_webui/README.md) |
| WebUI 自检与验收报告 | [`agent_webui/ACCEPTANCE.md`](../agent_webui/ACCEPTANCE.md) |
| 回归测试集：跑法与覆盖地图 | [`tests/README.md`](../tests/README.md) |
| 审批规范清单 | [`agent/approvals/README.md`](../agent/approvals/README.md) |
| 技能库用法 | [`agent_skills/README.md`](../agent_skills/README.md) |
| MCP 服务站清单与用法 | [`agent_MCP/README.md`](../agent_MCP/README.md) |
