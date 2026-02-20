from fastapi import FastAPI, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from urllib.parse import quote, urlparse, urljoin
from typing import Optional, List, Dict
from functools import lru_cache
from pathlib import Path
import re
import ipaddress
import socket
import urllib.request
import json

from database import SessionLocal, engine, Base
from models import RecipeLink

# =========================
# App
# =========================
app = FastAPI(title="Recipon")
Base.metadata.create_all(bind=engine)

# =========================
# Static (PWA)
# =========================
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Chrome devtoolsが勝手に叩くやつ（404が気になるなら黙らせる）
@app.get("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_dummy():
    return JSONResponse({})

# =========================
# 固定カテゴリ（かわいい寄せ）
# =========================
CATEGORIES: List[Dict[str, str]] = [
    {"key": "ごはん・丼", "emoji": "🍚"},
    {"key": "パスタ", "emoji": "🍝"},
    {"key": "麺", "emoji": "🍜"},
    {"key": "パン", "emoji": "🍞"},
    {"key": "お肉", "emoji": "🍖"},
    {"key": "お魚", "emoji": "🐟"},
    {"key": "卵・豆", "emoji": "🥚"},
    {"key": "おかず", "emoji": "🥗"},
    {"key": "サラダ", "emoji": "🥬"},
    {"key": "スープ", "emoji": "🍲"},
    {"key": "朝ごはん", "emoji": "🌅"},
    {"key": "お弁当", "emoji": "🍱"},
    {"key": "作り置き", "emoji": "🧊"},
    {"key": "おつまみ", "emoji": "🍺"},
    {"key": "スイーツ", "emoji": "🍰"},
    {"key": "おやつ", "emoji": "🍪"},
    {"key": "鍋", "emoji": "🫕"},
    {"key": "ドリンク", "emoji": "☕"},
    {"key": "その他", "emoji": "✨"},
]
CATEGORY_KEYS = {c["key"] for c in CATEGORIES}
DEFAULT_CATEGORY = "おかず"


# =========================
# DB
# =========================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# =========================
# Utils
# =========================
def h(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def normalize_spaces(s: str) -> str:
    return (s or "").replace("　", " ").strip()


def q(s: str) -> str:
    return quote(s or "")


def normalize_category(cat: str) -> str:
    cat = normalize_spaces(cat)
    if cat in CATEGORY_KEYS:
        return cat
    return "その他"


# =========================
# SSRF-ish safety
# =========================
def _is_safe_public_http_url(url: str) -> bool:
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        host = p.hostname
        if not host:
            return False
        if host in ("localhost",):
            return False

        ip = socket.gethostbyname(host)
        ip_obj = ipaddress.ip_address(ip)
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_reserved
            or ip_obj.is_multicast
        ):
            return False
        return True
    except Exception:
        return False


# =========================
# OGP image
# =========================
@lru_cache(maxsize=512)
def get_og_image(page_url: str) -> Optional[str]:
    if not page_url:
        return None
    page_url = page_url.strip()
    if not _is_safe_public_http_url(page_url):
        return None

    try:
        req = urllib.request.Request(
            page_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Recipon/0.1",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as res:
            ctype = (res.headers.get("Content-Type") or "").lower()
            if "text/html" not in ctype:
                return None
            raw = res.read(220_000)
            html = raw.decode("utf-8", errors="ignore")

        m = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            flags=re.IGNORECASE,
        )
        if not m:
            m = re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                html,
                flags=re.IGNORECASE,
            )

        if not m:
            m = re.search(
                r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)["\']',
                html,
                flags=re.IGNORECASE,
            )
            if not m:
                m = re.search(
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image(?::src)?["\']',
                    html,
                    flags=re.IGNORECASE,
                )

        if not m:
            return None

        img = (m.group(1) or "").strip()
        if not img:
            return None

        img = urljoin(page_url, img)
        if not img.startswith(("http://", "https://")):
            return None
        return img

    except Exception:
        return None


# =========================
# Dish name extraction helpers
# =========================
_NOISE_WORDS = [
    "レシピ", "作り方", "簡単", "人気", "おすすめ", "献立", "材料", "手順", "動画",
    "プロの", "定番", "料理", "キッチン",
]

_SPLIT_SEP_RE = re.compile(r"\s*(?:[｜|]|[-–—]|:|：|／|/)\s*")

_TAIL_RE = re.compile(
    r"\s*(?:by\s+\S+|By\s+\S+|【[^】]{1,40}】|\([^)]{1,40}\)|（[^）]{1,40}）)\s*$"
)

def clean_dish_title(raw: str) -> Optional[str]:
    if not raw:
        return None

    s = re.sub(r"\s+", " ", (raw or "")).strip()
    if not s:
        return None

    # まず「右側に付くサイト名」を切る
    s = _SPLIT_SEP_RE.split(s)[0].strip()

    # 末尾の括弧系を軽く複数回落とす
    for _ in range(2):
        ns = _TAIL_RE.sub("", s).strip()
        if ns == s:
            break
        s = ns

    # よくある語尾・語頭を落とす
    s = re.sub(r"レシピ$", "", s).strip()
    s = re.sub(r"^レシピ[:：]?\s*", "", s).strip()
    s = re.sub(r"作り方$", "", s).strip()
    s = re.sub(r"^作り方[:：]?\s*", "", s).strip()

    # ノイズワード（先頭/末尾だけ）を落とす
    for w in _NOISE_WORDS:
        s = re.sub(rf"^{re.escape(w)}\s*", "", s).strip()
        s = re.sub(rf"\s*{re.escape(w)}$", "", s).strip()

    # 末尾に付きがちな「〜さん」「〜のレシピ」系を雑に削る（破壊しすぎない程度）
    s = re.sub(r"\s+(さん|ちゃん|くん|氏)$", "", s).strip()

    # 記号整理
    s = s.strip(" -–—|｜:：/／").strip()

    if 2 <= len(s) <= 60:
        return s
    return None


def extract_recipe_name_from_jsonld(html: str) -> Optional[str]:
    # <script type="application/ld+json"> ... </script> を全部拾う
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        blob = (m.group(1) or "").strip()
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue

        candidates: List[dict] = []
        if isinstance(data, dict):
            candidates.append(data)
            g = data.get("@graph")
            if isinstance(g, list):
                candidates.extend([x for x in g if isinstance(x, dict)])
        elif isinstance(data, list):
            candidates.extend([x for x in data if isinstance(x, dict)])

        for obj in candidates:
            t = obj.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(isinstance(x, str) and x == "Recipe" for x in types):
                name = obj.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()

    return None


def extract_og_or_title(html: str) -> Optional[str]:
    # og:title
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        flags=re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
            html,
            flags=re.IGNORECASE,
        )

    # twitter:title
    if not m:
        m = re.search(
            r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            flags=re.IGNORECASE,
        )
        if not m:
            m = re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:title["\']',
                html,
                flags=re.IGNORECASE,
            )

    if m:
        t = (m.group(1) or "").strip()
        if t:
            return t

    # <title>
    mt = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if mt:
        t = re.sub(r"\s+", " ", (mt.group(1) or "").strip())
        if t:
            return t

    return None


# =========================
# Dish name (JSON-LD -> og:title -> <title>)
# =========================
@lru_cache(maxsize=512)
def get_og_title(page_url: str) -> Optional[str]:
    if not page_url:
        return None
    page_url = page_url.strip()
    if not _is_safe_public_http_url(page_url):
        return None

    try:
        req = urllib.request.Request(
            page_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Recipon/0.1",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as res:
            ctype = (res.headers.get("Content-Type") or "").lower()
            if "text/html" not in ctype:
                return None
            raw = res.read(240_000)
            html = raw.decode("utf-8", errors="ignore")

        # 1) JSON-LD (Recipe.name)
        title = extract_recipe_name_from_jsonld(html)

        # 2) OGP / twitter / title
        if not title:
            title = extract_og_or_title(html)

        # 3) Clean
        cleaned = clean_dish_title(title or "")
        if cleaned:
            return cleaned

        # fallback
        title = (title or "").strip()
        if 2 <= len(title) <= 80:
            return title
        return None

    except Exception:
        return None


# =========================
# 軽いカテゴリ推定（AIじゃない）
# =========================
def guess_category_from_text(text: str) -> str:
    t = (text or "").lower()

    if any(k in t for k in ["パスタ", "スパゲ", "カルボナーラ", "ボロネーゼ", "ペペロン"]):
        return "パスタ"
    if any(k in t for k in ["うどん", "そば", "ラーメン", "そうめん", "焼きそば", "麺"]):
        return "麺"
    if any(k in t for k in ["丼", "チャーハン", "炊き込み", "おにぎり", "カレー", "リゾット"]):
        return "ごはん・丼"
    if any(k in t for k in ["パン", "トースト", "サンド", "ホットサンド"]):
        return "パン"

    if any(k in t for k in ["ケーキ", "プリン", "パフェ", "タルト", "アイス", "ブラウニー", "クレープ"]):
        return "スイーツ"
    if any(k in t for k in ["クッキー", "ドーナツ", "マフィン", "スコーン", "おやつ"]):
        return "おやつ"

    if any(k in t for k in ["スープ", "味噌汁", "みそ汁", "ポタージュ", "シチュー"]):
        return "スープ"
    if any(k in t for k in ["鍋", "しゃぶ", "すき焼", "キムチ鍋", "もつ鍋"]):
        return "鍋"

    if "サラダ" in t:
        return "サラダ"

    if any(k in t for k in ["弁当", "お弁当"]):
        return "お弁当"
    if any(k in t for k in ["作り置き", "つくりおき", "常備菜"]):
        return "作り置き"
    if any(k in t for k in ["朝", "モーニング", "朝ごはん"]):
        return "朝ごはん"
    if any(k in t for k in ["つまみ", "おつまみ"]):
        return "おつまみ"

    if any(k in t for k in ["鶏", "豚", "牛", "ひき肉", "から揚げ", "唐揚げ", "ハンバーグ", "生姜焼"]):
        return "お肉"
    if any(k in t for k in ["鮭", "さけ", "サーモン", "鯖", "さば", "ぶり", "鯛", "あじ", "いわし"]):
        return "お魚"
    if any(k in t for k in ["卵", "たまご", "豆腐", "納豆", "大豆", "厚揚げ"]):
        return "卵・豆"

    return DEFAULT_CATEGORY


# =========================
# JS用: URL→料理名候補 + カテゴリ候補
# =========================
@app.get("/meta")
def meta(url: str = Query(...)):
    title = get_og_title(url)
    suggested = guess_category_from_text(title or "")
    return JSONResponse({"title": title, "category": suggested})


# =========================
# UI
# =========================
@app.get("/", response_class=HTMLResponse)
def index(
    category: Optional[str] = Query(default=None),
    msg: Optional[str] = Query(default=None),
    prefill_url: Optional[str] = Query(default=None),
    prefill_title: Optional[str] = Query(default=None),
    prefill_category: Optional[str] = Query(default=None),
    edit_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
):
    print("### INDEX HIT / app.py version = 2026-02-20-JSONLD-CLEAN-TITLE-PWA ###")

    filter_category = normalize_category(category) if category else None
    if category and filter_category == "その他" and category not in CATEGORY_KEYS:
        filter_category = None

    stmt = select(RecipeLink).order_by(RecipeLink.id.desc())
    if filter_category:
        stmt = stmt.where(RecipeLink.category == filter_category)
    items = db.execute(stmt).scalars().all()

    # dropdown options (選んだ瞬間に遷移)
    options = []
    selected_all = " selected" if not filter_category else ""
    options.append(f'<option value="/"{selected_all}>すべて</option>')
    for c in CATEGORIES:
        key = c["key"]
        selected = " selected" if filter_category == key else ""
        options.append(f'<option value="/?category={q(key)}"{selected}>{h(key)}</option>')

    # prefill
    prefill_category = normalize_category(prefill_category or "")
    if not prefill_category or prefill_category == "その他":
        prefill_category = DEFAULT_CATEGORY

    # toast
    toast = ""
    if msg == "ok":
        toast = "<div class='toast ok'>保存できたよ ✨</div>"
    elif msg == "dup":
        toast = "<div class='toast warn'>同じURLが登録済み（内容は変えなかった）</div>"
    elif msg == "upd":
        toast = "<div class='toast info'>登録済みURLだったから更新したよ</div>"
    elif msg == "del":
        toast = "<div class='toast info'>削除したよ</div>"
    elif msg == "editok":
        toast = "<div class='toast ok'>更新できたよ ✨</div>"

    # chips in sheet
    chip_html = []
    for c in CATEGORIES:
        key = c["key"]
        emoji = c["emoji"]
        selected_cls = " selected" if key == prefill_category else ""
        chip_html.append(
            f"<button type='button' class='chipbtn{selected_cls}' data-cat='{h(key)}' aria-pressed={'true' if key == prefill_category else 'false'}>"
            f"<span class='e'>{h(emoji)}</span><span class='t'>{h(key)}</span>"
            f"</button>"
        )

    # cards
    cards = []
    for it in items:
        og_img = get_og_image(it.url)
        zoom_cls = ""
        if og_img and ("kikkoman" in og_img.lower() or "kikkoman" in (it.url or "").lower()):
            zoom_cls = " zoom"

        if og_img:
            thumb = f"<img class='thumbimg{zoom_cls}' src='{h(og_img)}' alt=''>"
        else:
            thumb = "<div class='thumbph'>🍓</div>"

        # 編集UIはスマホでは要らないので出さない（残すならedit_idで表示、ただしdisplay:none）
        edit_block = ""
        if edit_id == it.id:
            edit_block = f"""
            <div class="editbox">
              <div class="edithead">編集（PC用）</div>
              <form method="post" action="/edit/{it.id}" class="editform">
                <label>URL（固定）
                  <input value="{h(it.url)}" disabled>
                </label>
                <label>料理名
                  <input name="title" value="{h(it.title)}" required>
                </label>
                <label>カテゴリ
                  <input name="category" value="{h(it.category)}" required>
                </label>
                <input type="hidden" name="current_filter" value="{h(filter_category or '')}">
                <div class="editactions">
                  <button class="btn primary" type="submit">更新</button>
                  <a class="btn ghost" href="/{('?category=' + q(filter_category)) if filter_category else ''}">キャンセル</a>
                </div>
              </form>
            </div>
            """

        cards.append(
            f"""
        <div class="card" tabindex="0" data-id="{it.id}" data-filter="{h(filter_category or '')}">
          <a class="thumb" href="{h(it.url)}" target="_blank" rel="noreferrer">
            {thumb}
          </a>
          <div class="cardpad">
            <a href="{h(it.url)}" target="_blank" rel="noreferrer" class="title">{h(it.title)}</a>
          </div>
          {edit_block}
        </div>
        """
        )

    # IMPORTANT: f-string内のCSS/JSの { } は全部 {{ }} にしてる
    html = f"""\
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Recipon</title>

  <!-- PWA -->
  <link rel="manifest" href="/static/manifest.json">
  <meta name="theme-color" content="#ff5fa2">
  <link rel="apple-touch-icon" href="/static/icon-192.png">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">

  <style>
    :root {{
      --bg1: #fff3fa;
      --bg2: #f4fbff;
      --card: rgba(255,255,255,.92);
      --text: #1f2430;
      --muted: #6b7280;
      --border: rgba(30, 41, 59, .10);
      --shadow: 0 14px 40px rgba(31,36,48,.10);
      --shadow2: 0 10px 26px rgba(31,36,48,.08);
      --pink: #ff5fa2;
      --pink2: #ff8cc4;
      --radius: 22px;
      --radius2: 16px;
    }}

    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-rounded, system-ui, -apple-system, Segoe UI, sans-serif;
      color: var(--text);
      background:
        radial-gradient(900px 520px at 10% 10%, var(--bg2), transparent 60%),
        radial-gradient(800px 540px at 90% 20%, #fff0f8, transparent 55%),
        linear-gradient(180deg, var(--bg1), #ffffff);
    }}

    .wrap {{
      max-width: 430px;
      margin: 0 auto;
      padding: 14px 14px 110px;
    }}

    input, select, button {{ font-size: 16px; }}

    .hero {{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap: 12px;
      margin: 8px 0 12px;
    }}
    .brand {{
      display:flex;
      align-items:center;
      gap: 10px;
      min-width: 0;
    }}
    .logo {{
      width: 44px; height: 44px;
      border-radius: 16px;
      display:flex; align-items:center; justify-content:center;
      background: linear-gradient(135deg, rgba(255,95,162,.22), rgba(74,163,255,.18));
      border: 1px solid rgba(255,95,162,.20);
      box-shadow: var(--shadow2);
      font-size: 22px;
      flex: 0 0 auto;
    }}
    h1 {{
      margin: 0;
      font-size: 26px;
      letter-spacing: .2px;
      line-height: 1;
      white-space: nowrap;
    }}

    .catselect {{
      border: 1px solid rgba(31,36,48,.12);
      border-radius: 999px;
      padding: 10px 12px;
      background: rgba(255,255,255,.75);
      box-shadow: 0 8px 22px rgba(0,0,0,.06);
      color: var(--muted);
      font-weight: 800;
      max-width: 180px;
    }}

    .toast {{
      margin: 10px 0 12px;
      padding: 12px 14px;
      border-radius: 16px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,.82);
      box-shadow: 0 12px 30px rgba(0,0,0,.06);
      font-size: 13px;
    }}
    .toast.ok {{
      border-color: rgba(55,208,176,.35);
      background: linear-gradient(0deg, rgba(55,208,176,.10), rgba(255,255,255,.86));
    }}
    .toast.info {{
      border-color: rgba(74,163,255,.30);
      background: linear-gradient(0deg, rgba(74,163,255,.10), rgba(255,255,255,.86));
    }}
    .toast.warn {{
      border-color: rgba(255,176,32,.35);
      background: linear-gradient(0deg, rgba(255,176,32,.10), rgba(255,255,255,.86));
    }}

    .listhead {{
      display:flex;
      align-items:center;
      justify-content:flex-start;
      gap:10px;
      margin: 8px 2px 8px;
    }}
    .listhead .count {{
      font-weight: 900;
      font-size: 13px;
    }}

    .cards {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 6px;
    }}

    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow2);
      backdrop-filter: blur(8px);
      display:flex;
      flex-direction:column;
      overflow:hidden;
      outline: none;
      transform: translateY(0);
      transition: transform .12s ease, box-shadow .15s ease, filter .15s ease;
      position: relative;
      -webkit-tap-highlight-color: transparent;
    }}

    .card.press {{
      filter: brightness(.97);
      transform: translateY(1px) scale(.997);
      box-shadow: var(--shadow);
    }}

    .thumb {{
      display:block;
      width:100%;
      aspect-ratio: 4 / 5;
      background: linear-gradient(135deg, rgba(255,95,162,.12), rgba(74,163,255,.12));
      text-decoration:none;
      overflow:hidden;
    }}
    .thumbimg {{
      width:100%;
      height:100%;
      object-fit: cover;
      display:block;
      transform: scale(1.00);
      transition: transform .15s ease;
    }}
    .thumbimg.zoom {{ transform: scale(1.35); }}

    .thumbph {{
      width:100%;
      height:100%;
      display:flex;
      align-items:center;
      justify-content:center;
      font-size: 34px;
      color: rgba(31,36,48,.55);
    }}

    .cardpad {{
      padding: 10px 12px 12px;
    }}

    .title {{
      font-weight: 900;
      text-decoration: none;
      color: #ff4da6;
      letter-spacing: .1px;
      line-height: 1.2;
      font-size: 13px;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}

    .empty {{
      color: var(--muted);
      padding: 16px;
    }}

    /* FAB */
    #fab {{
      position: fixed;
      right: 16px;
      bottom: 16px;
      width: 62px;
      height: 62px;
      border-radius: 999px;
      border: none;
      color: #fff;
      font-size: 30px;
      font-weight: 900;
      background: linear-gradient(135deg, var(--pink), var(--pink2));
      box-shadow: 0 16px 34px rgba(255,95,162,.38);
      z-index: 1000;
      display: flex;
      align-items: center;
      justify-content: center;
      line-height: 1;
      -webkit-tap-highlight-color: transparent;
    }}
    #fab:active {{ transform: scale(.97); }}

    /* Bottom sheet */
    .sheet {{
      position: fixed;
      inset: 0;
      background: rgba(10,10,10,.35);
      opacity: 0;
      pointer-events: none;
      transition: opacity .18s ease;
      z-index: 999;
    }}
    .sheet.open {{
      opacity: 1;
      pointer-events: auto;
    }}
    .sheet > .sheetpanel {{
      position: absolute;
      left: 0; right: 0; bottom: 0;
      background: rgba(255,255,255,.95);
      border-radius: 26px 26px 0 0;
      box-shadow: 0 -18px 40px rgba(0,0,0,.18);
      transform: translateY(110%);
      transition: transform .24s cubic-bezier(.2,.9,.2,1);
      padding: 12px 14px 16px;
      backdrop-filter: blur(10px);
    }}
    .sheet.open > .sheetpanel {{
      transform: translateY(0);
    }}
    .handle {{
      width: 46px;
      height: 5px;
      border-radius: 999px;
      background: rgba(0,0,0,.14);
      margin: 4px auto 10px;
    }}
    .sheethead {{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      margin-bottom: 10px;
    }}
    .sheettitle {{
      font-weight: 900;
      font-size: 16px;
    }}
    .xbtn {{
      width: 36px;
      height: 36px;
      border-radius: 999px;
      border: 1px solid rgba(31,36,48,.14);
      background: rgba(255,255,255,.88);
      font-size: 20px;
      line-height: 1;
      -webkit-tap-highlight-color: transparent;
    }}

    .sheetform {{
      display:grid;
      gap: 10px;
    }}
    .sheetform label {{
      display:grid;
      gap: 6px;
      font-size: 12px;
      color: #4b5563;
    }}
    .sheetform input {{
      width: 100%;
      padding: 14px 14px;
      border: 1px solid rgba(31,36,48,.16);
      border-radius: 16px;
      outline: none;
      background: rgba(255,255,255,.96);
      transition: box-shadow .15s ease, border-color .15s ease, transform .08s ease;
    }}
    .sheetform input:focus {{
      border-color: rgba(255,95,162,.55);
      box-shadow: 0 0 0 4px rgba(255,95,162,.16);
      transform: translateY(-1px);
    }}

    .chipgrid {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 2px;
      padding-bottom: 2px;
    }}
    .chipbtn {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 11px 14px;
      border-radius: 999px;
      border: 1px solid rgba(31,36,48,.14);
      background: rgba(255,255,255,.92);
      color: rgba(31,36,48,.92);
      box-shadow: 0 10px 22px rgba(0,0,0,.06);
      cursor: pointer;
      user-select: none;
      -webkit-tap-highlight-color: transparent;
      font-weight: 900;
      line-height: 1;
    }}
    .chipbtn .e {{ font-size: 16px; line-height: 1; }}
    .chipbtn .t {{ font-size: 14px; line-height: 1; white-space: nowrap; }}
    .chipbtn:active {{ transform: scale(.99); }}
    .chipbtn.selected {{
      border-color: rgba(255,95,162,.55);
      background: linear-gradient(135deg, rgba(255,95,162,.16), rgba(74,163,255,.12));
      box-shadow: 0 14px 28px rgba(255,95,162,.18);
    }}

    .tiny {{
      font-size: 11px;
      color: var(--muted);
      margin-top: -2px;
    }}

    .sheetSave {{
      width: 100%;
      border: none;
      border-radius: 16px;
      padding: 14px 14px;
      font-weight: 900;
      color: #fff;
      background: linear-gradient(135deg, var(--pink), var(--pink2));
      box-shadow: 0 14px 28px rgba(255,95,162,.26);
      -webkit-tap-highlight-color: transparent;
    }}

    body.noscroll {{ overflow: hidden; }}

    /* PCデバッグ用（普段は見えない） */
    .editbox {{
      margin: 0 12px 12px;
      padding: 12px;
      border: 1px dashed rgba(255,95,162,.30);
      border-radius: var(--radius2);
      background: rgba(255,255,255,.75);
      display: none;
    }}
    .edithead {{ font-weight: 900; margin-bottom: 10px; }}
    .editform {{ display: grid; gap: 10px; }}
    .editactions {{ display:flex; gap: 10px; align-items:center; }}
    .btn {{
      display:inline-flex; align-items:center; justify-content:center;
      padding: 10px 14px; border-radius: 999px;
      border: 1px solid rgba(31,36,48,.14);
      background: rgba(255,255,255,.92);
      font-weight: 800; text-decoration:none; color: inherit;
    }}
    .btn.primary {{
      border-color: rgba(255,95,162,.55);
      background: linear-gradient(135deg, rgba(255,95,162,.18), rgba(74,163,255,.12));
    }}
    .btn.ghost {{
      border-color: transparent;
      background: transparent;
      color: var(--muted);
      font-weight: 700;
    }}
  </style>
</head>

<body>
  <div class="wrap">
    <div class="hero">
      <div class="brand">
        <div class="logo">🍓</div>
        <h1>Recipon</h1>
      </div>

      <select class="catselect" onchange="location=this.value" aria-label="カテゴリ">
        {''.join(options)}
      </select>
    </div>

    {toast}

    <div class="listhead">
      <div class="count">一覧（{len(items)}件）</div>
    </div>

    <div class="cards">
      {''.join(cards) if cards else "<div class='empty'>まだ0件。右下の「＋」から追加してね ✨</div>"}
    </div>
  </div>

  <button id="fab" aria-label="追加">＋</button>

  <div id="sheet" class="sheet" aria-hidden="true">
    <div class="sheetpanel">
      <div class="handle"></div>

      <div class="sheethead">
        <div class="sheettitle">レシピ追加</div>
        <button type="button" id="sheetClose" class="xbtn" aria-label="閉じる">×</button>
      </div>

      <form method="post" action="/add" class="sheetform">
        <label>URL
          <input id="urlInput" name="url" value="{h(prefill_url or '')}" required placeholder="URLを貼るだけでOK">
        </label>

        <label>料理名
          <input id="dishInput" name="title" value="{h(prefill_title or '')}" required placeholder="例：ぶり大根">
        </label>

        <label>カテゴリ
          <input type="hidden" id="catValue" name="category" value="{h(prefill_category)}">
          <div class="chipgrid" id="chipGrid">
            {''.join(chip_html)}
          </div>
        </label>

        <div class="tiny">※URLを入れると料理名とカテゴリ候補を出すよ（外れたらタップで変更）</div>

        <button type="submit" class="sheetSave">保存する ✨</button>
      </form>
    </div>
  </div>

  <script>
    // PWA: Service Worker
    if ("serviceWorker" in navigator) {{
      navigator.serviceWorker.register("/static/sw.js").catch(() => {{}});
    }}

    const fab = document.getElementById("fab");
    const sheet = document.getElementById("sheet");
    const closeBtn = document.getElementById("sheetClose");
    const urlInput = document.getElementById("urlInput");
    const dishInput = document.getElementById("dishInput");

    const chipGrid = document.getElementById("chipGrid");
    const catValue = document.getElementById("catValue");

    let userTouchedCategory = false;

    function setCategory(cat, byUser) {{
      if (!cat) return;
      catValue.value = cat;

      const btns = chipGrid.querySelectorAll(".chipbtn");
      btns.forEach((b) => {{
        const v = b.getAttribute("data-cat");
        const on = (v === cat);
        b.classList.toggle("selected", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      }});

      if (byUser) userTouchedCategory = true;
    }}

    chipGrid.addEventListener("click", (e) => {{
      const btn = e.target.closest(".chipbtn");
      if (!btn) return;
      const cat = btn.getAttribute("data-cat");
      setCategory(cat, true);
    }});

    if (!catValue.value) {{
      setCategory("{h(DEFAULT_CATEGORY)}", false);
    }}

    function openSheet() {{
      sheet.classList.add("open");
      sheet.setAttribute("aria-hidden", "false");
      document.body.classList.add("noscroll");
      if (urlInput) setTimeout(() => urlInput.focus(), 50);
    }}

    function closeSheet() {{
      sheet.classList.remove("open");
      sheet.setAttribute("aria-hidden", "true");
      document.body.classList.remove("noscroll");
    }}

    fab.addEventListener("click", () => {{
      userTouchedCategory = false;
      openSheet();
    }});
    closeBtn.addEventListener("click", closeSheet);

    sheet.addEventListener("click", (e) => {{
      if (e.target === sheet) closeSheet();
    }});

    document.addEventListener("keydown", (e) => {{
      if (e.key === "Escape") closeSheet();
    }});

    let metaTimer = null;
    let lastUrl = "";

    async function fetchMeta(u) {{
      try {{
        const res = await fetch("/meta?url=" + encodeURIComponent(u));
        const data = await res.json();

        if (data && data.title) {{
          if (!dishInput.value) {{
            dishInput.value = data.title;
          }}
        }}

        if (data && data.category) {{
          if (!userTouchedCategory) {{
            setCategory(data.category, false);
          }}
        }}
      }} catch (e) {{
        // 失敗は無視（体験優先）
      }}
    }}

    function scheduleMetaFetch() {{
      if (!urlInput) return;
      const u = (urlInput.value || "").trim();
      if (!u) return;
      if (u === lastUrl) return;

      if (metaTimer) clearTimeout(metaTimer);
      metaTimer = setTimeout(() => {{
        lastUrl = u;
        fetchMeta(u);
      }}, 350);
    }}

    urlInput.addEventListener("input", scheduleMetaFetch);
    urlInput.addEventListener("blur", scheduleMetaFetch);

    window.addEventListener("load", () => {{
      const u = (urlInput.value || "").trim();
      if (u) {{
        if (!dishInput.value || !userTouchedCategory) {{
          fetchMeta(u);
        }}
      }}
    }});

    // -------------------------
    // 長押し削除（ボタン無し）
    // -------------------------
    const LONGPRESS_MS = 600;
    let pressTimer = null;
    let longPressed = false;
    let pressedCard = null;

    function clearPress() {{
      if (pressTimer) {{
        clearTimeout(pressTimer);
        pressTimer = null;
      }}
      if (pressedCard) {{
        pressedCard.classList.remove("press");
        pressedCard = null;
      }}
    }}

    function postDelete(id, currentFilter) {{
      const body = new URLSearchParams();
      body.set("current_filter", currentFilter || "");

      fetch("/delete/" + id, {{
        method: "POST",
        headers: {{
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"
        }},
        body: body.toString()
      }}).then(() => {{
        location.href = (currentFilter ? ("/?msg=del&category=" + encodeURIComponent(currentFilter)) : "/?msg=del");
      }}).catch(() => {{
        location.reload();
      }});
    }}

    function startLongPress(card) {{
      clearPress();
      longPressed = false;

      pressedCard = card;
      pressedCard.classList.add("press");

      const id = card.getAttribute("data-id");
      const currentFilter = card.getAttribute("data-filter") || "";

      pressTimer = setTimeout(() => {{
        longPressed = true;
        try {{ if (navigator.vibrate) navigator.vibrate(15); }} catch (e) {{}}

        const ok = confirm("削除しますか？");
        if (ok) {{
          postDelete(id, currentFilter);
        }}
      }}, LONGPRESS_MS);
    }}

    const cards = document.querySelectorAll(".card[data-id]");
    cards.forEach((card) => {{
      card.addEventListener("touchstart", () => {{
        startLongPress(card);
      }}, {{ passive: true }});

      card.addEventListener("touchend", () => {{
        clearPress();
      }});

      card.addEventListener("touchcancel", () => {{
        clearPress();
      }});

      card.addEventListener("touchmove", () => {{
        clearPress();
      }}, {{ passive: true }});

      // PCデバッグ用
      card.addEventListener("mousedown", () => {{
        startLongPress(card);
      }});
      card.addEventListener("mouseup", () => {{
        clearPress();
      }});
      card.addEventListener("mouseleave", () => {{
        clearPress();
      }});

      // 長押し後のクリック遷移だけ抑止
      card.addEventListener("click", (e) => {{
        if (longPressed) {{
          e.preventDefault();
          e.stopPropagation();
          longPressed = false;
        }}
      }}, true);
    }});
  </script>
</body>
</html>
"""
    return HTMLResponse(html)


# =========================
# CRUD
# =========================
@app.post("/add")
def add(
    url: str = Form(...),
    title: str = Form(...),
    category: str = Form(...),
    db: Session = Depends(get_db),
):
    url = normalize_spaces(url)
    title = normalize_spaces(title)
    category = normalize_category(category)

    existing = db.execute(select(RecipeLink).where(RecipeLink.url == url)).scalar_one_or_none()

    try:
        if existing:
            changed = False
            if title and existing.title != title:
                existing.title = title
                changed = True
            if category and existing.category != category:
                existing.category = category
                changed = True

            if changed:
                db.commit()
                return RedirectResponse(
                    url=(
                        f"/?msg=upd"
                        f"&prefill_url={q(url)}"
                        f"&prefill_title={q(title)}"
                        f"&prefill_category={q(category)}"
                    ),
                    status_code=303,
                )

            return RedirectResponse(
                url=(
                    f"/?msg=dup"
                    f"&prefill_url={q(url)}"
                    f"&prefill_title={q(title)}"
                    f"&prefill_category={q(category)}"
                ),
                status_code=303,
            )

        item = RecipeLink(url=url, title=title, category=category)
        db.add(item)
        db.commit()
        return RedirectResponse(url="/?msg=ok", status_code=303)

    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=(
                f"/?msg=dup"
                f"&prefill_url={q(url)}"
                f"&prefill_title={q(title)}"
                f"&prefill_category={q(category)}"
            ),
            status_code=303,
        )


@app.post("/edit/{item_id}")
def edit_item(
    item_id: int,
    title: str = Form(...),
    category: str = Form(...),
    current_filter: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    title = normalize_spaces(title)
    category = normalize_category(category)

    item = db.get(RecipeLink, item_id)
    if item:
        item.title = title
        item.category = category
        db.commit()

    if current_filter:
        current_filter = normalize_category(current_filter)
        return RedirectResponse(url=f"/?msg=editok&category={q(current_filter)}", status_code=303)
    return RedirectResponse(url="/?msg=editok", status_code=303)


@app.post("/delete/{item_id}")
def delete_item(
    item_id: int,
    current_filter: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    item = db.get(RecipeLink, item_id)
    if item:
        db.delete(item)
        db.commit()

    if current_filter:
        current_filter = normalize_category(current_filter)
        return RedirectResponse(url=f"/?msg=del&category={q(current_filter)}", status_code=303)
    return RedirectResponse(url="/?msg=del", status_code=303)