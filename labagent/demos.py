"""Materials for the optional, live diagnosis walkthrough."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def network_access_demo():
    name = "终端跨网段访问.txt"
    original = (ROOT / "data/examples/实验诊断材料_终端跨网段访问.txt").read_text(encoding="utf-8")
    repaired = original.split("五、本轮运行记录")[0].replace("10.20.0.254", "10.20.0.1")
    repaired = repaired.replace("PC1已经获取地址，但访问10.30.0.10失败。", "PC1重新获取地址后，可以访问10.30.0.10。")
    repaired += """五、本轮修改与运行记录
本轮已将R1地址池gateway-list改为10.20.0.1，PC1重新获取DHCP租约，以下输出在修改后重新采集。
PC1执行ping 10.20.0.1：发送4个，收到4个。
PC1执行ping 10.30.0.10：发送4个，收到4个。
PC1的ARP表：10.20.0.1有对应MAC。
R1执行ping 10.20.0.10：发送4个，收到4个。
R1执行ping 10.30.0.10：发送4个，收到4个。
SRV1执行ping 10.30.0.1：发送4个，收到4个。
R1路由表包含10.20.0.0/24和10.30.0.0/24两条直连路由。

六、本轮任务
比较配置和终端参数的修改，核对当前Ping结果，确认PC1访问10.30.0.10的实验目标。
"""
    return {"title": "终端取得地址，但跨网段访问失败", "source": "合成演示材料",
            "description": "两轮材料展示诊断、追问、修改与复测。分析由当前模型和工具执行。",
            "stages": [
                {"title": "故障材料", "description": "带入DHCP地址池、终端参数、拓扑与访问记录。", "materials": {name: original}},
                {"title": "修改与复测材料", "description": "更新同名材料，带入修改后的网关、租约和本轮Ping输出。", "materials": {name: repaired}}
            ]}
