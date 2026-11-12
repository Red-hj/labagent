# LabAgent 实验诊断工具

当前版本 v0.8.0，默认页面只需要用户提供诊断材料。上传文件或粘贴内容后直接开始分析，问题描述可以留空。分析后可以继续追问原因、获取修改步骤、直接补充请求中的材料，再核对修改与测试结果。

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

材料导入会显示添加/更新数量；提交后诊断区显示处理状态和等待时间，追问立即进入对话并显示等待回复。完成后显示成功提示；失败会说明原因并保留输入，便于重新提交。回复采用直接的技术说明，正文解释关键参数，行号与原文在“查看材料依据”中展开。

诊断后浏览器地址会保留会话编号，刷新或收藏该地址即可恢复材料与结果，继续补充证据。

支持 txt/cfg/conf/log/md/json 文本文件，文件名保留为引用来源；同名文件重新上传会更新材料。UTF-8 与常见中文文本编码自动读取。诊断页面自动组织材料输入，用户无需填写拓扑 JSON、命令输出数组或手动绑定。

原来的专业界面保留在 http://127.0.0.1:8765/workbench ，供队友调试 OSPF 专用工具、演示案例与课程知识库。普通用户使用首页即可。

页面背景按 suan.link 原站公开配置实现，使用本地 tsParticles 3.9.1：点击增加4个节点，鼠标400px范围连线，视差与自然漂移；顶部可暂停。没有登录与短信功能。动画库及许可证在 web/vendor，页面运行时不依赖外部CDN。

## 模型与结果

将 `.env.example` 复制为项目内 `.env`，填写服务地址、模型名和密钥。本机使用 DeepSeek 官方 Chat Completions 接口及 `deepseek-flash` 非思考模式。首页点击诊断时自动使用已配置模型；专业工作台仍可单独选择规则检查。

开放模式分析上传材料，逐行核对引用，给出排查方向和具体材料请求。上传补充输出后可继续分析并核对复测记录。OSPF 模式提供规则检查，开放模式提供模型分析。AgentArts 云端接入是下一阶段工作。

密钥、运行数据和个人课程资料不进入协作包。所有主动生成文件、环境与缓存均位于本项目内。

## 三人在线测试

[在Render创建免费测试站点](https://render.com/deploy?repo=https://github.com/Red-hj/labagent)。部署配置在 `render.yaml`：Free计划、Docker运行现有Python与LangGraph后端、健康检查 `/api/health`，从main更新。

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

本轮改动集中在引擎交互、学生入口和你指定的背景效果。专业工作台的 web/index.html、web/app.js、web/style.css 及队友实验资料未改动。付雨航继续真实实验与评测，赵亦康继续真实案例联调、演示与比赛展示；AgentArts平台接入仍由黄泓钧推进。

完整包适合在新目录运行。已有v0.7.0的队友使用 LabAgent_update_v0.7.0_to_v0.7.3.zip；仍在v0.6的队友先合并上一份v0.6到v0.7的增量。只合并改动文件；遇到自己已修改的同名文件先比较再合并，保留各自的实验数据与本地配置。不要直接用完整文件夹覆盖队友的工作。

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
