# -*- coding: utf-8 -*-
"""会话摘要：把「已经滑出上下文窗口」的旧对话压成一段背景，跟着最近几条一起喂给判断和起草。

为什么有它：build_state() 和 draft_candidates() 都只看最近 N 条（设置里的「参考上下文」，默认 10）。
她半小时前说的那句关键的话被后面十几条闲聊顶出窗口，Jev 就看不见了。这里把窗口外的部分折成一段
一两百字的背景——「摘要 + 最近 N 条」，判断和起草都带上。

摘要活在内存里（main.py 的 chats），不落盘、不进日志、重启即弃；只有真触发压缩的那一次会出网，
发出去的是旧摘要 + 那几条被折进去的消息，走的是**起草**那把 key 和那个模型（判断那把不动）。

攒够 _MIN_LINES 条（或 _MIN_CHARS 字）才压一次：来一条压一条等于每次分析都多一次调用，不划算。
"""
from __future__ import annotations

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .llm import chat
except ImportError:
    from llm import chat

_TARGET = 300         # 摘要目标字数
_MAX = 800            # 模型偶尔写超，硬截
_MIN_LINES = 6        # 窗口外攒够这么多条才压
_MIN_CHARS = 300      # 或者攒够这么多字
_HISTORY_SLACK = 100  # 已经进摘要的原文再多留几条（留点余量，改口径时好排查）

_SYSTEM = (
    "你把一段聊天记录压成「背景摘要」，给另一个模型判断后续对话用。\n"
    "只输出摘要本身：不要标题、不要开场白、不要解释你在做什么。\n"
    "必须留下：双方是什么关系、正在为什么事较劲或商量、说定过的事（时间、地点、承诺）、"
    "还没解决的情绪和没说出口的伏笔。\n"
    "不要留：寒暄、重复的话、语气词；也不要写记录里没出现过的推测。\n"
    f"不超过 {_TARGET} 字，用中文。"
)


def update(old: str, lines: list, *, protocol: str, base_url: str | None, key: str,
           model: str, timeout: float, headers: dict | None = None) -> str:
    """旧摘要 + 新滑出窗口的这几条 → 新摘要。lines 是已经排好序的「谁: 说了什么」。
    失败会抛（JevError 等）：调用方自己决定降级——摘要挂了不该连累这一次判断和起草。"""
    body = "\n".join(str(x) for x in lines)
    user = (f"已有摘要（可能为空）：\n{old.strip() or '（无）'}\n\n"
            f"新滑出窗口的对话（按时间顺序）：\n{body}\n\n输出更新后的摘要。")
    out = chat(protocol, base_url, key, model, _SYSTEM, [user],
               temperature=0.2, max_tokens=600, headers=headers, timeout=timeout)
    return out.strip()[:_MAX]


def plan(history: list, summarized: int, keep: int) -> list:
    """history 里「已经滑出最近 keep 条、又还没进摘要」的那一段；没攒够就返回空。

    history 是 [(who, text, name)]，summarized = 前多少条已经进摘要了（下标从 0 数）。
    """
    end = max(0, len(history) - keep)
    seg = history[summarized:end] if summarized < end else []
    if len(seg) >= _MIN_LINES or sum(len(str(m[1])) for m in seg) >= _MIN_CHARS:
        return list(seg)
    return []


def trim(history: list, summarized: int) -> int:
    """已经进摘要的原文不必一直占内存：摘要点之前只留 slack 条做余量，返回新的 summarized。
    只动 history 头部（都是已摘要的），所以 summarized 跟着减去同样多就还是对的。"""
    cut = summarized - _HISTORY_SLACK
    if cut <= 0:
        return summarized
    del history[:cut]
    return summarized - cut


if __name__ == "__main__":
    # ponytail: 不联网。这里唯一会坏的是「什么时候压」「压完下标对不对」这两件纯逻辑。
    lines = [(lambda i: ("her", f"第{i}条", None))(i) for i in range(20)]

    assert len(plan(lines, 0, keep=10)) == 10          # 窗口外 10 条，够压
    assert plan(lines, 0, keep=17) == []               # 只滑出 3 条，先攒着
    assert plan(lines, 5, keep=10) == []               # 只滑出 5 条，也先攒着
    long_words = [("her", "字" * 200, None)]           # 条数不够但字数够，照样压
    assert len(plan(long_words * 2, 0, keep=0)) == 2
    assert plan([], 0, keep=10) == [] and plan(lines, 0, keep=0)[0][1] == "第0条"

    # trim：摘要点之前只留 slack 条，返回的 summarized 跟着平移
    h = [("her", "x", None)] * 300
    assert trim(h, 100) == 100 and len(h) == 300       # 还没超余量，不裁
    assert trim(h, 300) == 100 and len(h) == 100       # 裁掉 200 条老的，留下 slack 条
    assert trim([("her", "x", None)] * 5, 5) == 5      # 短会话不动

    # update：拼进请求的是旧摘要 + 这批新对话；返回去掉空白、超长截断
    seen = {}
    chat = lambda *a, **k: (seen.update({"system": a[4], "user": a[5][0], "model": a[3]}),
                            "  摘要正文  ")[1]
    out = update("旧摘要", ["her: 在吗", "me: 在"], protocol="openai", base_url=None,
                 key="k", model="m", timeout=1)
    assert out == "摘要正文", out
    assert "旧摘要" in seen["user"] and "her: 在吗" in seen["user"] and "更新后的摘要" in seen["user"]
    assert seen["model"] == "m" and "背景摘要" in seen["system"]
    chat = lambda *a, **k: "字" * 1000
    assert len(update("", ["x"], protocol="openai", base_url=None, key="k", model="m",
                       timeout=1)) == _MAX  # 写超了硬截
    print("summary ok")
