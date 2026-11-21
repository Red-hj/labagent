"""Keep each model tool's schema, handler and progress label together."""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Tool:
    definition: dict
    handler: Callable
    label: str


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def register(self, definition, label):
        def install(handler):
            name = definition["function"]["name"]
            if name in self.tools:
                raise ValueError(f"重复的工具名称：{name}")
            self.tools[name] = Tool(definition, handler, label)
            return handler
        return install

    def definitions(self):
        return [tool.definition for tool in self.tools.values()]

    def execute(self, name, *args, **kwargs):
        tool = self.tools.get(name)
        if tool is None:
            raise ValueError("不支持的工具")
        return tool.handler(*args, **kwargs)

    def label(self, name):
        tool = self.tools.get(name)
        return tool.label if tool else "正在处理工具请求"
