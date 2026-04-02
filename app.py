import os, re, json, time, hashlib
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify
from google import genai

# NEW: Import the ultimate Cloudflare bypasser
from curl_cffi import requests as cffi_requests

app = Flask(__name__)

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
CACHE = Path("cache")
CACHE.mkdir(exist_ok=True)

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

# --- UPDATED FETCH FUNCTION ---
def fetch(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8,zh;q=0.7,en-US;q=0.6",
        "Referer": "https://www.google.com/",
    }
    
    try:
        # impersonate="chrome" perfectly mimics a real browser's internal signatures
        r = cffi_requests.get(
            url, 
            headers=headers, 
            impersonate="chrome", 
            timeout=20, 
            allow_redirects=True
        )
        r.encoding = r.apparent_encoding or "utf-8"
        
        if r.status_code == 403:
            raise Exception("Trang web trả về lỗi 403. Website đang chặn IP của bạn.")
        elif r.status_code != 200:
            raise Exception(f"Trang web trả về lỗi {r.status_code}.")
            
        return BeautifulSoup(r.text, "lxml")
        
    except Exception as e:
         raise Exception(f"Không thể kết nối đến web: {str(e)}")

def get_chapters(index_url):
    soup = fetch(index_url)
    containers = soup.select(
        "#list a, .listmain a, .chapter-list a, .mulu a, "
        "#chapterList a, .volume-wrap a, .book-list a, "
        "#catalog a, .catalog-content a, .chapter a, "
        ".zjlist a, .chapters a, .mu_contain a, dd a, .box_con a, "
        "#dir a, .dir a, .booklist a, #booklist a, "
        ".book_list a, #indexList a, .index_list a, "
        "table a, .chapterlist a, ul.mulu a, "
        "#chapterlist a, .ml_list a, li a"
    )
    if not containers:
        containers = [
            a for a in soup.find_all("a", href=True)
            if a.get_text(strip=True) and len(a.get_text(strip=True)) > 1
            and re.search(r"[\u4e00-\u9fff]", a.get_text(strip=True))
            and not re.search(r"(登录|注册|首页|书架|排行|分类|搜索)", a.get_text(strip=True))
            and (a["href"].endswith(".html") or a["href"].endswith(".htm") or re.search(r"/\d+/?$", a["href"]))
        ]
    chapters, seen = [], set()
    for a in containers:
        href, title = a.get("href", ""), a.get_text(strip=True)
        if not href or not title: continue
        full = urljoin(index_url, href)
        if full not in seen:
            seen.add(full)
            chapters.append({"title": title, "url": full})
    return chapters

def get_content(url):
    soup = fetch(url)
    for tag in soup.find_all(["script", "style", "iframe", "ins", "noscript"]):
        tag.decompose()
    el = soup.select_one(
        "#content, #chaptercontent, .chapter-content, .content, "
        "#BookText, .readcontent, .read-content, #TextContent, "
        ".txtnav, #txt, .chapter_content, .articlecontent, "
        ".novelcontent, #novel_content, .nr_txt, .zhangjieTXT"
    )
    if not el:
        blocks = [(len(d.get_text(strip=True)), d) for d in soup.find_all(["div", "article", "section"])
                  if len(d.get_text(strip=True)) > 200 and re.search(r"[\u4e00-\u9fff]", d.get_text(strip=True))]
        if blocks:
            blocks.sort(key=lambda x: -x[0])
            el = blocks[0][1]
    if not el: return ""
    for br in el.find_all("br"): br.replace_with("\n")
    for p in el.find_all("p"): p.insert_after("\n")
    lines = el.get_text().split("\n")
    cleaned = [l.strip() for l in lines if l.strip()
               and not re.search(r"(推荐|收藏|书签|书架|上一章|下一章|目录|返回|广告|加入书签|手机阅读|最新章节|本站|www\.|\.com|\.net|http)", l.strip())]
    return "\n\n".join(cleaned)

def translate(text, glossary="", custom_prompt=""):
    client = genai.Client(api_key=GEMINI_KEY)
    max_chunk = 3000
    if len(text) <= max_chunk:
        chunks = [text]
    else:
        paras = text.split("\n\n")
        chunks, cur = [], ""
        for p in paras:
            if len(cur) + len(p) + 2 > max_chunk and cur:
                chunks.append(cur); cur = p
            else:
                cur = f"{cur}\n\n{p}" if cur else p
        if cur: chunks.append(cur)

    gp = f"\nBảng thuật ngữ:\n{glossary}\n" if glossary.strip() else ""
    tone = custom_prompt.strip() if custom_prompt.strip() else "Giữ nguyên ý nghĩa, giọng văn và phong cách văn học"
    results = []
    
    i = 0
    while i < len(chunks):
        chunk = chunks[i]
        prompt = f"""Bạn là dịch giả tiểu thuyết Trung Quốc chuyên nghiệp.
Dịch đoạn văn sau từ tiếng Trung sang tiếng Việt.

Yêu cầu:
- {tone}
- Dịch tên nhân vật sang âm Hán-Việt
- Không thêm giải thích hay chú thích
- Chỉ trả lời bản dịch
{gp}
{chunk}"""
        try:
            resp = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
            results.append(resp.text)
            
            if i < len(chunks) - 1: 
                time.sleep(4.5)
                
            i += 1 

        except Exception as e:
            error_message = str(e)
            if '429' in error_message or 'RESOURCE_EXHAUSTED' in error_message:
                print(f"[Lỗi chunk {i+1}] Quá giới hạn API (429). Đang đợi 30 giây để thử lại...")
                time.sleep(30)
            else:
                print(f"[Lỗi chunk {i+1}]: {error_message}")
                results.append(f"[Lỗi dịch chunk {i+1}: {error_message}]")
                i += 1
                
    return "\n\n".join(results)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/chapters", methods=["POST"])
def api_chapters():
    url = request.json.get("url", "").strip()
    if not url: return jsonify(error="URL required"), 400
    try:
        chs = get_chapters(url)
        if not chs: return jsonify(error="Không tìm thấy chương."), 404
        return jsonify(chapters=chs, count=len(chs))
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route("/api/translate", methods=["POST"])
def api_translate():
    url = request.json.get("url", "").strip()
    glossary = request.json.get("glossary", "")
    custom_prompt = request.json.get("custom_prompt", "")
    if not url: return jsonify(error="URL required"), 400
    cached = get_cache(url)
    if cached: return jsonify(translation=cached["text"], cached=True)
    try:
        content = get_content(url)
        if not content or len(content) < 30:
            return jsonify(error="Không tìm thấy nội dung."), 404
        result = translate(content, glossary, custom_prompt)
        set_cache(url, {"text": result})
        return jsonify(translation=result, cached=False)
    except Exception as e:
        return jsonify(error=str(e)), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
