import os
import re
import json
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify

from openai import OpenAI
from curl_cffi import requests as cffi_requests

app = Flask(__name__)

# ====================== CONFIG ======================
# Support both keys — Gemini preferred, XAI as fallback
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
XAI_API_KEY = os.environ.get("XAI_API_KEY")

AI_CLIENT = None
MODEL = None

if GEMINI_API_KEY:
    AI_CLIENT = OpenAI(
        api_key=GEMINI_API_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    MODEL = "gemini-3.1-flash-lite-preview"
    print(f"[OK] Using Gemini: {MODEL}")
elif XAI_API_KEY:
    AI_CLIENT = OpenAI(
        api_key=XAI_API_KEY,
        base_url="https://api.x.ai/v1"
    )
    MODEL = "grok-3-mini"
    print(f"[OK] Using XAI/Grok: {MODEL}")
else:
    print("[WARN] No API key set! Set GEMINI_API_KEY or XAI_API_KEY in Railway Environment Variables")

CACHE = Path("cache")
CACHE.mkdir(exist_ok=True)


def cache_key(url):
    return hashlib.md5(url.encode()).hexdigest()

def get_cache(url, prefix=""):
    p = CACHE / f"{prefix}{cache_key(url)}.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else None

def set_cache(url, data, prefix=""):
    p = CACHE / f"{prefix}{cache_key(url)}.json"
    p.write_text(json.dumps(data, ensure_ascii=False), "utf-8")


# ====================== FETCH ======================
def fetch(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8",
    }
    r = cffi_requests.get(url, headers=headers, impersonate="chrome", timeout=60)
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}")
    return BeautifulSoup(r.content, "lxml")


def is_all_page(url):
    path = urlparse(url).path.lower()
    return "all.html" in path or "all.htm" in path


# ====================== PARSE ALL.HTML ======================
TITLE_RE = re.compile(r'第[零一二三四五六七八九十百千万\d]{1,10}[章节回集卷]\s*[^\n]{0,60}')
NOISE_RE = re.compile(
    r"(推荐|收藏|上一[章页]|下一[章页]|目录|返回|广告|本站|书签|加入书架|"
    r"投票|打赏|www\.|\.com|\.net|http|最新章节|手机阅读|请牢记|备用域名)"
)


def parse_all_html(url):
    """Parse all.html → one big cache file with ALL chapters + content."""
    cached = get_cache(url, prefix="allhtml_")
    if cached:
        return cached

    soup = fetch(url)
    title_tag = soup.find("title")
    novel_title = title_tag.get_text(strip=True).split("_")[0].split("-")[0].strip() if title_tag else ""
    for tag in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        tag.decompose()

    content_el = None
    for sel in ["#content", "#all", "#at", ".content", "#BookText",
                "#chaptercontent", ".chapter-content", ".txtnav", "#txt"]:
        c = soup.select_one(sel)
        if c and len(c.get_text(strip=True)) > 500:
            content_el = c
            break
    if not content_el:
        best, best_len = None, 0
        for c in soup.find_all(["div", "article", "section"]):
            txt = c.get_text(strip=True)
            if len(txt) > best_len:
                best, best_len = c, len(txt)
        if best and best_len > 500:
            content_el = best
    if not content_el:
        return {"title": novel_title, "chapters": []}

    for br in content_el.find_all("br"):
        br.replace_with("\n")
    full_text = content_el.get_text(separator="\n")

    matches = list(TITLE_RE.finditer(full_text))
    if not matches:
        return {"title": novel_title, "chapters": []}

    chapters = []
    for i, m in enumerate(matches):
        title = m.group().strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        raw = full_text[start:end].strip()
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        lines = [l for l in lines if not NOISE_RE.search(l)]
        content = "\n\n".join(lines)
        if len(content) > 30:
            chapters.append({"title": title, "content": content})

    result = {"title": novel_title, "chapters": chapters}
    set_cache(url, result, prefix="allhtml_")
    return result


def get_allhtml_chapter_content(novel_url, chapter_index):
    """Read chapter content by index from the allhtml_ cache. No individual files needed."""
    data = get_cache(novel_url, prefix="allhtml_")
    if not data:
        return None
    chs = data.get("chapters", [])
    if 0 <= chapter_index < len(chs):
        return chs[chapter_index].get("content", "")
    return None


# ====================== STANDARD PARSING ======================
def get_chapters_standard(url):
    cached = get_cache(url, prefix="chapters_")
    if cached:
        return cached
    soup = fetch(url)
    selectors = ["#list a", ".listmain a", ".chapter-list a", ".mulu a",
                 "#chapterList a", "dd a", ".book-list a", ".chapters a",
                 "#dir a", ".book-chapter a", ".catalog li a", ".mu_contain a",
                 "#catalog a", ".centent a", "ul.mulu_list a"]
    links = []
    for sel in selectors:
        links.extend(soup.select(sel))
    chapters, seen = [], set()
    for a in links:
        href, title = a.get("href", ""), a.get_text(strip=True)
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
    for t in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        t.decompose()
    el = None
    for sel in ["#content", "#chaptercontent", ".chapter-content", "#BookText",
                ".read-content", ".txtnav", "#txt", ".book-content"]:
        c = soup.select_one(sel)
        if c and len(c.get_text(strip=True)) > 100:
            el = c
            break
    if not el:
        return ""
    for br in el.find_all("br"):
        br.replace_with("\n")
    lines = [l.strip() for l in el.get_text(separator="\n").split("\n") if l.strip()]
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
    if not AI_CLIENT:
        return mem
    try:
        r = AI_CLIENT.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": f"""Trích xuất JSON từ cặp Trung-Việt:
"characters" (Trung→Việt), "places", "terms", "summary" (1 câu Việt).
Đã biết: {json.dumps(mem.get('characters', {}), ensure_ascii=False)[:400]}
GỐC: {cn[:1500]}
DỊCH: {vn[:1500]}
CHỈ JSON."""}],
            temperature=0.1, max_tokens=1500)
        raw = re.sub(r'^```json\s*', '', r.choices[0].message.content.strip())
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
    if not AI_CLIENT:
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
            temperature=0.1, max_tokens=2000)
        raw = re.sub(r'^```json\s*', '', r.choices[0].message.content.strip())
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
    if not AI_CLIENT:
        return "[Lỗi] Chưa cấu hình API key. Vào Railway → Variables → thêm GEMINI_API_KEY hoặc XAI_API_KEY"
    ck = f"{chapter_url}__s_{style}" if chapter_url else ""
    if ck:
        c = get_cache(ck, prefix="tr_")
        if c:
            return c
    mem = get_memory(novel_url) if novel_url else {}
    mb = build_memory_block(mem, glossary)
    tone = custom_prompt.strip() if custom_prompt.strip() else STYLES.get(style, STYLES["nguyenban"])
    paras = text.split("\n\n")
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) > 5000 and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = cur + "\n\n" + p if cur else p
    if cur:
        chunks.append(cur)
    system_msg = ("Bạn là dịch giả tiểu thuyết Trung-Việt hàng đầu. "
                  "Dịch mượt mà, tự nhiên, truyền tải chính xác tinh thần nguyên tác. "
                  "Tên nhân vật/địa danh/thuật ngữ PHẢI theo bảng thuật ngữ nếu có.")
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
                messages=[{"role": "system", "content": system_msg},
                          {"role": "user", "content": prompt}],
                temperature=0.3, max_tokens=8000)
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


# ====================== TRANSLATE TITLE ======================
def translate_titles(titles, novel_url=""):
    """Batch translate chapter titles to Vietnamese."""
    ck = f"{novel_url}__titles"
    cached = get_cache(ck, prefix="titlevn_")
    if cached:
        return cached

    mem = get_memory(novel_url) if novel_url else {}
    mem_block = build_memory_block(mem)

    if not AI_CLIENT:
        return titles  # Return Chinese titles if no API key

    # Batch in groups of 80
    all_vn = []
    for start in range(0, len(titles), 80):
        batch = titles[start:start + 80]
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(batch))
        try:
            r = AI_CLIENT.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": f"""Dịch các tiêu đề chương tiểu thuyết sau sang tiếng Việt (phiên âm Hán-Việt cho tên riêng).
{mem_block}
Trả về ĐÚNG số dòng, mỗi dòng: số. tiêu đề tiếng Việt

{numbered}"""}],
                temperature=0.1, max_tokens=4000)
            raw = r.choices[0].message.content.strip()
            for line in raw.split("\n"):
                line = line.strip()
                if line and line[0].isdigit():
                    # Remove "1. " prefix
                    parts = line.split(".", 1)
                    if len(parts) > 1:
                        all_vn.append(parts[1].strip())
                    else:
                        all_vn.append(line)
                elif line:
                    all_vn.append(line)
        except Exception:
            all_vn.extend(batch)  # fallback to Chinese

    # Pad if translation returned fewer
    while len(all_vn) < len(titles):
        all_vn.append(titles[len(all_vn)])

    set_cache(ck, all_vn[:len(titles)], prefix="titlevn_")
    return all_vn[:len(titles)]


# ====================== ROUTES ======================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "model": MODEL or "NOT SET",
        "api_key": "set" if AI_CLIENT else "MISSING - set GEMINI_API_KEY or XAI_API_KEY",
    })


@app.route("/api/chapters", methods=["POST"])
def api_chapters():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "Vui lòng nhập URL"}), 400
    try:
        if is_all_page(url):
            data = parse_all_html(url)
            chs = data.get("chapters", [])
            if not chs:
                return jsonify({"error": "Không tìm thấy chương nào."}), 404
            # Return chapter list with index-based IDs (no individual cache files!)
            ch_list = [{"title": ch["title"], "url": f"{url}#ch_{i}"} for i, ch in enumerate(chs)]
            return jsonify({
                "chapters": ch_list,
                "novel_title": data.get("title", ""),
                "mode": "all_html",
                "novel_url": url,
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
                "novel_url": url,
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/init_memory", methods=["POST"])
def api_init_memory():
    d = request.json
    novel_url = d.get("novel_url", "").strip()
    if not novel_url:
        return jsonify({"error": "Thiếu URL"}), 400

    mem = get_memory(novel_url)
    if mem.get("n", 0) > 0 or mem.get("characters"):
        return jsonify({"status": "ready", "memory": _ms(mem)})

    # Get first chapter content — from allhtml_ cache if available
    text = ""
    if is_all_page(novel_url):
        text = get_allhtml_chapter_content(novel_url, 0) or ""
    else:
        fu = d.get("first_chapter_url", "")
        if fu:
            text = get_content_standard(fu) if fu else ""

    if text and len(text) > 100:
        mem = init_memory(novel_url, text)

    return jsonify({"status": "initialized", "memory": _ms(mem)})


@app.route("/api/translate_titles", methods=["POST"])
def api_translate_titles():
    """Translate chapter titles to Vietnamese for the index page."""
    d = request.json
    titles = d.get("titles", [])
    novel_url = d.get("novel_url", "")
    if not titles:
        return jsonify({"error": "Thiếu titles"}), 400
    vn = translate_titles(titles, novel_url)
    return jsonify({"titles_vi": vn})


@app.route("/api/translate", methods=["POST"])
def api_translate():
    d = request.json
    cu = d.get("url", "").strip()
    if not cu:
        return jsonify({"error": "Thiếu URL chương"}), 400

    nu = d.get("novel_url", "").strip()

    try:
        # KEY FIX: for all.html chapters, read directly from allhtml_ cache by index
        raw = None
        if "#ch_" in cu and nu:
            try:
                idx = int(cu.split("#ch_")[1])
                raw = get_allhtml_chapter_content(nu, idx)
            except (ValueError, IndexError):
                pass

        # Fallback for standard mode
        if not raw:
            raw = get_cache(cu, prefix="raw_")
        if not raw:
            raw = get_content_standard(cu)
        if not raw:
            return jsonify({"error": "Không tìm thấy nội dung chương"}), 500

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
    app.run(host="0.0.0.0", port=5000)
