# -*- coding: utf-8 -*-
"""风格 skill：读 skills/<名字>/SKILL.md，只取「怎么说」的那几节，拼成一段给起草模型的参考。

格式照 Claude Code / Agent Skills 那套来：一个目录一个 SKILL.md（YAML frontmatter + Markdown 正文）。
但本项目是**一次 API 调用**，没有「模型自己去读文件」那一层，所以 references/、scripts/、allowed-tools
这些一概不看——要哪几节只能由代码先挑好拼进去。

两条省 token 的路，也是这里唯一干的两件事：
- 位置：拼出来的这段接在 SYSTEM 尾部（core/draft._system），整段内容稳定、位置固定 ⇒ 是稳定前缀，
  OpenAI 系（DeepSeek 等）的自动前缀缓存能命中，缓存命中的那部分按各家规则打折；
- 体积：蒸馏开着就用起草模型把它压成几百字的口吻卡，缓存在 skills/.cache/（源文件或模型变了自动重算）；
  蒸馏关掉就直接用取节后的原文，长得多，每次都要原价发。

硬约束：只借表达层，不借身份层——「角色扮演规则」「身份卡」这类标题一律不进，否则模型会开始扮演文档里那个人，
而本项目要的是「像 me 本人」。落盘的只有 skill 自己的文本（口吻卡），不含任何聊天内容。
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .llm import chat
except ImportError:
    from llm import chat

_SKILL_FILE = "SKILL.md"
_CACHE_DIR = ".cache"

# 节标题的取舍：先看 drop，命中就丢（身份层和传记类）；再看优先级，越靠前越该留。
# 都是关键词包含匹配、大小写无关——同一份文档的节名各写各的，写死标题没法通用。
_SECTION_DROP = ("角色扮演", "身份卡", "roleplay", "role-play", "persona", "identity",
                 "时间线", "timeline", "谱系", "lineage", "调研来源", "research source", "来源")
_SECTION_PRIORITY = (
    ("表达", "dna", "风格", "语气", "口吻", "style", "tone", "voice", "speech"),        # 最该留：怎么说
    ("启发", "heuristic", "决策", "decision"),                                          # 怎么写
    ("价值观", "禁忌", "反模式", "values", "anti-pattern"),                              # 不写什么
    ("心智模型", "思维模型", "mental model"),                                            # 怎么想，最占地方，额度不够先丢它
)

_MAX_CHARS = 3000      # 不蒸馏时的上限：按上面的优先级拼，超了就丢后面的节（直发，每次都要原价发）
_INPUT_MAX = 4000      # 蒸馏时的输入上限：先让模型看全再压，输出仍然只有几百字，这里可以宽松
_DISTILL_CHARS = 400   # 蒸馏的目标字数
_DISTILL_MAX = 1200    # 模型偶尔写超，硬截到这儿
_PROMPT_VERSION = "1"  # 改了口吻卡的提示词就 +1，让老缓存失效

_DISTILL_SYSTEM = (
    "你把一份人物风格文档压成一张「口吻卡」，给另一个写聊天消息的模型当参考。\n"
    "只输出卡片本身：不要开场白、不要标题、不要 markdown 代码块、不要解释你在做什么。\n"
    "全用中文，只写这四类内容：\n"
    "1) 称呼习惯（怎么叫对方）；\n"
    "2) 用词和句子习惯（句长、标点、语气词、常挂嘴边的话）；\n"
    "3) 表达禁忌（这个人明确不会说什么样的话）；\n"
    "4) 遇事时的判断倾向，一条一句话。\n"
    f"总共不超过 {_DISTILL_CHARS} 字。不要写「我是XX」这种自称，不要抄原文档里的例子和原话。"
)


@dataclass(frozen=True)
class Skill:
    """扫到的一份 skill。key = 目录名（设置里存的就是它）。"""
    key: str
    title: str   # frontmatter 的 name，没有就用目录名
    desc: str    # frontmatter 的 description，设置页显示用
    path: str    # SKILL.md 的绝对路径
    chars: int   # 文件总字数，设置页显示用


def root_dir() -> str:
    """skills/ 跟 config.json 放一起：打包后是 exe 旁边，源码跑就是仓库根。
    （同 app/settings.py 的 _ROOT，那边管 config.json，这边管 skill 目录。）"""
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "skills")


def list_skills(root: str | None = None) -> list[Skill]:
    """扫 skills/*/SKILL.md。没有这个目录、或某一份读不动，都只是少一条，不报错。"""
    base = root or root_dir()
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for name in names:
        path = os.path.join(base, name, _SKILL_FILE)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        fm = _frontmatter(text)
        out.append(Skill(key=name, title=fm.get("name") or name,
                         desc=fm.get("description") or "", path=path, chars=len(text)))
    return out


def resolve(name: str, *, distill: bool, protocol: str, base_url: str | None, key: str,
            model: str, timeout: float, headers: dict | None = None,
            root: str | None = None) -> str:
    """给起草用的最终文本。name 空 / 找不到 / 挑不出节，都返回空串（= 没用 skill，一个字都不多发）。

    distill=True：优先用 skills/.cache/ 里的口吻卡；没有或过期（源文件、模型、提示词版本任一变了）
    就现在生成一次再缓存；生成失败退回取节原文——宁可多发一点，也别让起草整个挂掉。
    """
    key_name = str(name or "").strip()
    if not key_name:
        return ""
    base = root or root_dir()
    try:
        with open(os.path.join(base, key_name, _SKILL_FILE), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""  # skill 被删了/改名了：当没选，设置页里看得出来
    picked = _sections(_body(text))
    if not picked:
        return ""
    if not distill:
        return _wrap(_assemble(picked))
    cache = _cache_path(base, key_name, _fingerprint(text, model))
    card = _read_cache(cache)
    if not card:
        try:
            card = _distill(_assemble(picked, _INPUT_MAX), protocol, base_url, key,
                            model, timeout, headers)[:_DISTILL_MAX]
        except Exception:
            card = ""
        if card:
            _write_cache(cache, card)
    return _wrap(card or _assemble(picked))  # 蒸馏挂了就用原文取节顶上


def _wrap(body: str) -> str:
    """口吻卡/节选外面那圈框。三件事必须说死：这不是身份、这份就是本次要用的口吻、
    别提这段参考的存在。选了 skill 就不再给 me 以往的样本了，所以这里不用写「样本优先」。"""
    return ("我的表达风格参考（这就是 me 接下来要用的口吻，不是你的身份，也不需要介绍自己）：\n"
            f"{body.strip()}\n"
            "用法：me 接下来这几条消息就照这份的口吻写——用词、句长、标点、语气词都照它，"
            "不用再模仿这段对话里 me 以往的说法。不要复述这份参考里的例子，也不要提起它。")


def _body(text: str) -> str:
    """剥掉 YAML frontmatter，只留正文。"""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1:])
    return text


def _frontmatter(text: str) -> dict:
    """只认 frontmatter 里的 name / description（别的字段本项目用不上）。
    手写解析，不为两行元数据拖个 yaml 依赖；块标量（`description: |`）拼成一行。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict = {}
    current = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            current = m.group(1)
            value = m.group(2).strip()
            out[current] = [] if value in ("", "|", ">", "|-", ">-", "|+", ">+") else value.strip("'\"")
        elif current and line[:1].isspace():  # 块标量的续行
            if isinstance(out.get(current), list):
                out[current].append(line.strip())
    return {k: (" ".join(v).strip() if isinstance(v, list) else v) for k, v in out.items()}


def _sections(body: str) -> list:
    """按二级标题切节，按优先级排好（同级保持原文顺序）。标题之前的引言和没归类的节都丢掉。"""
    parts, title, buf = [], None, []
    for line in body.splitlines():
        if line.startswith("## "):
            parts.append((title, "\n".join(buf).strip()))
            title, buf = line[3:].strip(), []
        else:
            buf.append(line)
    parts.append((title, "\n".join(buf).strip()))
    picked = [(t, b) for t, b in parts if b and _rank(t)]
    picked.sort(key=lambda kv: _rank(kv[0]))  # 稳定排序：同优先级的照原文顺序
    return picked


def _rank(title: str) -> int:
    """这个节该不该留、排多前。0 = 不要（身份层、传记类、没归类的）。"""
    t = (title or "").lower()
    if any(k in t for k in _SECTION_DROP):
        return 0
    for i, keys in enumerate(_SECTION_PRIORITY, 1):
        if any(k in t for k in keys):
            return i
    return 0


def _assemble(picked: list, limit: int = _MAX_CHARS) -> str:
    """按排好的顺序拼，额度用完就丢后面的节（半节不值得塞）。"""
    out, used = [], 0
    for title, body in picked:
        chunk = f"## {title}\n{body}"
        if used + len(chunk) > limit:
            break
        out.append(chunk)
        used += len(chunk) + 2
    return "\n\n".join(out)


def _fingerprint(text: str, model: str) -> str:
    """源文件 + 模型 + 提示词版本：任一变了就重算口吻卡。"""
    return f"{_PROMPT_VERSION}|{model}|{hashlib.sha1(text.encode('utf-8')).hexdigest()}"


def _cache_path(base: str, name: str, fingerprint: str) -> str:
    h = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:12]
    return os.path.join(base, _CACHE_DIR, f"{name}.{h}.txt")


def _read_cache(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_cache(path: str, text: str) -> None:
    """原子写（临时文件 + replace）：并发跑两次分析也不会读到半个文件。写不进去不算错，下次再算。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        pass


def _distill(text: str, protocol: str, base_url: str | None, key: str, model: str,
             timeout: float, headers: dict | None) -> str:
    """用起草那把 key、那个模型把节选压成口吻卡。低温度：这是压缩，不是创作。"""
    if not text.strip():
        return ""
    return chat(protocol, base_url, key, model, _DISTILL_SYSTEM,
                [f"文档：\n{text}\n\n输出口吻卡。"], temperature=0.3, max_tokens=800,
                headers=headers, timeout=timeout).strip()


if __name__ == "__main__":
    # ponytail: 不联网。frontmatter / 切节 / 优先级 / 截断是这里唯一会坏的非平凡逻辑。
    fm = _frontmatter("---\nname: demo-skill\ndescription: |\n  第一行\n  第二行\ntags: [a]\n---\n# 正文\n")
    assert fm["name"] == "demo-skill" and fm["description"] == "第一行 第二行", fm
    assert _frontmatter("没有 frontmatter") == {}
    assert _frontmatter("---\nname: x\n未完")["name"] == "x"  # 没闭合也认已读到的

    doc = ("引言不该进\n"
           "## 决策启发式\n- 不确定就是不喜欢\n"
           "## 身份卡\n我是XX\n"
           "## 表达DNA\n**称谓**：兄弟\n"
           "## 时间线\n| 2020 | 出道 |\n"
           "## 核心心智模型\n长篇大论\n")
    body = _body("---\nname: x\n---\n" + doc)
    picked = _sections(body)
    titles = [t for t, _ in picked]
    assert titles == ["表达DNA", "决策启发式", "核心心智模型"], titles  # 身份卡/时间线/引言都丢了
    text = _assemble(picked, limit=10)  # 额度不够就什么都不塞，不塞半节
    assert text == "", text
    text = _assemble(picked)
    assert "兄弟" in text and "我是XX" not in text and "不确定就是不喜欢" in text
    # 优先级：表达在最前，额度紧的时候先丢后面那些（决策启发式、心智模型垫底）
    text = _assemble(picked, limit=len("## 表达DNA\n**称谓**：兄弟") + 10)
    assert "表达DNA" in text and "决策" not in text and "心智模型" not in text, text

    assert _rank("表达DNA") == 1 and _rank("身份卡") == 0 and _rank("时间线") == 0
    assert _fingerprint("a", "m") != _fingerprint("a", "m2")  # 换模型重算
    assert _fingerprint("a", "m") != _fingerprint("b", "m")   # 改文档重算
    assert resolve("", distill=True, protocol="openai", base_url=None, key="k", model="m",
                   timeout=1) == ""
    assert resolve("不存在的skill", distill=False, protocol="openai", base_url=None, key="k",
                   model="m", timeout=1) == ""
    print("skills ok")
