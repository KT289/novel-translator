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


# ====================== DETECT GBK SITES ======================
def is_gbk_site(url):
    """69shuba and piaotia use GBK encoding."""
    host = (urlparse(url).hostname or "").lower()
    return any(k in host for k in ["69shuba", "69shu", "piaotia", "piaotian", "ptwxz"])


# ====================== FETCH (STRONG ANTI-403) ======================
def fetch(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                  "image/webp,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive",
    }
    gbk = is_gbk_site(url)

    for attempt in range(4):
        try:
            r = cffi_requests.get(
                url,
                headers=headers,
                impersonate="chrome124",
                timeout=30,
            )
            if r.status_code == 200:
                raw = r.content
                if gbk:
                    # GBK sites: decode manually then pass string to BS4
                    try:
                        html_text = raw.decode("gbk", errors="replace")
                        return BeautifulSoup(html_text, "lxml")
                    except Exception:
                        pass
                # Default: pass raw bytes, let BS4+lxml auto-detect encoding
                return BeautifulSoup(raw, "lxml")
            if r.status_code == 403:
                time.sleep(2 ** attempt)
                continue
            raise Exception(f"HTTP {r.status_code}")
        except Exception as e:
            if attempt == 3:
                raise Exception(f"Không thể tải trang: {str(e)}")
            time.sleep(2)
    raise Exception("Không thể tải trang sau nhiều lần thử")


# ====================== GET CHAPTERS ======================
def get_chapters(index_url):
    cached = get_cache(index_url, prefix="chapters_")
    if cached:
        return cached

    original_url = index_url

    # 69shuba: normalize .htm/.html chapter URL → directory listing URL
    host = (urlparse(index_url).hostname or "").lower()
    if any(k in host for k in ["69shuba", "69shu"]):
        if index_url.endswith(".htm") or index_url.endswith(".html"):
            index_url = index_url.rsplit("/", 1)[0] + "/"

    soup = fetch(index_url)

    # Try ALL known selectors — covers hetushu, 69shuba, piaotia, and others
    selectors = [
        "#list a",
        ".listmain a",
        ".chapter-list a",
        ".mulu a",
        "#chapterList a",
        "dd a",
        ".book-list a",
        ".chapters a",
        "#dir a",
        ".book-chapter a",
        ".catalog li a",
        ".mu_contain a",
        "#catalog a",
        ".centent a",
        "ul.mulu_list a",
        ".booklist a",
        ".mainbody a",
        "#chapterlist a",
        ".volume-wrap a",
        ".cf-list a",
    ]
    links = []
    for sel in selectors:
        links.extend(soup.select(sel))

    chapters = []
    seen = set()
    for a in links:
        href = a.get("href", "")
        title = a.get_text(strip=True)
        if href and title and len(title) > 2 and re.search(r"[\u4e00-\u9fff]", title):
            full_url = urljoin(index_url, href)
            if full_url not in seen:
                seen.add(full_url)
                chapters.append({"title": title, "url": full_url})

    set_cache(original_url, chapters, prefix="chapters_")
    return chapters


# ====================== GET CONTENT ======================
def get_content(url):
    cached = get_cache(url, prefix="raw_")
    if cached:
        return cached

    soup = fetch(url)

    # Remove noise tags
    for tag in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        tag.decompose()

    # Try all known content selectors
    selectors = [
        "#content",
        "#chaptercontent",
        ".chapter-content",
        "#BookText",
        ".read-content",
        ".txtnav",
        "#txt",
        ".book-content",
        "#htmlContent",
        ".mainbody",
        ".novelcontent",
        "#contentbox",
        ".content",
        "#booktxt",
    ]
    el = None
    for sel in selectors:
        candidate = soup.select_one(sel)
        if candidate and len(candidate.get_text(strip=True)) > 100:
            el = candidate
            break

    # Fallback: find largest text block with Chinese characters
    if not el:
        candidates = soup.find_all(["div", "article", "section"])
        best = None
        best_len = 0
        for c in candidates:
            txt = c.get_text(strip=True)
            if len(txt) > best_len and re.search(r"[\u4e00-\u9fff]", txt):
                best = c
                best_len = len(txt)
        if best and best_len > 200:
            el = best

    if not el:
        return ""

    # Handle <br> tags (69shuba, piaotia use <br> instead of <p>)
    for br in el.find_all("br"):
        br.replace_with("\n")

    text = el.get_text(separator="\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    # Filter out navigation/ad noise
    noise = re.compile(
        r"(推荐|收藏|上一[章页]|下一[章页]|目录|返回|广告|本站|书签|加入书架|"
        r"投票|打赏|举报|纠错|求月票|求推荐|www\.|\.com|\.net|\.org|http|"
        r"最新章节|手机阅读|书友|请牢记|备用域名|永久地址|一秒记住)"
    )
    cleaned = [line for line in lines if not noise.search(line)]
    final_text = "\n\n".join(cleaned)

    set_cache(url, final_text, prefix="raw_")
    return final_text


# ====================== TRANSLATION ======================
STYLE_PROMPTS = {
    "cotrang": "Dịch theo phong cách cổ trang, sử dụng ngôn ngữ trang trọng, giàu hình ảnh và cổ kính. "
               "Dùng từ Hán Việt khi phù hợp, giữ sắc thái trang nhã của văn phong kiếm hiệp, tiên hiệp.",
    "hiendai": "Dịch tự nhiên, hiện đại, dễ đọc. Giọng văn gần gũi, trôi chảy, phù hợp với bạn đọc trẻ.",
    "langman": "Dịch văn phong lãng mạn, trữ tình, giàu cảm xúc. Chú trọng miêu tả tâm lý nhân vật "
               "và không khí lãng mạn.",
    "satnghia": "Dịch sát nghĩa, chính xác từng câu, tối thiểu thay đổi cấu trúc so với nguyên bản.",
    "nguyenban": "Giữ nguyên ý nghĩa, giọng văn và phong cách văn học gốc. Cân bằng giữa tính chính xác "
                 "và sự tự nhiên trong tiếng Việt.",
}


def translate(text, glossary="", style="nguyenban", custom_prompt="", chapter_url=""):
    if not text.strip():
        return "Không có nội dung để dịch."

    # Cache key includes style so switching style re-translates
    cache_url = f"{chapter_url}__style_{style}" if chapter_url else ""
    if cache_url:
        cached = get_cache(cache_url, prefix="translated_")
        if cached:
            return cached

    # Split into chunks ~6000 chars
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

    # Build prompt components
    glossary_block = ""
    if glossary.strip():
        glossary_block = f"\n【BẢNG THUẬT NGỮ BẮT BUỘC】\n{glossary.strip()}\n"

    tone = custom_prompt.strip() if custom_prompt.strip() else STYLE_PROMPTS.get(style, STYLE_PROMPTS["nguyenban"])

    system_msg = (
        "Bạn là dịch giả tiểu thuyết Trung-Việt chuyên nghiệp hàng đầu, "
        "với hơn 20 năm kinh nghiệm dịch văn học Trung Quốc. "
        "Bạn nổi tiếng với khả năng truyền tải chính xác tinh thần nguyên tác "
        "sang tiếng Việt tự nhiên, mượt mà."
    )

    results = []
    for i, chunk in enumerate(chunks):
        prompt = f"""Dịch đoạn tiểu thuyết Trung Quốc sau sang tiếng Việt.

PHONG CÁCH: {tone}

QUY TẮC:
1. Dịch TOÀN BỘ nội dung, không bỏ sót câu nào.
2. Tên riêng phiên âm Hán-Việt nhất quán xuyên suốt.
3. Thành ngữ, tục ngữ chuyển sang tương đương tiếng Việt nếu có, nếu không thì diễn giải tự nhiên.
4. Giữ nguyên phân đoạn, mỗi đoạn xuống dòng đôi.
5. Đối thoại giữ nguyên dấu ngoặc kép, giọng nói phải phân biệt rõ tính cách nhân vật.
6. CHỈ trả về bản dịch tiếng Việt, không giải thích, không ghi chú.
{glossary_block}
=== VĂN BẢN ===
{chunk}
=== HẾT ==="""

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
                time.sleep(0.8)
        except Exception as e:
            results.append(f"[Lỗi dịch phần {i + 1}: {str(e)}]")

    final = "\n\n".join(results)

    if cache_url:
        set_cache(cache_url, final, prefix="translated_")

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
        chapters = get_chapters(url)
        if not chapters:
            return jsonify({"error": "Không tìm thấy mục lục. Kiểm tra lại URL."}), 404
        return jsonify({"chapters": chapters})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/translate", methods=["POST"])
def api_translate():
    data = request.json
    chapter_url = data.get("url", "").strip()
    glossary = data.get("glossary", "")
    style = data.get("style", "nguyenban")
    custom_prompt = data.get("custom_prompt", "")

    if not chapter_url:
        return jsonify({"error": "Thiếu URL chương"}), 400

    try:
        raw_text = get_content(chapter_url)
        if not raw_text:
            return jsonify({"error": "Không tìm thấy nội dung chương"}), 500
        viet_text = translate(raw_text, glossary, style, custom_prompt, chapter_url)
        return jsonify({"translation": viet_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
