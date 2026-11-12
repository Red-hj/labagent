# 互联 Area 不一致

- 设计状态：离线配置案例；仿真／设备未运行。
- 拓扑：PC1—R1 GE0/0/2，R1 GE0/0/0—R2 GE0/0/0，R2 GE0/0/2—PC2。
- 主机：PC1 192.168.10.10/24 网关 192.168.10.1；PC2 192.168.20.10/24 网关 192.168.20.1。
- 设备型号、VRP 或模拟器版本：待现场填写。
- 注入：仅将 R2 互联接口的 network 从 Area 0 移至 Area 1；保留其 LAN 在 Area 0。
- 修复：在 R2 的 OSPF 1 Area 1 视图撤销互联 network，在 Area 0 恢复同一条 network。
- 预期：预期邻接或端到端通信受影响；以原始采集结果为准。
- 验证：按 [实操与证据采集](../../../docs/实验环境与复测.md#b-组首批实验) 采集 before.txt、after.txt；当前模板不属于设备证据。
- 提交应用时只交 input.json 或 repaired.json，勿提交 expected.json。
