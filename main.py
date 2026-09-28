"""
이재 수유 기록 서버 (FastAPI)
- POST /log     : 음성 문장을 받아서 뜻을 파악하고 저장
- GET  /summary : 베이비타임 입력 순서대로 요약
- GET  /events  : 저장된 원본 기록 전체
- GET  /today   : 베이비타임 옮겨 적기용 폰 화면 (입력 완료 체크)
"""
from datetime import datetime, timedelta, timezone
import hmac
import html
import os
import sqlite3

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

KST = timezone(timedelta(hours=9))  # 한국 시간
DB = os.environ.get("DB_PATH", "feeding.db")  # 기록 파일 위치 (서버에선 /data/feeding.db)

API_KEY = os.environ.get("API_KEY", "")  # 비밀 키 (서버의 .env 파일에 저장)

app = FastAPI(title="이재 수유 기록")


@app.middleware("http")
async def check_key(request: Request, call_next):
    """비밀 키가 있는 요청만 통과 (우리 가족만 쓰게 잠금)
    - 폰 매크로/단축어: 주소 뒤에 ?key=비밀키
    - 브라우저: 처음 한 번 ?key=비밀키 로 열면 쿠키로 기억 (1년)
    """
    if not API_KEY or request.url.path == "/health":  # 키 미설정(노트북) / 상태 확인은 통과
        return await call_next(request)
    from_query = request.query_params.get("key")
    given = request.headers.get("x-key") or from_query or request.cookies.get("key") or ""
    if not hmac.compare_digest(given, API_KEY):
        return PlainTextResponse("접근 권한이 없어요", status_code=401)
    response = await call_next(request)
    if from_query:
        response.set_cookie("key", API_KEY, max_age=60 * 60 * 24 * 365,
                            httponly=True, secure=True, samesite="lax")
    return response


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


# 서버 켜질 때 테이블 없으면 만들기
with db() as c:
    c.execute(
        """CREATE TABLE IF NOT EXISTS events(
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            ts   TEXT NOT NULL,   -- 기록 시각
            kind TEXT NOT NULL,   -- start / left_end / right_end / end / poop / pee / unknown
            raw  TEXT NOT NULL    -- 말한 문장 그대로
        )"""
    )
    # 베이비타임에 옮겨 적은 항목 체크용
    c.execute("CREATE TABLE IF NOT EXISTS done(event_id INTEGER PRIMARY KEY)")


class Log(BaseModel):
    text: str  # 폰에서 보내는 문장 (예: "왼쪽 다 먹었어")


LABEL = {
    "start": "수유 시작",
    "left_end": "왼쪽 끝",
    "right_end": "오른쪽 끝",
    "end": "수유 종료",
    "poop": "대변",
    "pee": "소변",
    "unknown": "알 수 없음",
}


def parse(text: str) -> str:
    """문장 → 이벤트 종류 (키워드 방식)"""
    t = text.replace(" ", "")
    if any(k in t for k in ["똥", "응가", "대변"]):
        return "poop"
    if any(k in t for k in ["쉬했", "쉬야", "소변"]):
        return "pee"
    done = any(k in t for k in ["다먹", "끝", "종료", "그만"])
    if "왼" in t and done:
        return "left_end"
    if "오른" in t and done:
        return "right_end"
    if done:
        return "end"
    if any(k in t for k in ["먹", "시작", "수유"]):
        return "start"
    return "unknown"


@app.post("/log")
def add_log(log: Log):
    kind = parse(log.text)
    now = datetime.now(KST)
    with db() as c:
        c.execute(
            "INSERT INTO events(ts, kind, raw) VALUES (?, ?, ?)",
            (now.isoformat(timespec="seconds"), kind, log.text),
        )
    msg = f"{now:%H:%M} {LABEL[kind]} 기록됨"
    if kind in ("left_end", "right_end", "end"):
        feeds = [it for it in build_items(6) if it["type"] == "모유"]
        if feeds and not feeds[-1]["ongoing"]:
            f = feeds[-1]
            msg += f" · 수유 완료 {f['minutes']}분 ({f['position']})"
    # MacroDroid 알림에 그대로 띄울 한 줄
    return {"message": msg, "kind": kind}


def _position(order):
    if not order:
        return "모름"
    seq = [s for i, s in enumerate(order) if i == 0 or s != order[i - 1]]
    if seq == ["왼"]:
        return "왼쪽"
    if seq == ["오"]:
        return "오른쪽"
    if seq == ["왼", "오"]:
        return "왼→오"
    if seq == ["오", "왼"]:
        return "오→왼"
    return "양쪽"


def _feeding_item(s, end_time, ongoing=False):
    total = s["left"] + s["right"]
    if end_time and total == 0:  # 좌우 말 안 하고 시작→종료만 한 경우
        total = round((end_time - s["start"]).total_seconds() / 60)
    return {
        "id": s["id"],
        "event_ids": s["ids"],
        "type": "모유",
        "time": f"{s['start']:%H:%M}",
        "minutes": total,
        "position": _position(s["order"]),
        "memo": f"왼{s['left']}/오{s['right']}" if s["order"] else "",
        "ongoing": ongoing,
    }


def build_items(hours: int = 24):
    """최근 N시간 원본 기록 → 베이비타임에 입력할 단위(수유 1회, 기저귀 1회)로 묶기"""
    since = (datetime.now(KST) - timedelta(hours=hours)).isoformat()
    with db() as c:
        rows = c.execute(
            "SELECT id, ts, kind, raw FROM events WHERE ts >= ? ORDER BY ts", (since,)
        ).fetchall()
        done = {r[0] for r in c.execute("SELECT event_id FROM done")}

    items, s = [], None
    for r in rows:
        t, k = datetime.fromisoformat(r["ts"]), r["kind"]
        if k == "start":
            if s:  # 종료 안 말하고 새로 시작하면 이전 건 마감
                items.append(_feeding_item(s, None))
            s = {"id": r["id"], "ids": [r["id"]], "start": t, "last": t,
                 "order": [], "left": 0, "right": 0}
        elif k in ("left_end", "right_end") and s:
            mins = round((t - s["last"]).total_seconds() / 60)
            side = "왼" if k == "left_end" else "오"
            s["left" if side == "왼" else "right"] += mins
            s["order"].append(side)
            s["ids"].append(r["id"])
            s["last"] = t
            if "왼" in s["order"] and "오" in s["order"]:  # 양쪽 다 먹으면 자동 종료
                items.append(_feeding_item(s, t))
                s = None
        elif k == "end" and s:
            s["ids"].append(r["id"])
            items.append(_feeding_item(s, t))
            s = None
        elif k in ("poop", "pee"):
            items.append({"id": r["id"], "event_ids": [r["id"]], "type": "기저귀",
                          "time": f"{t:%H:%M}", "kind": LABEL[k], "ongoing": False})
        elif k == "unknown":
            items.append({"id": r["id"], "event_ids": [r["id"]], "type": "확인 필요",
                          "time": f"{t:%H:%M}", "raw": r["raw"], "ongoing": False})
    if s:
        # 마지막 말 이후 1시간 넘게 아무 말 없으면 끝난 걸로 봄
        still = datetime.now(KST) - s["last"] < timedelta(hours=1)
        items.append(_feeding_item(s, None, ongoing=still))
    for it in items:
        it["done"] = it["id"] in done
    return items


@app.get("/summary")
def summary(hours: int = 24):
    """최근 N시간 기록을 베이비타임 입력 순서(시각 | 시간 | 위치 | 메모)로 정리"""
    lines = []
    for it in build_items(hours):
        if it["type"] == "모유":
            line = f"{it['time']} | 모유 {it['minutes']}분 | {it['position']} | 메모: {it['memo'] or '-'}"
            if it["ongoing"]:
                line += " (진행 중)"
        elif it["type"] == "기저귀":
            line = f"{it['time']} | 기저귀 | {it['kind']}"
        else:
            line = f"{it['time']} | 확인 필요 | \"{it['raw']}\""
        lines.append(line)
    return {"summary": lines}


@app.post("/done/{event_id}")
def toggle_done(event_id: int):
    """베이비타임에 옮겨 적었으면 체크 (다시 누르면 해제)"""
    with db() as c:
        if c.execute("SELECT 1 FROM done WHERE event_id=?", (event_id,)).fetchone():
            c.execute("DELETE FROM done WHERE event_id=?", (event_id,))
            return {"done": False}
        c.execute("INSERT INTO done(event_id) VALUES (?)", (event_id,))
        return {"done": True}


@app.post("/delete/{item_id}")
def delete_item(item_id: int):
    """카드 하나 삭제 (수유면 시작~끝 기록 전부)"""
    target = next((it for it in build_items(24 * 365) if it["id"] == item_id), None)
    ids = target["event_ids"] if target else [item_id]
    with db() as c:
        c.executemany("DELETE FROM events WHERE id=?", [(i,) for i in ids])
        c.execute("DELETE FROM done WHERE event_id=?", (item_id,))
    return {"deleted": ids}


@app.get("/today", response_class=HTMLResponse)
def today(hours: int = 24):
    """아침에 폰으로 열어서 보고 베이비타임에 옮겨 적는 화면"""
    cards = []
    for it in reversed(build_items(hours)):  # 최신이 위로
        cls = "card done" if it["done"] else "card"
        if it["type"] == "모유":
            rows = (
                f"<div class='row'><span>시작 시각</span><b>{it['time']}</b></div>"
                f"<div class='row'><span>수유 시간</span><b>{it['minutes']}분</b></div>"
                f"<div class='row'><span>먹인 위치</span><b>{it['position']}</b></div>"
                f"<div class='row'><span>메모</span><b>{it['memo'] or '-'}</b></div>"
            )
            title = "🍼 모유" + (" <em>진행 중</em>" if it["ongoing"] else "")
        elif it["type"] == "기저귀":
            rows = (
                f"<div class='row'><span>시각</span><b>{it['time']}</b></div>"
                f"<div class='row'><span>종류</span><b>{it['kind']}</b></div>"
            )
            title = "🧷 기저귀"
        else:
            rows = (
                f"<div class='row'><span>시각</span><b>{it['time']}</b></div>"
                f"<div class='row'><span>말한 내용</span><b>{html.escape(it['raw'])}</b></div>"
            )
            title = "❓ 못 알아들음"
        btn = "✅ 입력 완료" if it["done"] else "베이비타임에 입력했어요"
        cards.append(
            f"<div class='{cls}'><h2>{title}</h2>{rows}"
            f"<div class='btns'><button onclick='toggle({it['id']})'>{btn}</button>"
            f"<button class='del' onclick='del({it['id']})'>삭제</button></div></div>"
        )
    body = "".join(cards) or "<p class='empty'>기록이 없어요</p>"
    left = sum(1 for it in build_items(hours) if not it["done"])
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>이재 기록</title>
<style>
body{{font-family:sans-serif;background:#f3f4f8;margin:0;padding:16px;color:#222}}
header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}}
h1{{font-size:20px;margin:0}} .left{{background:#e88;color:#fff;border-radius:12px;padding:4px 10px;font-size:14px}}
.card{{background:#fff;border-radius:16px;padding:14px 16px;margin-bottom:12px;box-shadow:0 1px 3px #0001}}
.card.done{{opacity:.45}} h2{{font-size:17px;margin:0 0 8px}} em{{color:#e88;font-size:13px;font-style:normal}}
.row{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #eee;font-size:16px}}
.row span{{color:#888}} .row b{{font-size:18px}}
button{{width:100%;margin-top:10px;padding:12px;border:0;border-radius:12px;background:#3d5199;color:#fff;font-size:16px}}
.done button{{background:#999}}
.btns{{display:flex;gap:8px}} .btns button:first-child{{flex:1}}
button.del{{width:auto;padding:12px 16px;background:#fff;color:#d44;border:1px solid #d44}} .empty{{text-align:center;color:#888}}
</style></head><body>
<header><h1>이재 최근 {hours}시간</h1><span class="left">남은 입력 {left}건</span></header>
{body}
<script>
async function toggle(id){{await fetch('/done/'+id,{{method:'POST'}});location.reload();}}
async function del(id){{if(!confirm('이 기록을 삭제할까요?'))return;await fetch('/delete/'+id,{{method:'POST'}});location.reload();}}
</script></body></html>"""


@app.get("/health")
def health():
    """서버 살아있는지 확인용 (모니터링 서비스가 5분마다 찌름)"""
    with db() as c:
        c.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/events")
def events():
    with db() as c:
        rows = c.execute("SELECT * FROM events ORDER BY ts DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]
