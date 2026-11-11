"""Framework-neutral local tool boundary. No AgentArts SDK dependency or deployment."""
from .engine import diagnose


def diagnose_ospf_tool(configs, topology, requirements=None, question="", hint_level=1, observations=None):
    """Use the same function locally and later wrap it in a cloud tool handler."""
    return diagnose({"configs": configs, "topology": topology, "requirements": requirements or {},
                     "question": question, "hint_level": hint_level, "observations": observations or {},
                     "use_llm": False})
