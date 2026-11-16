"""Install Huawei's official signing SDK inside the project runtime directory."""
import argparse
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
SDK_URL = "https://obs.cn-north-1.myhuaweicloud.com/apig-sdk/ApiGateway-python-sdk.zip"


def main():
    parser = argparse.ArgumentParser(description="准备华为云IAM调用所需的官方签名SDK")
    parser.add_argument("--archive", type=Path, help="使用已下载的官方SDK压缩包")
    args = parser.parse_args()
    if args.archive:
        content = args.archive.read_bytes()
    else:
        with urlopen(SDK_URL, timeout=60) as response:
            content = response.read()
    destination = ROOT / "runtime/agentarts-signing-sdk/apig_sdk"
    names = ("__init__.py", "signer.py", "signer_v11.py", "sm3_hash.py")
    with ZipFile(BytesIO(content)) as archive:
        members = {name: next((item for item in archive.namelist()
                              if item.endswith("/apig_sdk/" + name)), None) for name in names}
        if not all(members.values()):
            parser.error("SDK压缩包缺少V11签名文件，请使用华为官方Python签名SDK")
        destination.mkdir(parents=True, exist_ok=True)
        for name, member in members.items():
            (destination / name).write_bytes(archive.read(member))
    print("华为官方签名SDK已安装：" + str(destination))


if __name__ == "__main__":
    main()
