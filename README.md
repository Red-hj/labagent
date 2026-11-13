# LabAgent 实验诊断工具

当前版本 v0.11.0，默认页面只需要用户提供诊断材料。上传文件或粘贴内容后直接开始分析，问题描述可以留空。分析后可以继续追问原因、获取修改步骤、直接补充请求中的材料，再核对修改与测试结果。

已有两类后端能力：

- **OSPF 规则检查**：解析华为 VRP 配置，执行明确的链路与运行检查，提供分级提示、修改复测和实验报告。
- **开放网络排查**：模型自行读取配置、终端信息与日志，比较原始材料，提出带原文引用的故障假设，再请求下一步证据。无需预先指定故障类别，也不要求 OSPF 拓扑。

DeepSeek Flash 与本地 LangGraph 已接通。开放模式已经用 DHCP/终端访问合成材料完成真实模型交互：发现网关疑点、给出修改步骤、补充新材料、核对复测记录并结束本轮，随后仍可追问实验原理。

## 启动与体验

双击 `setup.cmd` 安装依赖，再双击 `start.cmd`，浏览器打开 http://127.0.0.1:8765 。本机已经安装依赖并配置模型；协作包中的密钥由队友自行填写。

1. 点击“添加诊断材料”，上传配置、日志、终端信息或实验说明，也可以直接粘贴内容。
2. 点击“开始诊断”。模型已由项目统一配置，用户无需选择模式或填写内部格式。
3. 用“解释一下原因”“给我修改步骤”或下方对话框继续交流，保留本次实验上下文。追问框按Enter发送，Shift+Enter换行。
4. 在补充请求下直接粘贴输出，或在左侧上传新文件/更新同名文件，再继续排查。
5. 点击“核对修改结果”，查看材料前后差异和上传文本中的Ping收发统计。目标对应的提交观测通过后，本轮排查结束；仍可继续问原理或保存整个实验过程报告。

材料导入会显示添加/更新数量。按Enter发送追问后输入框立即清空，消息进入对话；等待区实时显示材料读取、资料检索、前后比较、Ping核对和结论保存等实际执行进展。失败时恢复草稿并提供“重试这次请求”。回复采用直接的技术说明，正文解释关键参数，行号与原文在“查看材料依据”中展开。

追问中的配置、原始输出和恢复反馈也参与本轮排查。明确反馈目标已恢复时，状态显示“用户反馈已恢复”并结束当前排查；补充Ping原始输出、经工具核对后显示“测试输出核验通过”。概念追问保留完成状态，报告再次失败时重新排查。报告跟随最终状态生成，保留具体实验目标、恢复依据、修改过程与原文引用。

连接等待时页面区分“正在连接”和“服务启动中”；历史会话不存在时显示“会话已失效”，提供新建排查入口；本轮调用失败时显示重试提示。

诊断后浏览器地址会保留会话编号，刷新或收藏该地址即可恢复材料与结果，继续补充证据。

支持 txt/cfg/conf/log/md/json 文本文件，文件名保留为引用来源；同名文件重新上传会更新材料。UTF-8 与常见中文文本编码自动读取。诊断页面自动组织材料输入，用户无需填写拓扑 JSON、命令输出数组或手动绑定。

原来的专业界面保留在 http://127.0.0.1:8765/workbench ，供队友调试 OSPF 专用工具、演示案例与课程知识库。普通用户使用首页即可。

首次体验可展开“用示例体验一次完整排查”，点击“① 带入故障材料”，再开始诊断。得到修改步骤后，点击“② 带入修改与复测材料”并选择“核对修改结果”，沿用同一会话完成复测。示例材料标明为合成演示，分析使用当前模型和工具。已有材料时先点击“新的排查”即可开始示例。

诊断引用支持“定位原文”，自动展开对应文件并高亮引用行。“本轮工具记录”展示实际调用及结果，参考资料保留来源、版本与行号；顶部进度展示材料、分析、修改和复测阶段。比赛讲解与页面操作见 [三分钟演示](docs/demo/三分钟演示.md)，后续任务统一在 [GitHub Issues](https://github.com/Red-hj/labagent/issues) 跟进。

页面背景按 suan.link 原站公开配置实现，使用本地 tsParticles 3.9.1：点击增加4个节点，鼠标400px范围连线，视差与自然漂移；顶部可暂停。没有登录与短信功能。动画库及许可证在 web/vendor，页面运行时不依赖外部CDN。

## 模型与结果

将 `.env.example` 复制为项目内 `.env`，填写服务地址、模型名和密钥。本机使用 DeepSeek 官方 Chat Completions 接口及 `deepseek-flash` 非思考模式。首页点击诊断时自动使用已配置模型；专业工作台仍可单独选择规则检查。

开放模式分析上传材料，逐行核对引用，给出排查方向和具体材料请求。上传补充输出后可继续分析并核对复测记录。Ping工具支持华为VRP、Windows中英文和Linux多行输出，并保留目标与收发统计的原文行。OSPF模式和开放模式使用同一份Ping解析逻辑。

## AgentArts SDK入口

`agentarts_app.py`使用官方`agentarts-sdk==0.1.10`包装现有LangGraph流程，提供`GET /ping`与`POST /invocations`。首次提交`materials`，后续用返回的`session_id`追问、获取步骤或更新材料。新增文件会合并，同名文件会更新。

在项目内安装独立环境并启动：

```powershell
python -m venv runtime/agentarts-env
runtime/agentarts-env/Scripts/python.exe -m pip install -r requirements-agentarts.txt
runtime/agentarts-env/Scripts/python.exe -B agentarts_app.py
```

本地入口为`http://127.0.0.1:8080`，模型配置沿用项目`.env`。运行`runtime/agentarts-env/Scripts/python.exe -B scripts/verify_agentarts.py`检查SDK协议；加`--live-model`可验证实际模型与工具调用。

华为云运行时镜像使用`Dockerfile.agentarts`，端口8080；当前Render网页继续使用原Dockerfile。SDK依赖独立安装，便于队友按需使用。接口和云端配置步骤见`docs/接口说明.md`。[官方SDK发布说明](https://pypi.org/project/agentarts-sdk/)、[AgentArts运行时接口](https://support.huaweicloud.com/api-agentarts/agentarts_07_0035.html)。

GitHub的Actions页面提供“Build AgentArts ARM64 image”：手动运行后在ARM64环境构建镜像，检查SDK协议、会话续接和随项目加载的知识资料，并生成可下载的镜像压缩包。镜像标签使用本次提交编号，适合上传华为云SWR后创建AgentArts运行时。构建使用公开仓库的[标准ARM64运行器](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)。

取得云端调用地址后，在项目内`runtime/agentarts-client.env`填写`LABAGENT_AGENTARTS_URL`与API Key认证对应的`LABAGENT_AGENTARTS_KEY`。用以下入口上传材料、继续追问；记录保存在`runtime/agentarts-client/`：

```powershell
python -B scripts/call_agentarts.py "data/examples/实验诊断材料_终端跨网段访问.txt"
python -B scripts/call_agentarts.py --session 上次显示的平台会话编号 --message "给我修改步骤"
```

同一实验持续使用相同的平台会话编号；SDK会将它关联到诊断记录，HTTP响应同时返回`platform_session_id`与`session_id`。云端配置、SWR上传和本地构建命令见`docs/接口说明.md`。

密钥、运行数据和个人课程资料不进入协作包。所有主动生成文件、环境与缓存均位于本项目内。

## 三人在线测试

已上线：[LabAgent在线测试站](https://labagent-15yh.onrender.com/)。三人共用用户名 `labagent` 与团队口令；口令在Render服务的Environment中对应`LABAGENT_TEAM_PASSWORD`。

[在自己的Render账号创建免费测试站点](https://render.com/deploy?repo=https://github.com/Red-hj/labagent)。部署配置在 `render.yaml`：Free计划、Docker运行现有Python与LangGraph后端、健康检查 `/api/health`，从main构建。当前通过公开仓库URL部署，更新代码后可在Render的Manual Deploy中部署最新提交；连接GitHub集成后可使用自动部署。

首次部署时在Render填写 `LABAGENT_LLM_KEY`。其余模型参数已经配置；平台自动生成 `LABAGENT_TEAM_PASSWORD`，可在服务Environment中查看。三人打开站点后共用用户名 `labagent` 和这个访问口令，进入同一个测试环境。线上“下载实验报告”会把Markdown报告保存到访问者电脑。

Render免费服务闲置15分钟后休眠，再次访问约一分钟唤醒；本地文件、SQLite会话会在休眠、重启或重新部署后重置。完成测试后下载报告留存。主机采用[免费方案](https://render.com/docs/free)，DeepSeek调用按现有模型账户用量计费。

自有服务器也可运行本项目Docker镜像，挂载 `/app/runtime` 与 `/app/data/course_notes` 以持久保存过程和课程资料。`PORT`设置监听端口，`LABAGENT_PUBLIC_URL`设置HTTPS网站根地址，`LABAGENT_TEAM_PASSWORD`设置团队口令；Render会自动提供线上地址。

## 开发入口

- `labagent/investigation.py`：开放排查的材料读取、知识检索、假设提交。
- `labagent/workflow.py`：LangGraph 编排、等待与恢复。
- `labagent/engine.py`、`parser.py`、`tools.py`：OSPF 专用检查。
- `web/`：本地网页。
- `web/ambient-network.js`：独立背景动效；页面与交互分别在 student.html、student.css、student.js。
- `docs/接口说明.md`：接口约定。
- `docs/后续开发.md`：当前设计、实测结果与下一步。
- `AGENTS.md`：开发约定。

## 本次协作包与分工

GitHub仓库：[Red-hj/labagent](https://github.com/Red-hj/labagent)。首次协作用 `git clone https://github.com/Red-hj/labagent.git` 获取代码，再按上方步骤安装依赖并配置各自的 `.env`。

添加队友：仓库的 **Settings → Collaborators（或 Collaborators & teams）→ Add people**，分别搜索两位队友的GitHub用户名并发送邀请，队友接受后即可参与开发。每人从最新main建立自己的分支，完成后提交Pull Request合并；开始工作前先同步main，遇到同名文件改动先比较再合并。

项目代码与演示数据通过Git同步；各自的模型配置、运行记录、导入课程资料和本地打包文件保存在本机。

### B 组案例与资料

`data/team_cases/` 提供五个 OSPF 实验练习：正常、区域不匹配、接口关闭、OSPF 未启用和互联 IP 网段错误。每例包含配置、诊断输入、预期与修复输入。运行步骤见 [OSPF 实验环境与复测](docs/实验环境与复测.md#b-组首批实验)。

```powershell
python -X utf8 scripts/evaluate_b.py labs --output runtime/b-first-five
python -X utf8 scripts/evaluate_b.py import-knowledge
```

在 `/workbench` 的“演示案例”中选择 `B组 · 01～05` 并载入，即可检查对应配置；页面自动切换到 `b-ospf` 资料范围与 OSPF 规则模式。修改配置后可在同一会话继续复测。

三份自编资料随项目加载，首页默认检索包含这些资料，工作台也可选择 `b-ospf` 单独使用。首次启动即可使用，手动导入命令继续保留；已导入的相同资料在检索时合并为一份。独立评测输入与答案由 B 单独保管。

黄泓钧负责SDK接入、引擎会话、复测工具与案例资料接入。付雨航继续真实实验与评测，赵亦康继续真实案例联调、演示与比赛展示；黄泓钧继续华为云运行时发布与联调。

完整包适合在新目录运行。已有v0.10.0的队友可使用 LabAgent_update_v0.10.0_to_v0.11.0.zip，也可以从GitHub获取最新代码。遇到自己已修改的同名文件先比较再合并，保留各自的实验数据与本地配置。

## 验证

常规检查不调用外部模型：

```powershell
.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.venv\Scripts\python.exe -B scripts/evaluate.py
```

需要重新验证真实模型时，按当前问题选择一项运行，会使用账户额度：

```powershell
.venv\Scripts\python.exe -B scripts/verify_open_diagnosis.py
.venv\Scripts\python.exe -B scripts/verify_model.py
```

运行记录保存在 `runtime/`。开发样例用于回归；当前演示网络材料均为合成数据。
