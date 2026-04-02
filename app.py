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

# ====================== FETCH (working version) ======================
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
    cached = get_cache(index_url)
    if cached and "chapters" in cached:
        return cached["chapters"]

    soup = fetch(index_url)
    selectors = ["#list a", ".listmain a", ".chapter-list a", ".mulu a", "#chapterList a", "dd a", ".book-list a"]
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

    set_cache(index_url, {"chapters": chapters})
    return chapters

# ====================== LẤY NỘI DUNG (working version) ======================
def get_content(url):
    cached = get_cache(url)
    if cached and "content" in cached:
        return cached["content"]

    soup = fetch(url)
    for tag in soup.find_all(["script", "style", "iframe", "header", "footer"]):
        tag.decompose()

    selectors = ["#content", "#chaptercontent", ".chapter-content", "#BookText", ".read-content", ".txtnav", "#txt"]
    el = None
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            break

    if not el:
        return ""

    text = el.get_text(separator="\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    cleaned = [line for line in lines if not re.search(r"(推荐|收藏|上一章|下一章|目录|返回|广告)", line)]
    final_text = "\n\n".join(cleaned)

    set_cache(url, {"content": final_text})
    return final_text

# ====================== DỊCH GROK (STRONG VERSION) ======================
def translate(text, glossary="", custom_prompt=""):
    if not text.strip():
        return "Không có nội dung để dịch."

    # Chia chunk
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
    tone = custom_prompt.strip() or "Giữ nguyên ý nghĩa, giọng văn và phong cách văn học gốc"

    results = []
    for i, chunk in enumerate(chunks):
        prompt = f"""Bạn là dịch giả chuyên nghiệp tiểu thuyết Trung Quốc sang tiếng Việt.

YÊU CẦU BẮT BUỘC:
- Dịch TOÀN BỘ văn bản sau sang tiếng Việt.
- {tone}
- Sử dụng bảng thuật ngữ nếu có (ưu tiên tuyệt đối).
- Giữ nguyên văn phong kiếm hiệp, cổ trang, huyền huyễn.
- Chỉ trả về bản dịch sạch bằng tiếng Việt, KHÔNG thêm chú thích, không giải thích, không ghi "Dịch:".

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
                time.sleep(1.5)
        except Exception as e:
            results.append(f"[Lỗi dịch chunk {i+1}: {str(e)}]")

    return "\n\n".join(results)

# ====================== ROUTES ======================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/chapters", methods=["POST"])
def api_chapters():
    url = request.json.get("url", "").strip()
    try:
        chapters = get_chapters(url)
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
        viet_text = translate(raw_text, glossary, custom_prompt)
        return jsonify({"translation": viet_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
