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

# ====================== DETECT SITE ======================
def detect_site(url):
    host = urlparse(url).hostname or ""
    if "hetushu" in host:
        return "hetushu"
    elif "69shuba" in host or "69shu" in host:
        return "69shuba"
    elif "piaotia" in host or "piaotian" in host:
        return "piaotia"
    return "generic"

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
                if "69shuba" in url or "piaotia" in url:
                    r.encoding = "gbk"
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

    site = detect_site(index_url)
    if site == "hetushu":
        chapters = _chapters_hetushu(index_url)
    elif site == "69shuba":
        chapters = _chapters_69shuba(index_url)
    elif site == "piaotia":
        chapters = _chapters_piaotia(index_url)
    else:
        chapters = _chapters_generic(index_url)

    if chapters:
        set_cache(index_url, chapters, prefix="chapters_")
    return chapters

def _chapters_hetushu(url):
    soup = fetch(url)
    chapters = []
    seen = set()
    for sel in ["#dir a", ".book-chapter a", ".chapter a", "dd a", "#list a", ".mulu a"]:
        for a in soup.select(sel):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if href and title and len(title) > 2:
                full_url = urljoin(url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    chapters.append({"title": title, "url": full_url})
    return chapters

def _chapters_69shuba(url):
    if url.endswith(".htm") or url.endswith(".html"):
        url = url.rsplit("/", 1)[0] + "/"
    soup = fetch(url)
    chapters = []
    seen = set()
    for sel in [".catalog li a", ".mu_contain a", "#catalog a", ".chapterlist a", "#chapterList a", "dd a", ".listmain a"]:
        for a in soup.select(sel):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if href and title and len(title) > 2 and re.search(r"[\u4e00-\u9fff]", title):
                full_url = urljoin(url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    chapters.append({"title": title, "url": full_url})
    return chapters

def _chapters_piaotia(url):
    soup = fetch(url)
    chapters = []
    seen = set()
    for sel in [".centent a", ".chapter-list a", "#list a", "ul.mulu_list a", "dd a", ".booklist a"]:
        for a in soup.select(sel):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if href and title and len(title) > 2 and re.search(r"[\u4e00-\u9fff]", title):
                full_url = urljoin(url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    chapters.append({"title": title, "url": full_url})
    return chapters

def _chapters_generic(url):
    soup = fetch(url)
    selectors = ["#list a", ".listmain a", ".chapter-list a", ".mulu a", "#chapterList a", "dd a"]
    chapters = []
    seen = set()
    for sel in selectors:
        for a in soup.select(sel):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if href and title and len(title) > 2 and re.search(r"[\u4e00-\u9fff]", title):
                full_url = urljoin(url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    chapters.append({"title": title, "url": full_url})
    return chapters

# ====================== GET CONTENT ======================
def get_content(url):
    cached = get_cache(url, prefix="raw_")
    if cached:
        return cached

    site = detect_site(url)
    if site == "hetushu":
        text = _content_hetushu(url)
    elif site == "69shuba":
        text = _content_69shuba(url)
    elif site == "piaotia":
        text = _content_piaotia(url)
    else:
        text = _content_generic(url)

    if text:
        set_cache(url, text, prefix="raw_")
    return text

def _content_hetushu(url):
    soup = fetch(url)
    for tag in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        tag.decompose()
    for sel in ["#content", ".book-content", "#BookText", ".chapter-content"]:
        el = soup.select_one(sel)
        if el:
            return _clean_text(el.get_text(separator="\n"))
    return ""

def _content_69shuba(url):
    soup = fetch(url)
    for tag in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        tag.decompose()
    for sel in ["#chaptercontent", "#content", ".txtnav", "#BookText", ".chapter-content"]:
        el = soup.select_one(sel)
        if el:
            for br in el.find_all("br"):
                br.replace_with("\n")
            return _clean_text(el.get_text(separator="\n"))
    return ""

def _content_piaotia(url):
    soup = fetch(url)
    for tag in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        tag.decompose()
    for sel in ["#content", "#BookText", ".chapter-content", ".mainbody"]:
        el = soup.select_one(sel)
        if el:
            for br in el.find_all("br"):
                br.replace_with("\n")
            return _clean_text(el.get_text(separator="\n"))
    return ""

def _content_generic(url):
    soup = fetch(url)
    for tag in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        tag.decompose()
    selectors = ["#content", "#chaptercontent", ".chapter-content", "#BookText", ".read-content", ".txtnav", "#txt"]
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            return _clean_text(el.get_text(separator="\n"))
    return ""

def _clean_text(raw_text):
    lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
    noise = re.compile(r"(推荐|收藏|上一[章页]|下一[章页]|目录|返回|广告|本站|书签|加入书架)")
    cleaned = [line for line in lines if not noise.search(line)]
    return "\n\n".join(cleaned)

# ====================== TRANSLATE ======================
STYLE_PROMPTS = {
    "cotrang": "Dịch theo phong cách cổ trang, ngôn ngữ trang trọng, giàu hình ảnh, sử dụng từ Hán Việt phù hợp.",
    "hiendai": "Dịch tự nhiên, hiện đại, gần gũi, dễ đọc.",
    "langman": "Dịch lãng mạn, trữ tình, giàu cảm xúc.",
    "satnghia": "Dịch sát nghĩa, chính xác từng câu.",
    "nguyenban": "Giữ nguyên ý nghĩa, giọng văn và phong cách văn học gốc.",
}

def translate(text, glossary="", style="nguyenban", custom_prompt="", chapter_url=""):
    if not text.strip():
        return "Không có nội dung để dịch."

    cache_key_str = f"{chapter_url}__style_{style}" if chapter_url else ""
    if cache_key_str:
        cached = get_cache(cache_key_str, prefix="translated_")
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

    glossary_block = f"\n【BẢNG THUẬT NGỮ BẮT BUỘC】\n{glossary}\n" if glossary.strip() else ""
    tone = custom_prompt.strip() if custom_prompt.strip() else STYLE_PROMPTS.get(style, STYLE_PROMPTS["nguyenban"])

    results = []
    for i, chunk in enumerate(chunks):
        prompt = f"""Bạn là dịch giả tiểu thuyết Trung Quốc sang tiếng Việt chuyên nghiệp nhất.

Yêu cầu bắt buộc:
- Dịch toàn bộ đoạn văn bản sau sang tiếng Việt.
- {tone}
- Tên nhân vật, môn phái, võ công... theo bảng thuật ngữ.
- Giữ nguyên phong cách văn học gốc.
- Câu văn mượt mà, tự nhiên, hay.
- Chỉ trả về bản dịch sạch, không chú thích.

{glossary_block}
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
        except Exception as e:
            results.append(f"[Lỗi dịch phần {i+1}]")

    final = "\n\n".join(results)

    if cache_key_str:
        set_cache(cache_key_str, final, prefix="translated_")

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
