# DeepSeek 接入与本地验收

## 已完成

2026-11-11 使用授权密钥连接官方 `/models` 接口，确认本账户可以访问 `deepseek-flash` 与 `deepseek-v4-pro`。本项目选择 Flash 的非思考模式，接入地址为 `https://api.deepseek.com/chat/completions`。密钥仅写入项目根目录 `.env`，未写入本文件或协作包。

当前实际链路：网页提交 → LangGraph 检索资料 → DeepSeek 选择待检查链路 → 本地执行 check_link → 汇总静态与运行证据 → DeepSeek 调用只读证据工具 → 工具返回回送模型 → 教学解释 → 保存会话、暂停或结束。

规划仅选择现有待检查链路的顺序，仍保证全部链路被检查。教学工具包括 inspect_link、inspect_verification、retrieve_reference；模型未成功读取工具时不采纳其回答。模型状态、工具参数和返回、每次请求耗时、token 用量可以在执行记录及保存的 JSON 中查看。

## 怎么使用

1. 本机双击 start.cmd，打开 http://127.0.0.1:8765 。已启动时直接访问。
2. 载入 R1—R2—R3 完整演示，勾选“使用已配置模型调用证据工具并补充教学解释”。
3. 开始诊断，查看当前故障、模型提示及实际读取的证据工具。
4. 载入下一阶段再复测，保持同一会话。三个阶段依次为区域故障、路由缺失、提交观测通过。
5. 提示等级分别控制方向、线索与修改建议。一级向解释模型隐藏原配置行和具体参数，不保证模型不会从一般知识推测答案。
6. 点击“保存记录到项目”，在 runtime/exports 查看 JSON 和 Markdown 报告。

启用模型会发送问题和相关工具证据至 DeepSeek。原始认证类证据行会省略。会话和报告保存在本机。协作包不包含本机密钥或验收数据库，队友在自己的 .env 配置已授权服务。

## 真实调用验收

在 LabAgent 文件夹运行：

```powershell
.venv\Scripts\python.exe -B scripts/verify_model.py
```

这项命令会调用外部模型并使用账户额度，不会在普通测试或网页启动时自动执行。测试一至三级提示、三阶段状态及同会话暂停恢复；要求每阶段都有实际模型选择、成功的证据工具调用与模型返回。不通过时停止后续阶段并保留已有记录。

每轮结果写入 `runtime/model-verification/<北京时间目录>/`：summary.json 为验收结果及调用统计，record.json 为完整过程，report.md 为可读报告。网络配置及运行输出为合成演示数据，模型请求和本地工具是真实执行；不代表真实网络准确率、教学效果或 AgentArts 云端运行。

## 失败处理与边界

接口单次默认超时45秒，最多四轮教学调用；响应默认最多800 token，无自动重试。timeout、authentication_failed、insufficient_balance、rate_limited 等失败类型会保留，本地诊断照常返回。错误响应正文、密钥和请求头不进入对外结果；重定向不会转发认证信息。返回过长或格式不完整时不作为有效解释。

目前专用 DeepSeek 路径只支持非思考模式，避免把私有推理内容写入或重放到会话中。切换其他模型时需重新验收其工具协议。配置与连通性分别对待，网页“已配置”只表示配置存在；每次诊断中的模型状态才说明该次调用是否成功。

后续重点是主动请求缺失证据、实际设备或仿真环境验证，以及 AgentArts 平台接入。暂未实现设备自动采集或自动修改配置。

## 官方参考

- [首次调用与接口地址](https://api-docs.deepseek.com/)
- [工具调用流程](https://api-docs.deepseek.com/guides/tool_calls/)
- [Chat Completions 参数](https://api-docs.deepseek.com/api/create-chat-completion/)
