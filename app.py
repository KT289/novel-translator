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

app = Flask(__name__)

# ====================== CONFIG ======================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise Exception("Thiếu GEMINI_API_KEY trong Environment Variables")

AI_CLIENT = OpenAI(
    api_key=GEMINI_API_KEY,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)
MODEL = "gemini-3.1-flash-lite"

CACHE = Path("cache")
CACHE.mkdir(exist_ok=True)


# ====================== CACHE HELPERS ======================
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


# ====================== FETCH ======================
def fetch(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8",
    }
    r = cffi_requests.get(url, headers=headers, impersonate="chrome", timeout=30)
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}")
    return BeautifulSoup(r.content, "lxml")


# ====================== DETECT PAGE TYPE ======================
def is_all_page(url):
    """Check if URL is an 'all chapters on one page' URL."""
    path = urlparse(url).path.lower()
    return "all.html" in path or "all.htm" in path or "/all" in path


# ====================== PARSE ALL.HTML ======================
CHAPTER_RE = re.compile(
    r'^(第[零一二三四五六七八九十百千万\d]+[章节回集]\s*.+)$'
)

def parse_all_html(url):
    """Parse an all.html page: extract ALL chapters + content from one page."""
    cached = get_cache(url, prefix="allhtml_")
    if cached:
        return cached

    soup = fetch(url)

    # Extract novel title from page
    title_tag = soup.find("title")
    novel_title = ""
    if title_tag:
        novel_title = title_tag.get_text(strip=True).split("_")[0].split("-")[0].strip()

    # Remove noise
    for tag in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        tag.decompose()

    # Find the content area
    content_el = None
    for sel in ["#content", "#all", "#at", ".content", "#BookText",
                "#chaptercontent", ".chapter-content", ".txtnav", "#txt"]:
        candidate = soup.select_one(sel)
        if candidate and len(candidate.get_text(strip=True)) > 500:
            content_el = candidate
            break

    if not content_el:
        # Fallback: largest text block
        best, best_len = None, 0
        for c in soup.find_all(["div", "article", "section"]):
            txt = c.get_text(strip=True)
            if len(txt) > best_len:
                best, best_len = c, len(txt)
        if best and best_len > 500:
            content_el = best

    if not content_el:
        return {"title": novel_title, "chapters": []}

    # Handle <br> → newline
    for br in content_el.find_all("br"):
        br.replace_with("\n")

    full_text = content_el.get_text(separator="\n")
    lines = [l.strip() for l in full_text.split("\n") if l.strip()]

    # Noise filter
    noise = re.compile(
        r"(推荐|收藏|上一[章页]|下一[章页]|目录|返回|广告|本站|书签|加入书架|"
        r"投票|打赏|举报|www\.|\.com|\.net|http|最新章节|手机阅读|请牢记|备用域名)"
    )

    # Split into chapters
    chapters = []
    current_title = None
    current_lines = []

    for line in lines:
        if noise.search(line):
            continue
        if CHAPTER_RE.match(line):
            if current_title and current_lines:
                chapters.append({
                    "title": current_title,
                    "content": "\n\n".join(current_lines)
                })
            current_title = line
            current_lines = []
        elif current_title:
            current_lines.append(line)

    # Last chapter
    if current_title and current_lines:
        chapters.append({
            "title": current_title,
            "content": "\n\n".join(current_lines)
        })

    result = {"title": novel_title, "chapters": chapters}
    set_cache(url, result, prefix="allhtml_")
    return result


# ====================== STANDARD CHAPTER PARSING ======================
def get_chapters_standard(index_url):
    """Standard: fetch chapter list from index page (each chapter = separate URL)."""
    cached = get_cache(index_url, prefix="chapters_")
    if cached:
        return cached

    soup = fetch(index_url)

    selectors = [
        "#list a", ".listmain a", ".chapter-list a", ".mulu a",
        "#chapterList a", "dd a", ".book-list a", ".chapters a",
        "#dir a", ".book-chapter a", ".catalog li a", ".mu_contain a",
        "#catalog a", ".centent a", "ul.mulu_list a",
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
            full_url = urljoin(index_url, href)
            if full_url not in seen:
                seen.add(full_url)
                chapters.append({"title": title, "url": full_url})

    set_cache(index_url, chapters, prefix="chapters_")
    return chapters


def get_content_standard(url):
    """Standard: fetch single chapter content."""
    cached = get_cache(url, prefix="raw_")
    if cached:
        return cached

    soup = fetch(url)
    for tag in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        tag.decompose()

    selectors = [
        "#content", "#chaptercontent", ".chapter-content", "#BookText",
        ".read-content", ".txtnav", "#txt", ".book-content",
    ]
    el = None
    for sel in selectors:
        candidate = soup.select_one(sel)
        if candidate and len(candidate.get_text(strip=True)) > 100:
            el = candidate
            break

    if not el:
        return ""

    for br in el.find_all("br"):
        br.replace_with("\n")

    text = el.get_text(separator="\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    noise = re.compile(r"(推荐|收藏|上一[章页]|下一[章页]|目录|返回|广告|www\.|\.com|http)")
    cleaned = [l for l in lines if not noise.search(l)]
    final = "\n\n".join(cleaned)

    set_cache(url, final, prefix="raw_")
    return final


# ====================== MEMORY SYSTEM ======================
def get_memory(novel_url):
    mem = get_cache(novel_url, prefix="memory_")
    return mem or {"characters": {}, "places": {}, "terms": {}, "summary": "", "n": 0}

def save_memory(novel_url, memory):
    set_cache(novel_url, memory, prefix="memory_")

def build_memory_block(memory, user_glossary=""):
    """Build the glossary/context block for the translator from memory + user glossary."""
    parts = []
    all_terms = {}

    for mapping in [memory.get("characters", {}), memory.get("places", {}), memory.get("terms", {})]:
        all_terms.update(mapping)

    # Add user glossary (overrides auto)
    if user_glossary.strip():
        for line in user_glossary.strip().split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                all_terms[k.strip()] = v.strip()

    if all_terms:
        pairs = [f"  {k} = {v}" for k, v in all_terms.items()]
        parts.append("【BẢNG THUẬT NGỮ BẮT BUỘC】\n" + "\n".join(pairs))

    summary = memory.get("summary", "")
    if summary:
        parts.append(f"【BỐI CẢNH】 {summary}")

    return "\n\n".join(parts)


def extract_memory_from_chapter(chinese_text, translated_text, memory, novel_url):
    """After translating, extract entities to build memory over time."""
    prompt = f"""Phân tích cặp văn bản Trung-Việt. Trích xuất JSON:
- "characters": tên nhân vật Trung→Việt
- "places": địa danh Trung→Việt  
- "terms": thuật ngữ đặc biệt Trung→Việt
- "summary": tóm tắt 1 câu tiếng Việt

Nhân vật đã biết (giữ nguyên): {json.dumps(memory.get('characters',{}), ensure_ascii=False)[:500]}

GỐC: {chinese_text[:2000]}
DỊCH: {translated_text[:2000]}

CHỈ trả về JSON, không giải thích."""

    try:
        r = AI_CLIENT.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=1500,
        )
        raw = r.choices[0].message.content.strip()
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        data = json.loads(raw)

        mem = memory.copy()
        for key in ["characters", "places", "terms"]:
            mem.setdefault(key, {}).update(data.get(key, {}))
        mem["summary"] = data.get("summary", mem.get("summary", ""))
        mem["n"] = mem.get("n", 0) + 1
        save_memory(novel_url, mem)
        return mem
    except Exception as e:
        print(f"Memory extraction error: {e}")
        return memory


def init_memory_from_chapter(novel_url, text):
    """Initialize memory from first chapter text."""
    mem = get_memory(novel_url)
    if mem.get("n", 0) > 0:
        return mem

    prompt = f"""Phân tích chương đầu tiểu thuyết Trung Quốc. Trích xuất JSON:
- "characters": tên nhân vật Trung→phiên âm Hán-Việt
- "places": địa danh Trung→Hán-Việt
- "terms": thuật ngữ đặc biệt Trung→Hán-Việt
- "genre": thể loại (kiếm hiệp/tiên hiệp/đô thị/lịch sử...)
- "summary": bối cảnh truyện 1 câu tiếng Việt
- "novel_title_vi": tên truyện phiên âm Hán-Việt

VĂN BẢN: {text[:4000]}

CHỈ JSON."""

    try:
        r = AI_CLIENT.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=2000,
        )
        raw = r.choices[0].message.content.strip()
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        data = json.loads(raw)

        mem["characters"] = data.get("characters", {})
        mem["places"] = data.get("places", {})
        mem["terms"] = data.get("terms", {})
        mem["summary"] = data.get("summary", "")
        mem["genre"] = data.get("genre", "")
        mem["novel_title_vi"] = data.get("novel_title_vi", "")
        mem["n"] = 0
        save_memory(novel_url, mem)
        return mem
    except Exception as e:
        print(f"Memory init error: {e}")
        return mem


# ====================== TRANSLATION ======================
STYLE_PROMPTS = {
    "cotrang": "Dịch phong cách cổ trang, ngôn ngữ trang trọng, giàu hình ảnh kiếm hiệp.",
    "hiendai": "Dịch tự nhiên, hiện đại, dễ đọc, giọng văn gần gũi.",
    "langman": "Dịch văn phong lãng mạn, trữ tình, giàu cảm xúc.",
    "satnghia": "Dịch sát nghĩa, chính xác từng câu, tối thiểu thay đổi cấu trúc.",
    "nguyenban": "Giữ nguyên phong cách gốc, cân bằng chính xác và tự nhiên.",
}


def translate(text, glossary="", style="nguyenban", custom_prompt="",
             chapter_url="", novel_url=""):
    if not text.strip():
        return "Không có nội dung."

    cache_url = f"{chapter_url}__s_{style}" if chapter_url else ""
    if cache_url:
        cached = get_cache(cache_url, prefix="tr_")
        if cached:
            return cached

    memory = get_memory(novel_url) if novel_url else {}
    memory_block = build_memory_block(memory, glossary)
    tone = custom_prompt.strip() if custom_prompt.strip() else STYLE_PROMPTS.get(style, STYLE_PROMPTS["nguyenban"])

    # Chunk
    paragraphs = text.split("\n\n")
    chunks, current = [], ""
    for p in paragraphs:
        if len(current) + len(p) > 5000 and current:
            chunks.append(current)
            current = p
        else:
            current = current + "\n\n" + p if current else p
    if current:
        chunks.append(current)

    system_msg = (
        "Bạn là dịch giả tiểu thuyết Trung-Việt hàng đầu. "
        "Dịch mượt mà, tự nhiên, truyền tải chính xác tinh thần nguyên tác. "
        "Tên nhân vật/địa danh/thuật ngữ PHẢI theo bảng thuật ngữ nếu có."
    )

    results = []
    for i, chunk in enumerate(chunks):
        prompt = f"""Dịch sang tiếng Việt. PHONG CÁCH: {tone}

{memory_block}

QUY TẮC:
1. Dịch TOÀN BỘ, không bỏ sót.
2. Tên riêng phiên âm Hán-Việt nhất quán theo bảng thuật ngữ.
3. Giữ nguyên phân đoạn, đối thoại giữ dấu ngoặc kép.
4. Văn phong mượt mà như tiểu thuyết Việt, KHÔNG đọc như dịch máy.
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
            results.append(f"[Lỗi dịch phần {i+1}: {str(e)}]")

    final = "\n\n".join(results)

    if cache_url:
        set_cache(cache_url, final, prefix="tr_")

    # Update memory (non-blocking failure OK)
    if novel_url:
        try:
            extract_memory_from_chapter(text, final, memory, novel_url)
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
            # ALL.HTML MODE: parse everything from one page
            data = parse_all_html(url)
            chapters_list = []
            for i, ch in enumerate(data["chapters"]):
                # Cache each chapter's content individually
                virtual_url = f"{url}#ch_{i}"
                set_cache(virtual_url, ch["content"], prefix="raw_")
                chapters_list.append({"title": ch["title"], "url": virtual_url})
            return jsonify({
                "chapters": chapters_list,
                "novel_title": data.get("title", ""),
                "mode": "all_html",
            })
        else:
            # STANDARD MODE
            chapters = get_chapters_standard(url)
            if not chapters:
                return jsonify({"error": "Không tìm thấy mục lục."}), 404

            # Try to extract title from page
            try:
                soup = fetch(url)
                title_tag = soup.find("title")
                title = title_tag.get_text(strip=True).split("_")[0].split("-")[0].strip() if title_tag else ""
            except Exception:
                title = ""

            return jsonify({
                "chapters": chapters,
                "novel_title": title,
                "mode": "standard",
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/init_memory", methods=["POST"])
def api_init_memory():
    data = request.json
    novel_url = data.get("novel_url", "").strip()
    first_chapter_content = data.get("first_chapter_content", "")

    if not novel_url:
        return jsonify({"error": "Thiếu URL"}), 400

    mem = get_memory(novel_url)
    if mem.get("n", 0) > 0 or mem.get("characters"):
        return jsonify({"status": "ready", "memory": _mem_stats(mem)})

    # If content provided, use it; otherwise try to fetch first chapter
    text = first_chapter_content
    if not text:
        first_url = data.get("first_chapter_url", "")
        if first_url:
            text = get_cache(first_url, prefix="raw_")
            if not text:
                try:
                    text = get_content_standard(first_url) if not first_url.startswith(novel_url + "#") else ""
                except Exception:
                    text = ""

    if text and len(text) > 100:
        mem = init_memory_from_chapter(novel_url, text)

    return jsonify({"status": "initialized", "memory": _mem_stats(mem)})


@app.route("/api/translate", methods=["POST"])
def api_translate():
    data = request.json
    chapter_url = data.get("url", "").strip()
    glossary = data.get("glossary", "")
    style = data.get("style", "nguyenban")
    custom_prompt = data.get("custom_prompt", "")
    novel_url = data.get("novel_url", "").strip()

    if not chapter_url:
        return jsonify({"error": "Thiếu URL chương"}), 400

    try:
        # Check if content is pre-cached (from all.html parsing)
        raw_text = get_cache(chapter_url, prefix="raw_")
        if not raw_text:
            raw_text = get_content_standard(chapter_url)
        if not raw_text:
            return jsonify({"error": "Không tìm thấy nội dung chương"}), 500

        viet_text = translate(raw_text, glossary, style, custom_prompt, chapter_url, novel_url)
        mem = get_memory(novel_url) if novel_url else {}
        return jsonify({
            "translation": viet_text,
            "memory": _mem_stats(mem),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/memory", methods=["POST"])
def api_memory():
    novel_url = request.json.get("novel_url", "").strip()
    if not novel_url:
        return jsonify({"error": "Thiếu URL"}), 400
    mem = get_memory(novel_url)
    return jsonify({"memory": mem})


def _mem_stats(mem):
    return {
        "characters": len(mem.get("characters", {})),
        "places": len(mem.get("places", {})),
        "terms": len(mem.get("terms", {})),
        "n": mem.get("n", 0),
        "genre": mem.get("genre", ""),
        "novel_title_vi": mem.get("novel_title_vi", ""),
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
