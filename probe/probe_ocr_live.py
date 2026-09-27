# -*- coding: utf-8 -*-
"""
持续 OCR 探针：WGC 盯着聊天窗口（微信 / 企业微信），消息区一变就 OCR，新冒出来的文字实时打到控制台。
在 IDE 里直接 Run，Ctrl-C / 停止按钮结束。

    python probe/probe_ocr_live.py            # 自动认：按 platforms.ORDER 先微信后企业微信
    python probe/probe_ocr_live.py wecom      # 只认企业微信

识别这一套直接调 app/ 里的实现（同一个 chat_area、同一个 who_said），所以这里看到什么、主程序就是什么——
换一个聊天软件要校准，看的就是这个；认错人也先在这里看分类对不对。

    pip install rapidocr-onnxruntime numpy windows-capture
    命令行跑记得带上项目根：set PYTHONPATH=. && python probe/probe_ocr_live.py

只读，帧只在内存，不写盘不上传。
"""
import ctypes
import difflib
import re
import sys
import time
import traceback

import numpy as np
from rapidocr_onnxruntime import RapidOCR
from windows_capture import WindowsCapture

from app.capture import chat_area, find_chat_hwnd, unminimize
from app.ocr import who_said


ctypes.windll.user32.SetProcessDPIAware()
ocr = RapidOCR(intra_op_num_threads=4, det_limit_type="max", det_limit_side_len=4000)
state = {"shape": None, "area": None, "bg": None, "last": None, "seen": [], "pending": None, "t": 0, "t0": 0, "lh": None, "shown": None}
SETTLE = 0.25  # 秒：画面停稳这么久才 OCR，跳过滚动/新消息滑入的中间帧（半截气泡会认错、会重复）
MAX_WAIT = 1.0  # 秒：画面一直在变（动图表情包）就永远停不稳，最多等这么久照样 OCR
hwnd, pf = find_chat_hwnd(sys.argv[1] if len(sys.argv) > 1 else None)  # 认哪个软件见 app/platforms.py
cap = WindowsCapture(window_hwnd=hwnd)


@cap.event
def on_frame_arrived(frame, control):
    """采集线程：只做跟上一帧比，变了就把整帧挂成 pending，消息区定位 + OCR 交给主线程去抖后再做。"""
    full = np.ascontiguousarray(frame.frame_buffer[:, :, :3][:, :, ::-1])  # BGRA → RGB，纯内存
    if full.max() == 0:
        return
    if state["area"] is None or full.shape != state["shape"]:
        state["shape"], state["area"] = full.shape, chat_area(full, pf)
    if state["area"] is None:
        return
    x0, y0, x1, y1 = state["area"][:4]
    chat = full[y0:y1, x0:x1]
    if state["last"] is not None and np.array_equal(chat, state["last"]):
        return  # 画面没变，0 开销
    state["last"] = chat
    if state["pending"] is None:
        state["t0"] = time.perf_counter()  # 这轮变化开始的时刻
    state["pending"], state["t"] = full, time.perf_counter()


def process(full):
    area = chat_area(full, pf)  # 每次停稳都重算：拖完窗口布局会晚一拍才铺好，只按尺寸变化算一次会锁死在半成品上
    if area is None:
        print("消息区认不出来（窗口太小/布局没铺好），等下一帧")
        return
    if area[:4] != state["shown"]:  # 末位是 numpy 底色，只比前四个
        state["shown"] = area[:4]
        print(f"消息区 x{area[0]}-{area[2]} y{area[1]}-{area[3]}")
    state["area"] = area
    x0, y0, x1, y1, state["bg"], _pane = area
    chat = full[y0:y1, x0:x1]
    t0 = time.perf_counter()
    res, _ = ocr(chat, use_cls=False)
    ms = (time.perf_counter() - t0) * 1000
    # 群聊：每条 her 气泡上方有一行灰色发言人名（靠左、短、不带冒号、直接印在面板底色上），
    # 从上往下扫，名字带给后面的气泡。引用块/时间戳/公告带冒号，链接卡片的灰字印在气泡底色上，都不会被当成名字。
    # 单聊没有名字行，就是裸 her。
    # ponytail: 名字行被 OCR 漏掉时会挂到上一个人头上。
    name, lines = None, []
    for box, text, _ in sorted(res or [], key=lambda r: r[0][0][1]):
        kind, bg, h = who_said(chat, box, pf)
        if kind == "gray":
            on_pane = bg is not None and np.abs(bg - state["bg"]).sum() <= 6
            if on_pane and box[0][0] < 0.25 * (x1 - x0) and len(text) <= 16 and not re.search("[:：]", text):
                name = text
            continue
        if kind is None:
            continue
        if state["lh"] and h < 0.6 * state["lh"]:
            continue  # 字比正常气泡小得多 = 图片消息（截图/表情包）里的字，不是气泡
        lines.append((f"her({name})" if kind == "her" and name else kind, text, box[0][1], h))
    if not state["lh"] and len(lines) >= 3:
        state["lh"] = float(np.median([h for *_, h in lines]))  # 用头一帧定下正常字高
    # 去重（滚动不重复）：
    #  - 同一段像素挪个位置 OCR 结果会抖（「傻逼了」↔「傻逼」、「不好意思」↔「不好竟思」），所以按相似度判已见，不按全等
    #  - 本帧有已知行时，只打已知行下方的新行：往上滚翻出来的旧消息在已知行上方，不打
    #  - 本帧一行已知的都没有（大图/表情包把旧文字全顶出去了、切了聊天、滚远了）：全打。宁可多打也不能漏新消息。
    # ponytail: 同一人连发两句一模一样的会被吞一句；切聊天/滚远会把当前可见的历史打一遍。
    #           真要分「新来的」还是「翻出来的」，拿相邻两帧行均值做互相关算滚动方向，此处不做。
    seen = state["seen"]
    known_y = [y for w, t, y, _ in lines if is_seen(seen, w, t)]
    floor = max(known_y) if known_y else -1
    new = [(w, t) for w, t, y, _ in lines if y > floor and not is_seen(seen, w, t)]
    seen.extend((w, t) for w, t, *_ in lines if not is_seen(seen, w, t))
    del seen[:-500]
    for who, text in new:
        print(f"{time.strftime('%H:%M:%S')} [{ms:4.0f}ms] {who}: {text}", flush=True)


def is_seen(seen, who, text):
    for w, t in seen:
        if w != who:
            continue
        if t == text or difflib.SequenceMatcher(None, t, text).ratio() >= 0.75:
            return True
        if len(t) == len(text) >= 3 and sum(a != b for a, b in zip(t, text)) <= 1:  # 短句错一个字
            return True
    return False


@cap.event
def on_closed():
    print("聊天窗口关了，结束")


ctl = cap.start_free_threaded()
print(f"盯着{PF.name}中… Ctrl-C 结束")
try:
    while not ctl.is_finished():
        time.sleep(0.05)
        if unminimize(hwnd):
            print(f"{pf.name}被最小化了（系统不渲染最小化窗口，截不到）→ 已还原并压到最底层，别最小化，用别的窗口盖住就行")
        now = time.perf_counter()
        if state["pending"] is not None and (now - state["t"] > SETTLE or now - state["t0"] > MAX_WAIT):
            full, state["pending"] = state["pending"], None
            try:
                process(full)
            except Exception:
                traceback.print_exc()  # 一帧出错不退出
    ctl.wait()  # 采集线程若是报错死的，这里把错误抛出来，别静默结束
except KeyboardInterrupt:
    ctl.stop()
