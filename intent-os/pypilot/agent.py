"""Agent 对外入口。"""
from pypilot.config import RECURSION_LIMIT
from pypilot.graph.build import build_graph
from pypilot.graph.nodes import Ui
from pypilot.graph.state import new_turn
from pypilot.llm.client import make_llm
from pypilot.memory.checkpointer import make_checkpointer, new_thread_id
from pypilot.prompts import nudges

class Agent:
    def __init__(
        self,
        verbose: bool = True,
        llm=None,
        checkpointer=None,
        thread_id: str | None = None,
        summarize: bool = False,
    ):
        # llm 可注入：默认 make_llm()；测试时传 mock 对象确定性回放剧本。
        self.llm = llm if llm is not None else make_llm()
        self.ui = Ui(verbose)
        # checkpointer 就是"内存记忆"本体：按 thread_id 存整条消息史与全部记账状态
        self.checkpointer = checkpointer if checkpointer is not None else make_checkpointer()
        self.thread_id = thread_id or new_thread_id()
        self.app = build_graph(self.llm, self.checkpointer, self.ui, summarize=summarize)

    @property
    def verbose(self) -> bool:
        return self.ui.verbose

    @verbose.setter
    def verbose(self, value: bool) -> None:
        self.ui.verbose = bool(value)

    def run(self, user_input: str) -> str:
        """执行一个任务，返回最终回答。"""
        config = {
            "configurable": {"thread_id": self.thread_id},
            "recursion_limit": RECURSION_LIMIT,
        }
        out = self.app.invoke(new_turn(user_input), config)
        return out.get("final") or nudges.EMPTY_FINAL

    def reset(self):
        """清空当前会话历史。"""
        try:
            self.checkpointer.delete_thread(self.thread_id)
        except Exception:
            # 有的后端不支持 delete_thread；换 thread_id 本身已经能达到"翻篇"的效果
            pass
        self.thread_id = new_thread_id()
