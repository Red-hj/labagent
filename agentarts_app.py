"""Run LabAgent through Huawei's AgentArts Runtime SDK."""
import sys
sys.dont_write_bytecode = True

import os
from pathlib import Path
from labagent.model import load_env
from labagent.agentarts_runtime import create_app

load_env(Path(__file__).resolve().parent / ".env")
app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("AGENTARTS_BIND_IP", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8080")))
