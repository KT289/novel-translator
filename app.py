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

# ====================== FETCH ======================
def fetch(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8",
        "Referer": "https://www.google.com/",
    }
    for attempt in range(5):
        try:
            r = cffi_requests.get(url, headers=headers, impersonate="chrome124", timeout=30)
            if r.status_code == 200:
                return BeautifulSoup(r.text, "lxml")
            if r.status_code == 403:
                time.sleep(2 ** attempt)
                continue
            raise Exception(f"HTTP {r.status_code}")
        except Exception as e:
            if attempt == 4:
                raise Exception(f"Không thể tải trang: {str(e)}")
            time.sleep(2)
    raise Exception("Không thể bypass Cloudflare")

# ====================== GET CHAPTERS ======================
def get_chapters(index_url):
    cached = get_cache(index_url, prefix="chapters_")
    if cached:
        return cached

    soup = fetch(index_url)
    chapters = []
    seen = set()

    # Selector mạnh cho hetushu.com
    selectors = ["#dir a", ".book-chapter a", ".chapter a", "dd a", "#list a", ".mulu a", ".book-list a"]

    for sel in selectors:
        for a in soup.select(sel):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if href and title and len(title) > 2:
                full_url = urljoin(index_url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    chapters.append({"title": title, "url": full_url})

    set_cache(index_url, chapters, prefix="chapters_")
    return chapters

# ====================== GET CONTENT ======================
def get_content(url):
    cached = get_cache(url, prefix="raw_")
    if cached:
        return cached

    soup = fetch(url)
    for tag in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        tag.decompose()

    selectors = ["#content", "#chaptercontent", ".chapter-content", "#BookText", ".read-content", ".txtnav", "#txt"]
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n")
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            cleaned = [line for line in lines if not re.search(r"(推荐|收藏|上一章|下一章|目录|返回|广告)", line)]
            final_text = "\n\n".join(cleaned)
            set_cache(url, final_text, prefix="raw_")
            return final_text
    return ""

# ====================== TRANSLATE ======================
def translate(text, glossary="", custom_prompt="", chapter_url=""):
    if not text.strip():
        return "Không có nội dung để dịch."

    if chapter_url:
        cached = get_cache(chapter_url, prefix="translated_")
        if cached:
            return cached

    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) > 7000 and current:
            chunks.append(current)
            current = p
        else:
            current = current + "\n\n" + p if current else p
    if current:
        chunks.append(current)

    tone = custom_prompt.strip() or "Giữ nguyên ý nghĩa, giọng văn và phong cách văn học gốc"

    results = []
    for i, chunk in enumerate(chunks):
        prompt = f"""Bạn là dịch giả chuyên nghiệp tiểu thuyết Trung Quốc sang tiếng Việt.

YÊU CẦU BẮT BUỘC:
- Dịch TOÀN BỘ văn bản sau sang tiếng Việt.
- {tone}
- Tên nhân vật, môn phái, võ công... giữ nguyên theo bảng thuật ngữ nếu có.
- Giữ nguyên văn phong kiếm hiệp, cổ trang.
- Chỉ trả về bản dịch sạch bằng tiếng Việt, không thêm chú thích.

=== VĂN BẢN CẦN DỊCH ===
{chunk}
=== HẾT ==="""

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
                time.sleep(0.7)
        except Exception:
            results.append("[Lỗi dịch phần này]")

    final = "\n\n".join(results)

    if chapter_url:
        set_cache(chapter_url, final, prefix="translated_")

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
    custom_prompt = data.get("custom_prompt", "")

    try:
        raw_text = get_content(chapter_url)
        if not raw_text:
            return jsonify({"error": "Không tìm thấy nội dung chương"}), 500
        viet_text = translate(raw_text, glossary, custom_prompt, chapter_url)
        return jsonify({"translation": viet_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
