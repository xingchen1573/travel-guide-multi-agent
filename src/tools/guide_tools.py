"""随身向导agent专用工具：answer。

react-agent本身没有直接返回结构化数据的机制（见docs/ISSUES.md），沿用
src/tools/material_collection_tools.py的captured-dict边车容器模式：调用方
（src/orchestrator.py的guide_agent_node）传入一个可变dict，agent调用answer工具时
把回答文本和覆盖度标记写进这个dict，节点函数invoke完agent后从里面读回结构化结果。

guide_agent只绑这一个工具（加上workspace_tools.py里对它开放的read_workspace），
不涉及任何写入操作，符合docs/AGENTS.md第7节"权限矩阵里除read_workspace外没有任何
其他工具，是所有agent里权限最小的一个"这条约束。
"""
from __future__ import annotations

from langchain_core.tools import tool


def make_guide_tools(captured: dict) -> list:
    @tool
    def answer(text: str, covered: bool) -> str:
        """提交你对用户问题的回答。text是回答内容；covered标注这个问题是否已经被
        final_guide.md/outputs/poi_details/里的已有内容完全覆盖——true表示完全覆盖，
        不需要转交给其他agent补充；false表示部分或完全没有覆盖（比如问的是时效性信息，
        或树木详情没提到的更细节推荐），需要主agent决定是否转交给实时agent或衣食住行agent
        进一步查询。调用这个工具后本轮任务结束，不要再调用其他工具。"""
        captured["text"] = text
        captured["covered"] = covered
        return "回答已提交。"

    return [answer]
