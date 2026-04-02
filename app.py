import os
import re
import json
import time
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify

# === GROK (xAI) ===
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
    if "hetushu" in host:
        return "hetushu"
    if "69shu" in host:
        return "69shuba"
    if "piaotia" in host or "piaotian" in host or "ptwxz" in host:
        return "piaotia"
    return "generic"


# ====================== FETCH (EXACT WORKING VERSION) ======================
def fetch(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8",
    }
    r = cffi_requests.get(url, headers=headers, impersonate="chrome", timeout=25)
    if r.status_code != 200:
        raise Exception(f"Lỗi tải trang {r.status_code}")
    return BeautifulSoup(r.content, "lxml")


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

    soup = fetch(index_url)

    # All known selectors — covers hetushu, 69shuba, piaotia, generic
    selectors = [
        "#list a", ".listmain a", ".chapter-list a", ".mulu a",
        "#chapterList a", "dd a", ".book-list a", ".chapters a",
        "#dir a", ".book-chapter a", ".catalog li a", ".mu_contain a",
        "#catalog a", ".centent a", "ul.mulu_list a", ".booklist a",
        ".mainbody a", "#chapterlist a", ".volume-wrap a",
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

    site = detect_site(url)
    soup = fetch(url)

    # Remove noise tags
    for tag in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        tag.decompose()

    if site == "hetushu":
        text = _extract_hetushu(soup)
    else:
        text = _extract_generic(soup)

    if text:
        set_cache(url, text, prefix="raw_")
    return text


def _extract_hetushu(soup):
    """
    Hetushu scrambles paragraph order in HTML.
    <p> tags inside #content may have id/eid attributes with numeric order.
    We sort by those to restore correct reading order.
    """
    el = soup.select_one("#content")
    if not el:
        # fallback selectors
        for sel in [".book-content", "#BookText", ".chapter-content"]:
            el = soup.select_one(sel)
            if el:
                break
    if not el:
        return ""

    # Collect all paragraphs with potential ordering info
    children = el.find_all(["p", "div"], recursive=False)
    if not children:
        # If no direct children, try all <p> tags
        children = el.find_all("p")

    ordered_paras = []
    for idx, child in enumerate(children):
        text = child.get_text(strip=True)
        if not text:
            continue

        # Look for ordering attributes: id="c1", eid="2", data-eid="3", etc.
        order_num = None
        for attr in ["eid", "data-eid", "data-order", "id"]:
            val = child.get(attr, "")
            if val:
                m = re.search(r'(\d+)', str(val))
                if m:
                    order_num = int(m.group(1))
                    break

        if order_num is not None:
            ordered_paras.append((order_num, text))
        else:
            # No ordering attr — use DOM position (large offset to put after ordered ones)
            ordered_paras.append((10000 + idx, text))

    # Check if we actually found meaningful ordering
    real_order_count = sum(1 for num, _ in ordered_paras if num < 10000)
    if real_order_count > len(ordered_paras) * 0.5:
        # Most paragraphs have real ordering — sort by it
        ordered_paras.sort(key=lambda x: x[0])

    lines = [text for _, text in ordered_paras]
    return _clean_lines(lines)


def _extract_generic(soup):
    """Generic extraction for 69shuba, piaotia, and other sites."""
    selectors = [
        "#content", "#chaptercontent", ".chapter-content", "#BookText",
        ".read-content", ".txtnav", "#txt", ".book-content",
        "#htmlContent", ".mainbody", ".novelcontent", "#contentbox",
        ".content", "#booktxt",
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

    # Handle <br> tags (69shuba, piaotia use <br> instead of <p>)
    for br in el.find_all("br"):
        br.replace_with("\n")

    text = el.get_text(separator="\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return _clean_lines(lines)


def _clean_lines(lines):
    """Remove navigation/ad noise from extracted text lines."""
    noise = re.compile(
        r"(推荐|收藏|上一[章页]|下一[章页]|目录|返回|广告|本站|书签|加入书架|"
        r"投票|打赏|举报|纠错|求月票|求推荐|www\.|\.com|\.net|\.org|http|"
        r"最新章节|手机阅读|书友|请牢记|备用域名|永久地址|一秒记住)"
    )
    cleaned = [line for line in lines if not noise.search(line)]
    return "\n\n".join(cleaned)


# ====================== DỊCH GROK ======================
STYLE_PROMPTS = {
    "cotrang": "Dịch theo phong cách cổ trang, sử dụng ngôn ngữ trang trọng, giàu hình ảnh và cổ kính. Dùng từ Hán Việt khi phù hợp, giữ sắc thái trang nhã của văn phong kiếm hiệp, tiên hiệp.",
    "hiendai": "Dịch tự nhiên, hiện đại, dễ đọc. Giọng văn gần gũi, trôi chảy, phù hợp với bạn đọc trẻ.",
    "langman": "Dịch văn phong lãng mạn, trữ tình, giàu cảm xúc. Chú trọng miêu tả tâm lý nhân vật và không khí lãng mạn.",
    "satnghia": "Dịch sát nghĩa, chính xác từng câu, tối thiểu thay đổi cấu trúc so với nguyên bản.",
    "nguyenban": "Giữ nguyên ý nghĩa, giọng văn và phong cách văn học gốc. Cân bằng giữa tính chính xác và sự tự nhiên trong tiếng Việt.",
}


def translate(text, glossary="", style="nguyenban", custom_prompt="", chapter_url=""):
    if not text.strip():
        return "Không có nội dung để dịch."

    # Cache includes style so switching style re-translates
    cache_url = f"{chapter_url}__style_{style}" if chapter_url else ""
    if cache_url:
        cached = get_cache(cache_url, prefix="translated_")
        if cached:
            return cached

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

    glossary_block = f"\nBảng thuật ngữ (bắt buộc tuân thủ):\n{glossary}\n" if glossary.strip() else ""
    tone = custom_prompt.strip() if custom_prompt.strip() else STYLE_PROMPTS.get(style, STYLE_PROMPTS["nguyenban"])

    results = []
    for i, chunk in enumerate(chunks):
        prompt = f"""Bạn là dịch giả chuyên nghiệp tiểu thuyết Trung Quốc sang tiếng Việt.

YÊU CẦU BẮT BUỘC:
- Dịch TOÀN BỘ văn bản sau sang tiếng Việt.
- {tone}
- Tên riêng phiên âm Hán-Việt nhất quán.
- Thành ngữ chuyển sang tương đương tiếng Việt nếu có.
- Đối thoại giữ dấu ngoặc kép, phân biệt giọng nói nhân vật.
- Sử dụng bảng thuật ngữ nếu có.
- Giữ nguyên phân đoạn.
- Chỉ trả về bản dịch sạch bằng tiếng Việt, không thêm bất kỳ chữ nào khác.

{glossary_block}
=== VĂN BẢN CẦN DỊCH ===
{chunk}
=== KẾT THÚC VĂN BẢN ==="""

        try:
            response = XAI_CLIENT.chat.completions.create(
                model="grok-4.20-non-reasoning",
                messages=[
                    {"role": "system", "content": "Bạn là dịch giả tiểu thuyết Trung-Việt chuyên nghiệp nhất."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=8000
            )
            results.append(response.choices[0].message.content.strip())
            if i < len(chunks) - 1:
                time.sleep(1)
        except Exception as e:
            results.append(f"[Lỗi dịch chunk {i+1}: {str(e)}]")

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
