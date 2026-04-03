import os
import re
import json
import time
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify

from openai import OpenAI
from curl_cffi import requests as cffi_requests

print("=== APP.PY ĐANG KHỞI ĐỘNG ===")

app = Flask(__name__)

# ====================== CONFIG ======================
try:
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    print(f"GEMINI_API_KEY: {'✅ Có' if GEMINI_API_KEY else '❌ KHÔNG CÓ'}")

    if not GEMINI_API_KEY:
        raise Exception("Thiếu GEMINI_API_KEY trong Environment Variables")

    AI_CLIENT = OpenAI(
        api_key=GEMINI_API_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    MODEL = "gemini-3.1-flash-lite-preview"
    print(f"✅ Model: {MODEL} - OpenAI client OK")

except Exception as e:
    print("🚨 LỖI KHỞI ĐỘNG:")
    print(str(e))
    raise

# ====================== NOISE FILTER ======================
NOISE_RE = re.compile(
    r"(推荐|收藏|上一[章页]|下一[章页]|目录|返回|广告|本站|书签|加入书架|"
    r"投票|打赏|www\.|\.com|\.net|http|最新章节|手机阅读|请牢记|备用域名|"
    r"69书吧|书吧|设置|白天|下一章|上一章|书签|收藏)"
)

# ====================== CACHE ======================
CACHE = Path("cache")
CACHE.mkdir(exist_ok=True, parents=True)
print(f"✅ Cache folder: {CACHE.absolute()}")

def cache_key(url):
    return hashlib.md5(url.encode()).hexdigest()

def get_cache(url, prefix=""):
    p = CACHE / f"{prefix}{cache_key(url)}.json"
    if p.exists():
        return json.loads(p.read_text("utf-8"))
    return None

def set_cache(url, data, prefix=""):
    p = CACHE / f"{prefix}{cache_key(url)}.json"
    p.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

# ====================== FETCH (ANTI-503) ======================
def fetch(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.69shuba.com/",
    }
    r = cffi_requests.get(url, headers=headers, impersonate="chrome", timeout=60)
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}")
    time.sleep(0.8)  # Giảm rate limit
    return BeautifulSoup(r.content, "lxml")

def is_all_page(url):
    path = urlparse(url).path.lower()
    return "all.html" in path or "all.htm" in path

# ====================== PARSE ALL.HTML (69SHUBA SUPPORT) ======================
def parse_all_html(url):
    cached = get_cache(url, prefix="allhtml_")
    if cached:
        return cached

    soup = fetch(url)

    title_tag = soup.find("title")
    novel_title = ""
    if title_tag:
        novel_title = title_tag.get_text(strip=True).split("_")[0].split("-")[0].strip()

    # === 1. LẤY DANH SÁCH CHƯƠNG TỪ all.html ===
    chapters = []
    seen = set()

    # Tìm tất cả link chứa "章"
    for a in soup.find_all("a"):
        href = a.get("href", "").strip()
        title = a.get_text(strip=True).strip()
        if href and title and re.search(r'第.*[章回]', title):
            full_url = urljoin(url, href)
            if full_url not in seen:
                seen.add(full_url)
                chapters.append({"title": title, "url": full_url})

    if len(chapters) < 5:
        # Fallback selectors
        for sel in ["li a", ".list a", "dd a", "ul a"]:
            for a in soup.select(sel):
                href = a.get("href", "").strip()
                title = a.get_text(strip=True).strip()
                if href and title and "章" in title:
                    full_url = urljoin(url, href)
                    if full_url not in seen:
                        seen.add(full_url)
                        chapters.append({"title": title, "url": full_url})

    result = {"title": novel_title, "chapters": chapters}
    set_cache(url, result, prefix="allhtml_")
    return result

# ====================== STANDARD FUNCTIONS ======================
def get_chapters_standard(url):
    cached = get_cache(url, prefix="chapters_")
    if cached:
        return cached

    soup = fetch(url)
    selectors = [
        "#list a", ".listmain a", ".chapter-list a", ".mulu a",
        "#chapterList a", "dd a", ".book-list a", ".chapters a",
    ]
    links = []
    for sel in selectors:
        links.extend(soup.select(sel))

    chapters = []
    seen = set()
    for a in links:
        href = a.get("href", "")
        title = a.get_text(strip=True)
        if href and title and len(title) > 2 and re.search(r'[\u4e00-\u9fff]', title):
            full = urljoin(url, href)
            if full not in seen:
                seen.add(full)
                chapters.append({"title": title, "url": full})

    set_cache(url, chapters, prefix="chapters_")
    return chapters

def get_content_standard(url):
    cached = get_cache(url, prefix="raw_")
    if cached:
        return cached

    soup = fetch(url)

    # 69SHUBA SPECIAL PARSING
    if "69shuba.com" in url or "69read.net" in url:
        el = soup.select_one(".txtnav")
        if el:
            for bad in el.select(".txtinfo, .yueduad1, .bottom-ad, .bottom-ad2, .page1, #txtright, .tools, script, style, header, footer, nav"):
                bad.decompose()
            if el.find("h1"):
                el.find("h1").decompose()

            for br in el.find_all("br"):
                br.replace_with("\n")

            text = el.get_text(separator="\n")
            lines = [l.strip() for l in text.split("\n") if l.strip()]

            cleaned = [l for l in lines if not NOISE_RE.search(l) and not any(x in l for x in ["69书吧","上一章","下一章","目录","书签","收藏"])]
            final = "\n\n".join(cleaned)

            if len(final) > 100:
                set_cache(url, final, prefix="raw_")
                return final

    # FALLBACK
    for t in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        t.decompose()

    el = None
    for sel in ["#content", "#chaptercontent", ".chapter-content", "#BookText", ".txtnav", "#txt"]:
        c = soup.select_one(sel)
        if c and len(c.get_text(strip=True)) > 100:
            el = c
            break

    if not el:
        return ""

    for br in el.find_all("br"):
        br.replace_with("\n")

    text = el.get_text(separator="\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    cleaned = [l for l in lines if not NOISE_RE.search(l)]
    final = "\n\n".join(cleaned)

    set_cache(url, final, prefix="raw_")
    return final

# ====================== MEMORY ======================
def get_memory(url):
    m = get_cache(url, prefix="memory_")
    return m or {"characters": {}, "places": {}, "terms": {}, "summary": "", "n": 0}

def save_memory(url, m):
    set_cache(url, m, prefix="memory_")

def build_memory_block(mem, glossary=""):
    terms = {}
    for d in [mem.get("characters", {}), mem.get("places", {}), mem.get("terms", {})]:
        terms.update(d)
    if glossary.strip():
        for line in glossary.strip().split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                terms[k.strip()] = v.strip()
    parts = []
    if terms:
        parts.append("【THUẬT NGỮ BẮT BUỘC】\n" + "\n".join(f"  {k} = {v}" for k, v in terms.items()))
    if mem.get("summary"):
        parts.append(f"【BỐI CẢNH】 {mem['summary']}")
    return "\n\n".join(parts)

def extract_memory(cn, vn, mem, url):
    try:
        r = AI_CLIENT.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": f"""Trích xuất JSON từ cặp Trung-Việt:
"characters" (Trung→Việt), "places", "terms", "summary" (1 câu Việt).
Đã biết: {json.dumps(mem.get('characters', {}), ensure_ascii=False)[:400]}
GỐC: {cn[:1500]}
DỊCH: {vn[:1500]}
CHỈ JSON."""}],
            temperature=0.1,
            max_tokens=1500,
        )
        raw = r.choices[0].message.content.strip()
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        d = json.loads(raw)
        for k in ["characters", "places", "terms"]:
            mem.setdefault(k, {}).update(d.get(k, {}))
        mem["summary"] = d.get("summary", mem.get("summary", ""))
        mem["n"] = mem.get("n", 0) + 1
        save_memory(url, mem)
    except Exception as e:
        print(f"Memory extract error: {e}")
    return mem

def init_memory(url, text):
    mem = get_memory(url)
    if mem.get("n", 0) > 0 or mem.get("characters"):
        return mem
    try:
        r = AI_CLIENT.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": f"""Phân tích chương đầu tiểu thuyết TQ. Trích xuất JSON:
"characters" (Trung→phiên âm Hán-Việt)
"places" (Trung→Hán-Việt)
"terms" (thuật ngữ đặc biệt Trung→Hán-Việt)
"genre" (thể loại)
"summary" (bối cảnh 1 câu Việt)
"novel_title_vi" (tên truyện Hán-Việt)

{text[:4000]}

CHỈ JSON."""}],
            temperature=0.1,
            max_tokens=2000,
        )
        raw = r.choices[0].message.content.strip()
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        d = json.loads(raw)
        for k in ["characters", "places", "terms"]:
            mem[k] = d.get(k, {})
        mem["genre"] = d.get("genre", "")
        mem["novel_title_vi"] = d.get("novel_title_vi", "")
        mem["summary"] = d.get("summary", "")
        mem["n"] = 0
        save_memory(url, mem)
    except Exception as e:
        print(f"Memory init error: {e}")
    return mem

# ====================== TRANSLATE ======================
STYLES = {
    "cotrang": "Dịch phong cách cổ trang, ngôn ngữ trang trọng, giàu hình ảnh kiếm hiệp.",
    "hiendai": "Dịch tự nhiên, hiện đại, dễ đọc, giọng văn gần gũi.",
    "langman": "Dịch lãng mạn, trữ tình, giàu cảm xúc.",
    "satnghia": "Dịch sát nghĩa, chính xác từng câu.",
    "nguyenban": "Giữ nguyên phong cách gốc, cân bằng chính xác và tự nhiên.",
}

def translate(text, glossary="", style="nguyenban", custom_prompt="",
             chapter_url="", novel_url=""):
    if not text.strip():
        return "Không có nội dung."

    ck = f"{chapter_url}__s_{style}" if chapter_url else ""
    if ck:
        c = get_cache(ck, prefix="tr_")
        if c:
            return c

    mem = get_memory(novel_url) if novel_url else {}
    mb = build_memory_block(mem, glossary)
    tone = custom_prompt.strip() if custom_prompt.strip() else STYLES.get(style, STYLES["nguyenban"])

    paras = text.split("\n\n")
    chunks = []
    cur = ""
    for p in paras:
        if len(cur) + len(p) > 5000 and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = cur + "\n\n" + p if cur else p
    if cur:
        chunks.append(cur)

    system_msg = (
        "Bạn là dịch giả tiểu thuyết Trung-Việt hàng đầu. "
        "Dịch mượt mà, tự nhiên, truyền tải chính xác tinh thần nguyên tác. "
        "Tên nhân vật/địa danh/thuật ngữ PHẢI theo bảng thuật ngữ nếu có."
    )

    results = []
    for i, chunk in enumerate(chunks):
        prompt = f"""Dịch sang tiếng Việt.
PHONG CÁCH: {tone}

{mb}

QUY TẮC:
1. Dịch TOÀN BỘ, không bỏ sót.
2. Tên riêng phiên âm Hán-Việt nhất quán theo bảng thuật ngữ.
3. Giữ nguyên phân đoạn. Đối thoại giữ dấu ngoặc kép.
4. Văn phong mượt mà như tiểu thuyết Việt.
5. CHỈ trả về bản dịch, không giải thích.

=== VĂN BẢN ===
{chunk}
=== HẾT ==="""

        try:
            response = AI_CLIENT.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=8000,
            )
            results.append(response.choices[0].message.content.strip())
        except Exception as e:
            results.append(f"[Lỗi dịch {i + 1}: {str(e)}]")

    final = "\n\n".join(results)

    if ck:
        set_cache(ck, final, prefix="tr_")

    if novel_url:
        try:
            extract_memory(text, final, mem, novel_url)
        except Exception:
            pass

    return final

# ====================== ROUTES ======================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/chapters", methods=["POST"])
def api_chapters():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "Vui lòng nhập URL"}), 400
    try:
        if is_all_page(url):
            data = parse_all_html(url)
            ch_list = []
            for ch in data["chapters"]:
                ch_list.append({"title": ch["title"], "url": ch["url"]})
            return jsonify({
                "chapters": ch_list,
                "novel_title": data.get("title", ""),
                "mode": "all_html",
            })
        else:
            chs = get_chapters_standard(url)
            if not chs:
                return jsonify({"error": "Không tìm thấy mục lục."}), 404
            title = ""
            try:
                soup = fetch(url)
                t = soup.find("title")
                if t:
                    title = t.get_text(strip=True).split("_")[0].split("-")[0].strip()
            except Exception:
                pass
            return jsonify({
                "chapters": chs,
                "novel_title": title,
                "mode": "standard",
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/init_memory", methods=["POST"])
def api_init_memory():
    d = request.json
    url = d.get("novel_url", "").strip()
    if not url:
        return jsonify({"error": "Thiếu URL"}), 400

    mem = get_memory(url)
    if mem.get("n", 0) > 0 or mem.get("characters"):
        return jsonify({"status": "ready", "memory": _ms(mem)})

    text = ""
    fu = d.get("first_chapter_url", "")
    if fu:
        text = get_cache(fu, prefix="raw_") or ""
    if text and len(text) > 100:
        mem = init_memory(url, text)

    return jsonify({"status": "initialized", "memory": _ms(mem)})


@app.route("/api/translate", methods=["POST"])
def api_translate():
    d = request.json
    cu = d.get("url", "").strip()
    if not cu:
        return jsonify({"error": "Thiếu URL chương"}), 400

    try:
        raw = get_cache(cu, prefix="raw_")
        if not raw:
            raw = get_content_standard(cu)
        if not raw:
            return jsonify({"error": "Không tìm thấy nội dung chương"}), 500

        nu = d.get("novel_url", "").strip()
        vn = translate(raw, d.get("glossary", ""), d.get("style", "nguyenban"),
                       d.get("custom_prompt", ""), cu, nu)
        mem = get_memory(nu) if nu else {}
        return jsonify({"translation": vn, "memory": _ms(mem)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _ms(m):
    return {
        "characters": len(m.get("characters", {})),
        "places": len(m.get("places", {})),
        "terms": len(m.get("terms", {})),
        "n": m.get("n", 0),
        "genre": m.get("genre", ""),
        "novel_title_vi": m.get("novel_title_vi", ""),
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Server running on port {port}")
    app.run(host="0.0.0.0", port=port)
