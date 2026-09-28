# -*- coding: utf-8 -*-
"""聊天软件适配表：窗口怎么认、气泡什么颜色算「我发的」、填入点落在哪儿。
加一个聊天软件 = 在这张表里加一条 Platform，capture / ocr / fill / worker 都不用改。

采集路子不变：截自己的这个窗口 + 本地离线 OCR，不 hook、不注入、不解密、不碰对方进程内存。

实测基准是微信个人版（绿泡 + 三栏）。企业微信是同一套三栏布局（会话列表 | 聊天面板），
锚点规则同源，但**没有在真实企业微信上逐像素校准过**：认错了先调下面这条里的字段
（me_hue / me_right 最可能是它），或者照 probe/ 里那几个脚本拍几张帧跑一遍。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    exes: tuple          # 进程名（小写）：按它挑可见顶层窗口
    titles: tuple        # 主窗口标题：优先挑标题落在里面的那个，没有就取该进程第一个可见窗口
    header_h: int = 60   # 头部高度（像素，100% DPI）：面板顶往下这么多，会话名印在这一条里
    panel_col: float = 0.3   # 面板左右边界：整列底色占比超过它才算面板内（左侧会话列表是另一种底色，占比 ~0）
    panel_row: float = 0.9   # 面板上下边界：整行底色占比超过它才算面板内
    me_hue: str = "green"    # 「我发的」气泡底色：green（微信绿泡）/ blue / any（任何彩色气泡）
    me_right: bool = False   # 判「我发的」时是否还要气泡偏右：对方气泡也是彩色时多加一道保险（当前两家都没开）
    input_dx: int = 60   # 填入点：消息区左边界往右这么多像素
    input_dy: int = 40   # 填入点：消息区底线往下这么多像素


WECHAT = Platform(key="wechat", name="微信",
                  exes=("weixin.exe", "wechat.exe"), titles=("微信",))

# 企业微信：进程 WXWork.exe，标题「企业微信」。它头部比微信略矮一点，锚点照给。
# 自己的气泡实测是淡蓝 (201,231,255)（b−r=54）、对方是灰白 (228,231,235)（b−r=7），
# 颜色本身就把两边分干净了，所以只认蓝偏移、不再要求「偏右」——那道位置保险在真机上反而
# 会把自己的话判成对方（消息区宽窄一算偏，自己的气泡就落不进「右半边」）。
WECOM = Platform(key="wecom", name="企业微信",
                 exes=("wxwork.exe",), titles=("企业微信",),
                 me_hue="blue", me_right=False, header_h=56)

TABLE = {p.key: p for p in (WECHAT, WECOM)}
ORDER = ("wechat", "wecom")   # 自动识别时的优先顺序
DEFAULT = WECHAT
AUTO = "auto"                 # 设置里的「自动识别」，不是平台 key


def get(key) -> Platform:
    """key → Platform；认不出的一律当微信（老配置里没这个字段时也是走这里）。"""
    return TABLE.get(key or "", DEFAULT)


def is_me_bubble(bg, platform) -> bool:
    """这个气泡底色是不是「我发的」那种。微信是绿泡，企业微信是蓝泡（淡蓝到深蓝都算）；
    两家的对方气泡都是白/浅灰/深灰——灰阶一律不是。"""
    r, g, b = (int(v) for v in bg[:3])
    if platform.me_hue == "green":
        return g > r + 40 and g > b + 40
    if platform.me_hue == "blue":
        # 企业微信实测：自己的淡蓝泡 (201,231,255) b−r=54、对方的灰白泡 (228,231,235) b−r=7。
        # 饱和度很低（通道极差只有 ~54，深色主题还更低），按「彩色」的极差 ≥40 判会漏，
        # 自己的话全认成对方；改看蓝偏移：淡蓝泡 b−r ≥ 12 就分得开，深蓝泡更大也吃得下。
        # b ≥ g > r 顺带排掉绿泡（微信）和各种暖色。
        return b > r + 12 and b >= g > r
    return max(r, g, b) - min(r, g, b) >= 40
