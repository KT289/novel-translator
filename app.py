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
XAI_API_KEY = os.environ.get("XAI_API_KEY")
if not XAI_API_KEY:
    raise Exception("Thiếu XAI_API_KEY trong Railway Environment Variables")

XAI_CLIENT = OpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1"
)

CACHE = Path("cache")
CACHE.mkdir(exist_ok=True)


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


def detect_site(url):
    host = (urlparse(url).hostname or "").lower()
    if "hetushu" in host or "hetubook" in host:
        return "hetushu"
    if "69shu" in host and "69read" not in host:
        return "69shuba"
    if "69read" in host:
        return "69read"
    if "piaotia" in host or "piaotian" in host or "ptwxz" in host:
        return "piaotia"
    return "generic"


# ====================== FETCH ======================
HEADERS_SIMPLE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8",
}
HEADERS_FULL = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.google.com/",
}


def fetch(url, profile="chrome", headers=None):
    h = headers or (HEADERS_SIMPLE if profile == "chrome" else HEADERS_FULL)
    for attempt in range(4):
        try:
            r = cffi_requests.get(url, headers=h, impersonate=profile, timeout=25)
            if r.status_code == 200:
                return BeautifulSoup(r.content, "lxml")
            if r.status_code == 403:
                time.sleep(2 ** attempt)
                continue
            raise Exception(f"HTTP {r.status_code}")
        except Exception as e:
            if attempt == 3:
                raise Exception(f"Không thể tải trang: {str(e)}")
            time.sleep(2)
    raise Exception("Không thể tải trang sau nhiều lần thử")


def smart_fetch(url):
    site = detect_site(url)
    if site in ("hetushu", "69read"):
        return fetch(url, profile="chrome", headers=HEADERS_SIMPLE)
    else:
        try:
            return fetch(url, profile="chrome124", headers=HEADERS_FULL)
        except Exception:
            return fetch(url, profile="chrome", headers=HEADERS_SIMPLE)


# ====================== NOVEL MEMORY SYSTEM ======================
def get_novel_key(novel_url):
    """Derive a stable key from the novel's index URL."""
    return cache_key(novel_url)


def get_memory(novel_url):
    """Load existing memory for a novel."""
    mem = get_cache(novel_url, prefix="memory_")
    if mem:
        return mem
    return {
        "characters": {},
        "places": {},
        "terms": {},
        "recent_summary": "",
        "chapters_processed": 0,
    }


def save_memory(novel_url, memory):
    set_cache(novel_url, memory, prefix="memory_")


def extract_memory(chinese_text, translated_text, existing_memory, novel_url):
    """
    After translating a chapter, ask Grok to extract key entities.
    This builds the novel's memory over time for consistent translations.
    """
    # Build existing memory context
    existing_chars = ""
    if existing_memory.get("characters"):
        pairs = [f"{k} = {v}" for k, v in existing_memory["characters"].items()]
        existing_chars = "\n".join(pairs[:50])  # Limit to 50 entries

    existing_terms = ""
    if existing_memory.get("terms"):
        pairs = [f"{k} = {v}" for k, v in existing_memory["terms"].items()]
        existing_terms = "\n".join(pairs[:30])

    # Take first ~3000 chars of each for extraction
    cn_sample = chinese_text[:3000]
    vn_sample = translated_text[:3000]

    prompt = f"""Phân tích đoạn truyện Trung-Việt dưới đây. Trích xuất TẤT CẢ thông tin sau ở dạng JSON:

1. "characters": Tên nhân vật (Trung → Việt) — mỗi nhân vật xuất hiện trong chương
2. "places": Địa danh (Trung → Việt)
3. "terms": Thuật ngữ đặc biệt — chiêu thức, cảnh giới tu luyện, tổ chức, vũ khí... (Trung → Việt)
4. "summary": Tóm tắt nội dung chương trong 2-3 câu tiếng Việt

{f'Các nhân vật đã biết (giữ nguyên):{chr(10)}{existing_chars}' if existing_chars else ''}
{f'Thuật ngữ đã biết (giữ nguyên):{chr(10)}{existing_terms}' if existing_terms else ''}

=== VĂN BẢN GỐC (Trung) ===
{cn_sample}

=== BẢN DỊCH (Việt) ===
{vn_sample}

Trả lời CHỈ bằng JSON hợp lệ, không giải thích. Ví dụ:
{{"characters": {{"李瑕": "Lý Hà", "聂仲由": "Nhiếp Trọng Do"}}, "places": {{"庐州": "Lư Châu"}}, "terms": {{"斡腹": "Ngạc phúc"}}, "summary": "Lý Hà được giao nhiệm vụ..."}}"""

    try:
        response = XAI_CLIENT.chat.completions.create(
            model="grok-4.20-non-reasoning",
            messages=[
                {"role": "system", "content": "Bạn trích xuất thông tin từ tiểu thuyết. Chỉ trả về JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=2000,
        )
        raw = response.choices[0].message.content.strip()
        # Clean JSON from possible markdown
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        data = json.loads(raw)

        # Merge into existing memory
        memory = existing_memory.copy()
        if data.get("characters"):
            memory.setdefault("characters", {}).update(data["characters"])
        if data.get("places"):
            memory.setdefault("places", {}).update(data["places"])
        if data.get("terms"):
            memory.setdefault("terms", {}).update(data["terms"])
        if data.get("summary"):
            memory["recent_summary"] = data["summary"]
        memory["chapters_processed"] = memory.get("chapters_processed", 0) + 1

        save_memory(novel_url, memory)
        return memory

    except Exception as e:
        # Memory extraction failed — not critical, continue without
        print(f"Memory extraction error: {e}")
        return existing_memory


def build_memory_prompt(memory):
    """Convert memory into a prompt block for the translator."""
    parts = []

    chars = memory.get("characters", {})
    if chars:
        pairs = [f"  {k} = {v}" for k, v in chars.items()]
        parts.append("【NHÂN VẬT】\n" + "\n".join(pairs))

    places = memory.get("places", {})
    if places:
        pairs = [f"  {k} = {v}" for k, v in places.items()]
        parts.append("【ĐỊA DANH】\n" + "\n".join(pairs))

    terms = memory.get("terms", {})
    if terms:
        pairs = [f"  {k} = {v}" for k, v in terms.items()]
        parts.append("【THUẬT NGỮ】\n" + "\n".join(pairs))

    summary = memory.get("recent_summary", "")
    if summary:
        parts.append(f"【BỐI CẢNH CHƯƠNG TRƯỚC】\n  {summary}")

    if not parts:
        return ""

    return "\n\n".join(parts)


# ====================== LẤY MỤC LỤC ======================
def get_chapters(index_url):
    cached = get_cache(index_url, prefix="chapters_")
    if cached:
        return cached

    original_url = index_url
    site = detect_site(index_url)

    # 69shuba: normalize .htm/.html → directory URL
    if site == "69shuba":
        if index_url.endswith(".htm") or index_url.endswith(".html"):
            index_url = index_url.rsplit("/", 1)[0] + "/"

    soup = smart_fetch(index_url)

    # All known selectors
    selectors = [
        "#list a", ".listmain a", ".chapter-list a", ".mulu a",
        "#chapterList a", "dd a", ".book-list a", ".chapters a",
        "#dir a", ".book-chapter a", ".catalog li a", ".mu_contain a",
        "#catalog a", ".centent a", "ul.mulu_list a", ".booklist a",
        ".mainbody a", "#chapterlist a", ".volume-wrap a",
        # 69read.net
        ".chapter a", ".book_last a", ".chapterlist a",
        "#at a", ".at a",
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

    set_cache(original_url, chapters, prefix="chapters_")
    return chapters


# ====================== LẤY NỘI DUNG ======================
def get_content(url):
    cached = get_cache(url, prefix="raw_")
    if cached:
        return cached

    soup = smart_fetch(url)

    for tag in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        tag.decompose()

    # All known content selectors
    selectors = [
        "#content", "#chaptercontent", ".chapter-content", "#BookText",
        ".read-content", ".txtnav", "#txt", ".book-content",
        "#htmlContent", ".mainbody", ".novelcontent", "#contentbox",
        ".content", "#booktxt", "#at",
    ]
    el = None
    for sel in selectors:
        candidate = soup.select_one(sel)
        if candidate and len(candidate.get_text(strip=True)) > 100:
            el = candidate
            break

    # Fallback: largest Chinese text block
    if not el:
        candidates = soup.find_all(["div", "article", "section"])
        best, best_len = None, 0
        for c in candidates:
            txt = c.get_text(strip=True)
            if len(txt) > best_len and re.search(r'[\u4e00-\u9fff]', txt):
                best, best_len = c, len(txt)
        if best and best_len > 200:
            el = best

    if not el:
        return ""

    # Remove watermarks
    for tag in el.find_all(["s", "dfn", "samp", "var", "cite"]):
        txt = tag.get_text()
        if re.search(r'和.?图.?书|hetushu|www\.|\.com', txt):
            tag.decompose()

    # Handle <br> tags
    for br in el.find_all("br"):
        br.replace_with("\n")

    text = el.get_text(separator="\n")
    lines = [line.strip().replace("|", "") for line in text.split("\n") if line.strip()]

    noise = re.compile(
        r"(推荐|收藏|上一[章页]|下一[章页]|目录|返回|广告|本站|书签|加入书架|"
        r"投票|打赏|举报|纠错|求月票|求推荐|www\.|\.com|\.net|\.org|http|"
        r"最新章节|手机阅读|书友|请牢记|备用域名|永久地址|一秒记住|"
        r"和.?图.?书|hetushu|hetubook)"
    )
    cleaned = [line for line in lines if not noise.search(line)]
    final_text = "\n\n".join(cleaned)

    set_cache(url, final_text, prefix="raw_")
    return final_text


# ====================== DỊCH GROK VỚI MEMORY ======================
STYLE_PROMPTS = {
    "cotrang": "Dịch theo phong cách cổ trang, sử dụng ngôn ngữ trang trọng, giàu hình ảnh và cổ kính. Dùng từ Hán Việt khi phù hợp, giữ sắc thái trang nhã của văn phong kiếm hiệp, tiên hiệp.",
    "hiendai": "Dịch tự nhiên, hiện đại, dễ đọc. Giọng văn gần gũi, trôi chảy, phù hợp với bạn đọc trẻ.",
    "langman": "Dịch văn phong lãng mạn, trữ tình, giàu cảm xúc. Chú trọng miêu tả tâm lý nhân vật và không khí lãng mạn.",
    "satnghia": "Dịch sát nghĩa, chính xác từng câu, tối thiểu thay đổi cấu trúc so với nguyên bản.",
    "nguyenban": "Giữ nguyên ý nghĩa, giọng văn và phong cách văn học gốc. Cân bằng giữa tính chính xác và sự tự nhiên trong tiếng Việt.",
}


def translate(text, glossary="", style="nguyenban", custom_prompt="",
             chapter_url="", novel_url=""):
    if not text.strip():
        return "Không có nội dung để dịch."

    # Check translation cache
    cache_url = f"{chapter_url}__style_{style}" if chapter_url else ""
    if cache_url:
        cached = get_cache(cache_url, prefix="translated_")
        if cached:
            return cached

    # Load novel memory
    memory = get_memory(novel_url) if novel_url else {}
    memory_block = build_memory_prompt(memory)

    # Merge user glossary + memory characters/terms
    full_glossary = glossary.strip()
    if memory.get("characters") or memory.get("terms") or memory.get("places"):
        auto_terms = []
        for mapping in [memory.get("characters", {}),
                        memory.get("places", {}),
                        memory.get("terms", {})]:
            for k, v in mapping.items():
                auto_terms.append(f"{k} = {v}")
        if auto_terms:
            auto_block = "\n".join(auto_terms)
            if full_glossary:
                full_glossary = full_glossary + "\n" + auto_block
            else:
                full_glossary = auto_block

    # Chunk the text
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) > 6000 and current:
            chunks.append(current)
            current = p
        else:
            current = current + "\n\n" + p if current else p
    if current:
        chunks.append(current)

    glossary_block = f"\n【BẢNG THUẬT NGỮ BẮT BUỘC — phải tuân thủ chính xác】\n{full_glossary}\n" if full_glossary else ""
    tone = custom_prompt.strip() if custom_prompt.strip() else STYLE_PROMPTS.get(style, STYLE_PROMPTS["nguyenban"])

    # Build context block from memory
    context_block = ""
    if memory_block:
        context_block = f"\n【BỘ NHỚ TRUYỆN — dùng để giữ nhất quán】\n{memory_block}\n"

    system_msg = (
        "Bạn là dịch giả tiểu thuyết Trung-Việt chuyên nghiệp hàng đầu với hơn 20 năm kinh nghiệm. "
        "Bạn dịch mượt mà, tự nhiên, truyền tải chính xác tinh thần nguyên tác. "
        "Tên nhân vật, địa danh, thuật ngữ phải TUYỆT ĐỐI nhất quán với bảng thuật ngữ và bộ nhớ truyện nếu có."
    )

    results = []
    for i, chunk in enumerate(chunks):
        prompt = f"""Dịch đoạn tiểu thuyết Trung Quốc sau sang tiếng Việt.

PHONG CÁCH: {tone}
{context_block}
{glossary_block}
QUY TẮC:
1. Dịch TOÀN BỘ nội dung, không bỏ sót câu nào.
2. Tên riêng phiên âm Hán-Việt — PHẢI dùng đúng tên trong bảng thuật ngữ/bộ nhớ nếu có.
3. Thành ngữ, tục ngữ chuyển sang tương đương tiếng Việt nếu có, nếu không thì diễn giải tự nhiên.
4. Giữ nguyên phân đoạn. Mỗi đoạn xuống dòng đôi.
5. Đối thoại giữ dấu ngoặc kép. Giọng nói phải phân biệt rõ tính cách nhân vật.
6. Văn phong phải mượt mà, tự nhiên như tiểu thuyết tiếng Việt, KHÔNG được đọc như bản dịch máy.
7. CHỈ trả về bản dịch tiếng Việt, không giải thích, không ghi chú, không thêm bất kỳ chữ nào khác.

=== VĂN BẢN CẦN DỊCH ===
{chunk}
=== KẾT THÚC VĂN BẢN ==="""

        try:
            response = XAI_CLIENT.chat.completions.create(
                model="grok-4.20-non-reasoning",
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=8000,
            )
            results.append(response.choices[0].message.content.strip())
            if i < len(chunks) - 1:
                time.sleep(1)
        except Exception as e:
            results.append(f"[Lỗi dịch phần {i+1}: {str(e)}]")

    final = "\n\n".join(results)

    # Save translation cache
    if cache_url:
        set_cache(cache_url, final, prefix="translated_")

    # Extract memory from this chapter (async-like: do it after returning would be ideal,
    # but in sync Flask we do it here — adds ~5s but builds valuable context)
    if novel_url:
        try:
            extract_memory(text, final, memory, novel_url)
        except Exception:
            pass  # Memory extraction failure is not critical

    return final


# ====================== MEMORY INITIALIZATION ======================
def init_memory(novel_url, chapters):
    """
    Initialize memory for a new novel by analyzing the first chapter.
    Called when a novel has no existing memory.
    """
    memory = get_memory(novel_url)
    if memory.get("chapters_processed", 0) > 0:
        return memory  # Already initialized

    # Try to get content of first chapter for initial analysis
    if not chapters:
        return memory

    try:
        first_chapter_url = chapters[0]["url"]
        raw_text = get_content(first_chapter_url)
        if not raw_text or len(raw_text) < 100:
            return memory

        # Ask Grok to analyze the novel's first chapter
        prompt = f"""Phân tích chương đầu tiên của tiểu thuyết Trung Quốc này. Trích xuất:

1. "characters": Tất cả tên nhân vật xuất hiện (Trung → phiên âm Hán-Việt)
2. "places": Tất cả địa danh (Trung → Hán-Việt)
3. "terms": Thuật ngữ đặc biệt — cảnh giới, chiêu thức, tổ chức, vũ khí (Trung → Hán-Việt)
4. "genre": Thể loại truyện (kiếm hiệp / tiên hiệp / đô thị / lịch sử / huyền huyễn...)
5. "summary": Bối cảnh truyện trong 2 câu tiếng Việt

=== VĂN BẢN ===
{raw_text[:4000]}
=== HẾT ===

Trả lời CHỈ bằng JSON hợp lệ."""

        response = XAI_CLIENT.chat.completions.create(
            model="grok-4.20-non-reasoning",
            messages=[
                {"role": "system", "content": "Bạn phân tích tiểu thuyết Trung Quốc. Chỉ trả về JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=2000,
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        data = json.loads(raw)

        memory["characters"] = data.get("characters", {})
        memory["places"] = data.get("places", {})
        memory["terms"] = data.get("terms", {})
        memory["recent_summary"] = data.get("summary", "")
        memory["genre"] = data.get("genre", "")
        memory["chapters_processed"] = 0  # Will be incremented after first translation
        save_memory(novel_url, memory)

    except Exception as e:
        print(f"Memory init error: {e}")

    return memory


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
        chapters = get_chapters(url)
        if not chapters:
            return jsonify({"error": "Không tìm thấy mục lục. Kiểm tra lại URL."}), 404
        return jsonify({"chapters": chapters})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/init_memory", methods=["POST"])
def api_init_memory():
    """Initialize memory for a novel (called once when entering a new novel)."""
    data = request.json
    novel_url = data.get("novel_url", "").strip()
    chapters = data.get("chapters", [])

    if not novel_url:
        return jsonify({"error": "Thiếu URL truyện"}), 400

    memory = get_memory(novel_url)
    if memory.get("chapters_processed", 0) > 0:
        # Memory already exists
        return jsonify({
            "status": "ready",
            "memory": {
                "characters": len(memory.get("characters", {})),
                "terms": len(memory.get("terms", {})),
                "places": len(memory.get("places", {})),
                "chapters_processed": memory.get("chapters_processed", 0),
            }
        })

    # Initialize from first chapter
    memory = init_memory(novel_url, chapters)
    return jsonify({
        "status": "initialized",
        "memory": {
            "characters": len(memory.get("characters", {})),
            "terms": len(memory.get("terms", {})),
            "places": len(memory.get("places", {})),
            "chapters_processed": 0,
        }
    })


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
        raw_text = get_content(chapter_url)
        if not raw_text:
            return jsonify({"error": "Không tìm thấy nội dung chương"}), 500
        viet_text = translate(
            raw_text, glossary, style, custom_prompt,
            chapter_url, novel_url
        )
        # Return memory stats along with translation
        mem = get_memory(novel_url) if novel_url else {}
        return jsonify({
            "translation": viet_text,
            "memory_stats": {
                "characters": len(mem.get("characters", {})),
                "terms": len(mem.get("terms", {})),
                "chapters_processed": mem.get("chapters_processed", 0),
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/memory", methods=["POST"])
def api_memory():
    """Get or update memory for a novel."""
    data = request.json
    novel_url = data.get("novel_url", "").strip()
    if not novel_url:
        return jsonify({"error": "Thiếu URL truyện"}), 400

    memory = get_memory(novel_url)
    return jsonify({"memory": memory})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
