import os
import re
import json
import time
import hashlib
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify

# === GROK (xAI) ===
from openai import OpenAI

# Cloudflare bypass
from curl_cffi import requests as cffi_requests

app = Flask(__name__)

# ====================== CONFIG ======================
XAI_API_KEY = os.environ.get("XAI_API_KEY")
if not XAI_API_KEY:
    raise Exception("Thiếu biến môi trường XAI_API_KEY")

XAI_CLIENT = OpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1"
)

CACHE = Path("cache")
CACHE.mkdir(exist_ok=True)

# ====================== CACHE ======================
def cache_key(url):
    return hashlib.md5(url.encode()).hexdigest()

def get_cache(url):
    p = CACHE / f"{cache_key(url)}.json"
    if p.exists():
        return json.loads(p.read_text("utf-8"))
    return None

def set_cache(url, data):
    p = CACHE / f"{cache_key(url)}.json"
    p.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

# ====================== FETCH ======================
def fetch(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8",
    }
    try:
        r = cffi_requests.get(url, headers=headers, impersonate="chrome", timeout=25)
        if r.status_code != 200:
            raise Exception(f"Lỗi {r.status_code}")
        return BeautifulSoup(r.content, "lxml")
    except Exception as e:
        raise Exception(f"Không thể tải trang: {str(e)}")

# ====================== LẤY MỤC LỤC (ĐÃ TỐI ƯU CHO 69SHUBA) ======================
def get_chapters(index_url):
    cached = get_cache(index_url)
    if cached and "chapters" in cached:
        return cached["chapters"]

    # === SPECIAL FIX FOR 69SHUBA.COM ===
    original_url = index_url
    if "69shuba.com" in index_url and (index_url.endswith('.htm') or index_url.endswith('.html')):
        index_url = index_url.rsplit('/', 1)[0] + '/'

    soup = fetch(index_url)

    # Danh sách selector mạnh (đã thêm riêng cho 69shuba)
    selectors = [
        'a[href^="https://www.69shuba.com/txt/"]',   # 69shuba full catalog
        "#list a", ".listmain a", ".chapter-list a", ".mulu a",
        "#chapterList a", ".volume-wrap a", ".book-list a",
        "#catalog a", ".catalog-content a", ".chapter a",
        ".zjlist a", ".chapters a", ".mu_contain a", "dd a",
        ".box_con a", "#dir a", ".dir a", ".booklist a",
        "#booklist a", ".book_list a", "#indexList a",
        ".index_list a", "ul.mulu a", "#chapterlist a", ".ml_list a",
        "li a", "table a"
    ]

    links = []
    for sel in selectors:
        links.extend(soup.select(sel))

    chapters = []
    seen = set()
    for a in links:
        href = a.get("href", "")
        title = a.get_text(strip=True)
        if not href or not title or len(title) < 2:
            continue
        full_url = urljoin(index_url, href)
        if full_url not in seen and re.search(r'[\u4e00-\u9fff]', title):
            seen.add(full_url)
            chapters.append({"title": title, "url": full_url})

    # Nếu vẫn không tìm thấy → fallback lấy tất cả link có chữ Hán
    if len(chapters) < 5:
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if re.search(r'[\u4e00-\u9fff]', title) and len(title) > 3 and href:
                full_url = urljoin(index_url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    chapters.append({"title": title, "url": full_url})

    set_cache(original_url, {"chapters": chapters})
    return chapters

# ====================== LẤY NỘI DUNG CHƯƠNG ======================
def get_content(url):
    cached = get_cache(url)
    if cached and "content" in cached:
        return cached["content"]

    soup = fetch(url)
    for tag in soup.find_all(["script", "style", "iframe", "ins", "noscript", "header", "footer"]):
        tag.decompose()

    content_selectors = [
        "#content", "#chaptercontent", ".chapter-content", ".content",
        "#BookText", ".readcontent", ".read-content", "#TextContent",
        ".txtnav", "#txt", ".chapter_content", ".articlecontent",
        ".novelcontent", "#novel_content", ".nr_txt", ".zhangjieTXT",
        ".txt", ".book-content", ".text"
    ]
    el = None
    for sel in content_selectors:
        el = soup.select_one(sel)
        if el:
            break

    if not el:
        # Fallback: lấy div lớn nhất chứa chữ Hán
        blocks = [(len(d.get_text(strip=True)), d) for d in soup.find_all(["div", "article", "section"])
                  if len(d.get_text(strip=True)) > 300 and re.search(r"[\u4e00-\u9fff]", d.get_text(strip=True))]
        if blocks:
            blocks.sort(key=lambda x: -x[0])
            el = blocks[0][1]

    if not el:
        return ""

    for br in el.find_all("br"):
        br.replace_with("\n")
    text = el.get_text(separator="\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    cleaned = [line for line in lines if not re.search(r"(推荐|收藏|书签|上一章|下一章|目录|返回|广告|手机阅读)", line)]
    final_text = "\n\n".join(cleaned)

    set_cache(url, {"content": final_text})
    return final_text

# ====================== DỊCH BẰNG GROK ======================
def translate(text, glossary="", custom_prompt=""):
    if not text.strip():
        return ""
    max_chunk = 6500
    chunks = [text] if len(text) <= max_chunk else [text[i:i+max_chunk] for i in range(0, len(text), max_chunk)]

    glossary_block = f"\nBảng thuật ngữ (ưu tiên tuyệt đối):\n{glossary}\n" if glossary.strip() else ""
    tone = custom_prompt.strip() or "Giữ nguyên ý nghĩa, giọng văn và phong cách văn học gốc"

    results = []
    for i, chunk in enumerate(chunks):
        prompt = f"""Bạn là dịch giả tiểu thuyết web Trung Quốc chuyên nghiệp, dịch sang tiếng Việt.

Yêu cầu nghiêm ngặt:
- {tone}
- Dịch tên nhân vật, môn phái, võ công... theo bảng thuật ngữ
- Giữ nguyên văn phong kiếm hiệp / cổ trang / huyền huyễn
- Chỉ trả về bản dịch sạch, không chú thích

{glossary_block}
Văn bản cần dịch:

{chunk}"""

        try:
            response = XAI_CLIENT.chat.completions.create(
                model="grok-4.20-non-reasoning",
                messages=[
                    {"role": "system", "content": "Bạn là dịch giả tiểu thuyết Trung-Việt hàng đầu."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=8000
            )
            results.append(response.choices[0].message.content.strip())
            if i < len(chunks) - 1:
                time.sleep(1.2)
        except Exception as e:
            print(f"[Grok chunk {i+1} lỗi]: {e}")
            results.append(f"[Lỗi dịch chunk {i+1}]")

    return "\n\n".join(results)

# ====================== ROUTES ======================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/chapters", methods=["POST"])
def api_chapters():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        chapters = get_chapters(url)
        if not chapters:
            return jsonify({"error": "Không tìm thấy mục lục. Hãy thử URL đầy đủ catalog (kết thúc bằng /)"}), 500
        return jsonify({"chapters": chapters})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/translate", methods=["POST"])
def api_translate():
    data = request.json
    chapter_url = data.get("url", "").strip()
    glossary = data.get("glossary", "")
    custom_prompt = data.get("custom_prompt", "")

    if not chapter_url:
        return jsonify({"error": "URL required"}), 400

    try:
        raw_text = get_content(chapter_url)
        if not raw_text:
            return jsonify({"error": "Không tìm thấy nội dung chương"}), 500
        viet_text = translate(raw_text, glossary, custom_prompt)
        return jsonify({"translation": viet_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
