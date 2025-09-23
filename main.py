# -*- coding: utf-8 -*-
"""
ZEP '휴식 봇' — FastAPI + Playwright (최종본)
──────────────────────────────────────────────────────────────────────────────
핵심 기능
  • ZEP 방(게스트) 자동 입장 → 하단 채팅창에 메시지 전송
  • 채팅 '말풍선'을 스캔해 사용자의 명령을 감지:
      - "#휴식 10", "휴식 10", "휴식 10분", "#10", "10분 휴식하겠습니다"
      - "복귀했습니다" 류 멘트엔 짧게 응답(넵/복귀 확인 등)
  • ★ "이전 기록 재처리 금지" — 화면 Y좌표 기반으로 '새로 생긴 말풍선'만 처리
  • 서버 기동 시 기존 말풍선을 프리로드(Preload)하여 과거 대화 완전 무시
  • 전역 중복가드: 같은 '분' 명령 10초 내 1회만 수행
  • 전송 레이트리밋(“잠시 후에…” 토스트 대응), 직렬화, 백오프

실행 전 준비(.env)
  ZEP_PLAY_URL=https://zep.us/play/XXXXXX
  BOT_NAME=휴식 조교      (선택)

실행
  uvicorn main:app --reload --port 8000
API
  POST /say     {"text": "안녕하세요"}
  POST /break   {"minutes": 10, "who": "홍길동"}
  POST /command {"text": "#10"}  ← 서버가 파싱해서 타이머 시작
"""

import asyncio, os, re, time, random
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, Page

# ────────────────────────────────────────────────────────────────────────────
# 환경 변수/상수
# ────────────────────────────────────────────────────────────────────────────
load_dotenv()
ZEP_URL = os.getenv("ZEP_PLAY_URL")
BOT_NAME = os.getenv("BOT_NAME", "휴식 조교")
if not ZEP_URL:
    raise RuntimeError("ZEP_PLAY_URL(.env)이 비어 있습니다.")

DEBUG = True  # 문제 있을 때만 True로

# 중복/쿨다운 파라미터
SENDER_MSG_TTL       = 8      # (sender,msg) TTL (초)
MSG_ONLY_TTL         = 15     # msg-only TTL (초)
CMD_GUARD_TTL        = 15     # 같은 사람+분수 가드 TTL
RECENT_NAME_SEC      = 30     # 최근 닉네임 캐시 유지시간
GLOBAL_MINUTE_GUARD  = 10     # 전역 분수 가드: 같은 '분' 10초 내 1회만
MIN_SEND_INTERVAL    = 1.8    # 연속 전송 최소 간격(초)
MAX_RETRY            = 10     # 전송 재시도 최대 횟수
ENABLE_SCAN_LOOP     = True   # 채팅 스캔 on/off

# ────────────────────────────────────────────────────────────────────────────
# 셀렉터/정규식/템플릿
# ────────────────────────────────────────────────────────────────────────────
CHAT_INPUT_SEL     = 'input[placeholder="채팅을 입력해 주세요"], textarea[placeholder="채팅을 입력해 주세요"]'
BUBBLE_SEL         = 'div[data-sentry-element="NewBubbleContainer"]'
NICKNAME_INPUT_SEL = 'input[type="text"]'
ENTER_BUTTON_SEL   = 'button:has-text("Enter"), button:has-text("입장"), button:has-text("Start"), button:has-text("시작")'
COOLDOWN_TOAST     = 'div:has-text("잠시 후에 채팅을 입력할 수 있습니다")'

# 명령/자연어 패턴
CMD_RE       = re.compile(r'^#?\s*휴식\s*(\d+)\s*(?:분)?\s*$', re.I)                # "휴식 10", "#휴식 10", "휴식 10분"
SHORT_RE     = re.compile(r'^\s*#\s*(\d{1,3})\s*$', re.I)                          # "#10"
NATURAL_RE   = re.compile(r'^\s*(\d{1,3})\s*분\s*.*?(휴식|쉬)[가-힣\s\w]*$', re.I)    # "10분 휴식하겠습니다"
BACK_RE      = re.compile(r'복귀(했|함|요|완료)?', re.I)

# 안내/예시 문구, 시작/종료 멘트 등의 '잡음'을 무시
START_NOISE_RE = re.compile(
    r'(?:#?\s*휴식\s*\d+\s*분\s*시작\s*-\s*by)'  # 우리 봇이 보낸 시작 문구
    r'|(?:\d+\s*분\s*쉬어요)'                   # 우리 봇이 보낸 변형 시작 문구
    r'|(?:가즈아)'                              # 가즈아 포함 멘트
    r'|(?:처럼\s*입력(?:하면|해))'               # "처럼 입력하면/입력해" (안내문)
    r'|(?:입력하면\s*타이머)',                   # "입력하면 타이머" (안내문)
    re.I
)

START_TPL = [
    "#휴식 {m}분 시작 - by {who}",
    "⏱️ {m}분 쉬어요!! - {who}",
    "휴식 {m}분 가즈아~ 🙌 - {who}",
]
END_TPL = [
    "⏰휴식 종료 - {who}",
    "⏰끝! 다시 고! — {who}",
    "⏰휴식 {m}분 끝났어요. 파이팅 💪{who}✏️",
]
BACK_TPL = ["넵", "넵!!", "복귀 확인! 👋", "이제 공부 ㄱㄱ 🚀"]

def pick(seq):
    return random.choice(seq)

@dataclass
class Seen:
    key: str
    ts: float

# ────────────────────────────────────────────────────────────────────────────
# (유틸) 버블 주변에서 닉네임 추정 — DOM 구조 변경 대비
# ────────────────────────────────────────────────────────────────────────────
async def find_sender_near_bubble(page: Page, bubble_element):
    try:
        h = bubble_element
        for _ in range(4):
            prev = await h.evaluate_handle(
                """(e) => {
                    if (!e) return null;
                    let p = e.previousElementSibling;
                    if (p && p.innerText && p.innerText.trim()) return p;
                    const parent = e.parentElement;
                    if (!parent) return null;
                    let pp = parent.previousElementSibling;
                    if (pp && pp.innerText && pp.innerText.trim()) return pp;
                    return parent;
                }"""
            )
            if prev is None: break
            try:
                txt = (await prev.inner_text()).strip()
            except:
                txt = ""
            if txt and len(txt) <= 32 and not txt.startswith("#") and "분" not in txt and "휴식" not in txt:
                return txt
            h = prev
    except:
        pass
    return None

# ────────────────────────────────────────────────────────────────────────────
# (유틸) 명령 텍스트 → minutes 정규화
# ────────────────────────────────────────────────────────────────────────────
def normalize_cmd_text(t: str) -> Optional[int]:
    """
    다양한 입력을 하나의 '분' 값으로 정규화.
    허용: '#휴식 10', '휴식 10', '휴식 10분', '#10', '10분 휴식하겠습니다'
    무시: '#20분' (애매한 패턴 → 중복 유발), 숫자만("10")
    """
    s = t.strip()
    if s.isdigit():
        return None

    m = CMD_RE.match(s)
    if m:
        return int(m.group(1))

    m = SHORT_RE.match(s)
    if m:
        return int(m.group(1))

    m = NATURAL_RE.match(s)
    if m:
        return int(m.group(1))

    if re.match(r'^#\s*\d+\s*분$', s):
        return None

    return None

# ────────────────────────────────────────────────────────────────────────────
# Bot 본체
# ────────────────────────────────────────────────────────────────────────────
class BreakBot:
    def __init__(self, page: Page):
        self.page = page

        # 중복/가드 캐시
        self.trigger_seen: dict[str, float] = {}   # (sender,msg) TTL
        self.msg_seen: dict[str, float] = {}       # msg-only TTL
        self.cmd_guard: dict[str, float] = {}      # 같은 사람+분수 가드
        self.cmd_text_guard: dict[str, float] = {} # 같은 문장 가드
        self.minute_global_guard: dict[int, float] = {}  # 전역 분수 가드

        # 상태/락
        self._recent_sender: tuple[str, float] | None = None
        self._back_cooldown: float = 0.0
        self._send_lock = asyncio.Lock()
        self._last_send_ts = 0.0
        self._cooldown_until = 0.0

        # ★ ‘이전 기록 무시’용 커서: 마지막으로 처리한 말풍선의 Y좌표
        self.last_seen_y: float = 0.0

        self.running_timers: set[asyncio.Task] = set()

    # ── 안전 전송(레이트리밋 백오프 + 직렬화 + 최소 간격) ──
    async def type_and_send(self, text: str):
        async with self._send_lock:
            now = time.time()
            if now < self._cooldown_until:
                await asyncio.sleep(self._cooldown_until - now)

            gap = time.time() - self._last_send_ts
            if gap < MIN_SEND_INTERVAL:
                await asyncio.sleep(MIN_SEND_INTERVAL - gap)

            el = await self.page.query_selector(CHAT_INPUT_SEL)
            if not el:
                print("[WARN] 채팅 입력창을 찾을 수 없음:", CHAT_INPUT_SEL)
                return

            backoff = 0.8
            for _ in range(MAX_RETRY):
                # 쿨다운 토스트가 보이면 잠깐 대기
                try:
                    toast = await self.page.query_selector(COOLDOWN_TOAST)
                    if toast and await toast.is_visible():
                        self._cooldown_until = time.time() + backoff
                        await asyncio.sleep(backoff); backoff = min(backoff*1.6, 6.0)
                        continue
                except:
                    pass

                try:
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    try:
                        await el.fill("")
                    except:
                        # 일부 환경에서 .fill 실패할 경우 value를 직접 비움
                        await self.page.evaluate("(e)=>{e.value='';}", el)
                    await el.type(text)
                    await el.press("Enter")

                    # 전송 직후에도 토스트가 뜰 수 있으므로 한 번 더 확인
                    await asyncio.sleep(0.12)
                    toast = await self.page.query_selector(COOLDOWN_TOAST)
                    if toast and await toast.is_visible():
                        self._cooldown_until = time.time() + backoff
                        await asyncio.sleep(backoff); backoff = min(backoff*1.6, 6.0)
                        continue

                    self._last_send_ts = time.time()
                    await asyncio.sleep(0.05)
                    return
                except Exception as e:
                    if DEBUG: print("[SEND error]", e)
                    self._cooldown_until = time.time() + backoff
                    await asyncio.sleep(backoff); backoff = min(backoff*1.6, 6.0)

            print("[WARN] 전송 실패:", text)

    async def say(self, text: str):
        if DEBUG: print("[BOT SEND]", text)
        await self.type_and_send(text)

    # ── 공용: 휴식 시작 ──
    async def start_break(self, minutes: int, who: str = "누군가"):
        minutes = max(1, min(minutes, 180))

        # 전역 '분' 가드: 최근 GLOBAL_MINUTE_GUARD초 내 동일 분이면 무시
        now = time.time()
        if now - self.minute_global_guard.get(minutes, 0.0) < GLOBAL_MINUTE_GUARD:
            if DEBUG: print("[GLOBAL minute guard hit]", minutes)
            return {"ok": False, "reason": "minute-guard"}
        self.minute_global_guard[minutes] = now

        # 같은 사람+분수 가드(보조)
        gk = f"{who}::{minutes}"
        if now - self.cmd_guard.get(gk, 0.0) < CMD_GUARD_TTL:
            if DEBUG: print("[USER minute guard hit]", gk)
            return {"ok": False, "reason": "user-minute-guard"}
        self.cmd_guard[gk] = now

        start_text = random.choice(START_TPL).format(m=minutes, who=who)
        end_text   = random.choice(END_TPL).format(m=minutes, who=who)

        await self.say(start_text)

        async def timer():
            try:
                await asyncio.sleep(minutes * 60)
                await asyncio.sleep(random.uniform(0, 0.7))  # 자연스러운 딜레이
                await self.say(end_text)
            finally:
                self.running_timers.discard(asyncio.current_task())
        self.running_timers.add(asyncio.create_task(timer()))

        return {"ok": True}

    # ── 말풍선 1개 처리 ──
    async def handle_chat_item(self, text: str, sender: Optional[str]):
        t = text.strip()

        # 내 멘트/노이즈/숫자만은 즉시 무시
        if sender and sender.strip() == BOT_NAME.strip(): return
        if "시작 - by" in t or "종료 - " in t: return
        if START_NOISE_RE.search(t): return
        if t.isdigit(): return

        # 복귀 멘트(짧은 응답, 2초 쿨다운)
        if BACK_RE.search(t):
            now = time.time()
            if now >= self._back_cooldown:
                self._back_cooldown = now + 2
                await self.say(random.choice(BACK_TPL))
            return

        # 같은 문장 10초 가드
        text_key = re.sub(r'\s+', ' ', t.lower())
        if time.time() - self.cmd_text_guard.get(text_key, 0.0) < 10:
            return
        self.cmd_text_guard[text_key] = time.time()

        # 명령 정규화 → minutes
        minutes = normalize_cmd_text(t)
        if minutes is None:
            return
        minutes = max(1, min(minutes, 180))

        # 닉네임 보정(근 0.25초 내 최신 화자)
        if sender is None:
            await asyncio.sleep(0.25)
            if self._recent_sender and (time.time() - self._recent_sender[1] <= RECENT_NAME_SEC):
                sender = self._recent_sender[0]

        now = time.time()
        who = sender or (self._recent_sender[0] if (self._recent_sender and now - self._recent_sender[1] <= RECENT_NAME_SEC) else "누군가")

        await self.start_break(minutes, who=who)

    async def scan_loop(self):
        while True:
            try:
                bubbles = await self.page.query_selector_all(BUBBLE_SEL)

                for b in bubbles:
                    try:
                        # 이미 처리한 버블이면 스킵
                        seen = await b.get_attribute("data-bot-seen")
                        if seen == "1":
                            continue

                        content = (await b.inner_text()).strip()
                        # 바로 'seen' 찍어서 중복 방지 (내용이 비어도 처리 완료로 간주)
                        await b.evaluate("(el)=>el.setAttribute('data-bot-seen','1')")
                        if not content:
                            continue

                        # 닉네임/본문 분리
                        parts = [p.strip() for p in content.split("\n") if p.strip()]
                        sender, msg_text = None, content
                        if len(parts) >= 2 and (len(parts[0]) <= 32 and not parts[0].startswith("#")
                                                and "분" not in parts[0] and "휴식" not in parts[0]):
                            sender, msg_text = parts[0], "\n".join(parts[1:])
                        if not sender:
                            sender = await find_sender_near_bubble(self.page, b)

                        # 최근 화자 캐시
                        now = time.time()
                        if sender:
                            self._recent_sender = (sender, now)

                        # TTL 가드
                        k  = f"{(sender or 'unknown').strip()}::{msg_text.strip()}"
                        k2 = msg_text.strip().lower()
                        if now - self.trigger_seen.get(k, 0.0) < SENDER_MSG_TTL:   continue
                        if now - self.msg_seen.get(k2, 0.0)   < MSG_ONLY_TTL:     continue
                        self.trigger_seen[k] = now
                        self.msg_seen[k2]    = now

                        if DEBUG: print("[MSG]", sender or "(None)", "|", msg_text)
                        await self.handle_chat_item(msg_text, sender)

                    except Exception as e:
                        if DEBUG: print("[scan item err]", e)

            except Exception as e:
                if DEBUG: print("[scan loop err]", e)

            await asyncio.sleep(1.0)

# ────────────────────────────────────────────────────────────────────────────
# ZEP 방 입장(게스트)
# ────────────────────────────────────────────────────────────────────────────
async def enter_as_guest(page: Page):
    await page.goto(ZEP_URL, wait_until="domcontentloaded")
    try:
        nick = await page.wait_for_selector(NICKNAME_INPUT_SEL, timeout=6000)
        await nick.fill(BOT_NAME)
    except:
        pass
    try:
        btn = await page.query_selector(ENTER_BUTTON_SEL)
        if btn: await btn.click()
    except:
        pass
    await page.wait_for_timeout(3000)
    await page.wait_for_selector(CHAT_INPUT_SEL, timeout=15000)

# ────────────────────────────────────────────────────────────────────────────
# (중요) 서버 기동 시, ‘현재 화면의 최하단 버블’ Y좌표를 커서로 저장
#       → 과거 말풍선은 완전히 무시되고, 이후 더 아래로 생기는 버블만 처리
# ────────────────────────────────────────────────────────────────────────────
async def preload_mark_seen(bot: BreakBot):
    """현재 화면에 이미 떠있는 말풍선은 data-bot-seen=1로 마킹해서 완전 무시"""
    bubbles = await bot.page.query_selector_all(BUBBLE_SEL)
    cnt = 0
    for b in bubbles:
        try:
            await b.evaluate("(el)=>el.setAttribute('data-bot-seen','1')")
            cnt += 1
        except:
            pass
    print(f"[INIT] 기존 버블 {cnt}개를 'seen' 처리했습니다.")

# ────────────────────────────────────────────────────────────────────────────
# FastAPI 앱/수명주기
# ────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="ZEP Break Bot API")

class SayReq(BaseModel):
    text: str = Field(..., min_length=1)

class BreakReq(BaseModel):
    minutes: int = Field(..., ge=1, le=180)
    who: Optional[str] = None

class CommandReq(BaseModel):
    text: str = Field(..., min_length=1)

bot: Optional[BreakBot] = None
_playwright = None
_browser = None
_page: Optional[Page] = None
_scan_task: Optional[asyncio.Task] = None

@app.on_event("startup")
async def on_startup():
    global _playwright, _browser, _page, bot, _scan_task
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(headless=False)
    _page = await _browser.new_page()
    await enter_as_guest(_page)

    bot = BreakBot(_page)

    # 기존: await preload_cursor(bot)
    await preload_mark_seen(bot)

    # (안내 멘트 – 자연어 정규식에 안 걸리게 '예)' 형태 사용)
    await bot.say("안녕하세요 13기 친구들!! "
                  "예) #휴식 OO분 / "
                  "예) #OO / "
                  "예) OO분-휴식 / "
                  "예) OO분 휴식하겠습니다 ← 이런 식으로 보내주시면 타이머가 시작돼요.")

    if ENABLE_SCAN_LOOP:
        _scan_task = asyncio.create_task(bot.scan_loop())

@app.on_event("shutdown")
async def on_shutdown():
    global _playwright, _browser, _page, _scan_task
    try:
        if _scan_task: _scan_task.cancel()
        if _browser: await _browser.close()
        if _playwright: await _playwright.stop()
    except:
        pass

# ────────────────────────────────────────────────────────────────────────────
# HTTP API
# ────────────────────────────────────────────────────────────────────────────
@app.post("/say")
async def api_say(req: SayReq):
    assert bot is not None
    await bot.say(req.text)
    return {"ok": True}

@app.post("/break")
async def api_break(req: BreakReq):
    assert bot is not None
    who = req.who or "누군가"
    result = await bot.start_break(req.minutes, who=who)
    return {"ok": True, "started": result.get("ok", False)}

@app.post("/command")
async def api_command(req: CommandReq):
    """
    서버측 파서: "#휴식 10" / "#10" / "10분 휴식하겠습니다" 등을 minutes로 정규화한 뒤 실행.
    """
    assert bot is not None
    minutes = normalize_cmd_text(req.text)
    if minutes is None:
        return {"ok": False, "reason": "no-match-or-ignored"}
    minutes = max(1, min(minutes, 180))
    await bot.start_break(minutes, who="API")
    return {"ok": True, "type": "break", "minutes": minutes}