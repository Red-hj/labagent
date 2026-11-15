"""A small optional gateway exercise using generated, clearly labelled materials."""
import secrets

from .demos import network_access_demo


def new_challenge():
    variant = secrets.choice(("gateway", "subnet"))
    demo = network_access_demo()
    materials = demo["stages"][0]["materials"]
    text = next(iter(materials.values()))
    if variant == "subnet":
        text = text.replace("10.20.0.254", "10.99.0.254")
    text = text.split("六、待诊断的问题")[0]
    return {"id": variant, "title": "网关找错练习", "source": "合成练习材料", "materials": {"网关练习.txt": text},
            "question": "PC1访问服务器失败，先判断故障发生在哪一步。", "choices": ["主机选择的默认网关", "服务器的地址", "R1到服务器的直连接口"]}


def check_answer(challenge_id, choice):
    if challenge_id not in ("gateway", "subnet") or type(choice) is not int or choice not in range(3):
        raise ValueError("请先载入练习并选择一项")
    correct = choice == 0
    return {"correct": correct, "reply": "找到了：PC1应使用10.20.0.1作为默认网关。修改DHCP地址池并重新获取租约，再复测服务器。" if correct else "再看PC1的Default Gateway，并与R1接口地址比较。"}
