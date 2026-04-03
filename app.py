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
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
XAI_KEY = os.environ.get("XAI_API_KEY")
AI_CLIENT = None
MODEL = None
if GEMINI_KEY:
    AI_CLIENT = OpenAI(api_key=GEMINI_KEY, base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
    MODEL = "gemini-3.1-flash-lite-preview"
elif XAI_KEY:
    AI_CLIENT = OpenAI(api_key=XAI_KEY, base_url="https://api.x.ai/v1")
    MODEL = "grok-3-mini"
print(f"[BOOT] Model={MODEL}, Key={'OK' if AI_CLIENT else 'MISSING'}")

CACHE = Path("cache")
CACHE.mkdir(exist_ok=True)

# In-memory store for all.html chapters (workers=1 so this always works)
ALLHTML_STORE = {}


# ====================== CACHE ======================
def _ck(u): return hashlib.md5(u.encode()).hexdigest()
def cget(u, p=""):
    f = CACHE / f"{p}{_ck(u)}.json"
    try: return json.loads(f.read_text("utf-8")) if f.exists() else None
    except: return None
def cset(u, d, p=""):
    try: (CACHE / f"{p}{_ck(u)}.json").write_text(json.dumps(d, ensure_ascii=False), "utf-8")
    except: pass


# ====================== FETCH ======================
# Simple fetch — works for chapter listing pages
def fetch_simple(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8",
    }
    r = cffi_requests.get(url, headers=headers, impersonate="chrome", timeout=25)
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}")
    return BeautifulSoup(r.content, "lxml")


# Aggressive fetch — for content pages that block simple requests
def fetch_aggressive(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.com/",
    }
    for attempt in range(2):
        try:
            r = cffi_requests.get(url, headers=headers, impersonate="chrome124", timeout=15)
            if r.status_code == 200:
                return BeautifulSoup(r.content, "lxml")
            if r.status_code in (403, 503) and attempt == 0:
                time.sleep(2)
                continue
            raise Exception(f"Trang chặn truy cập (HTTP {r.status_code}). Thử dùng link all.html từ 69read.net")
        except Exception as e:
            if attempt == 1:
                raise
            time.sleep(2)
    raise Exception("Không thể tải nội dung chương")


# Smart fetch — try simple first, then aggressive
def fetch_content(url):
    # Try simple first (fast)
    try:
        return fetch_simple(url)
    except:
        pass
    # Then aggressive with retries
    return fetch_aggressive(url)


# ====================== ALL.HTML ======================
TITLE_RE = re.compile(r'第[零一二三四五六七八九十百千万\d]{1,10}[章节回集卷]\s*[^\n]{0,60}')
NOISE_RE = re.compile(
    r"(推荐|收藏|上一[章页]|下一[章页]|目录|返回|广告|本站|书签|加入书架|"
    r"投票|打赏|www\.|\.com|\.net|http|最新章节|手机阅读|请牢记|备用域名)"
)

def is_all_page(url):
    return "all.htm" in urlparse(url).path.lower()

def load_all_html(url):
    if url in ALLHTML_STORE: return ALLHTML_STORE[url]
    cached = cget(url, "allhtml_")
    if cached and cached.get("chapters"):
        ALLHTML_STORE[url] = cached
        return cached
    soup = fetch_simple(url)
    title = ""
    t = soup.find("title")
    if t: title = t.get_text(strip=True).split("_")[0].split("-")[0].strip()
    for tag in soup.find_all(["script","style","iframe","header","footer","nav"]): tag.decompose()
    el = None
    for sel in ["#content","#all","#at",".content","#BookText","#chaptercontent",".txtnav","#txt"]:
        c = soup.select_one(sel)
        if c and len(c.get_text(strip=True))>500: el=c; break
    if not el:
        best, bl = None, 0
        for c in soup.find_all(["div","article","section"]):
            txt = c.get_text(strip=True)
            if len(txt)>bl: best,bl = c,len(txt)
        if best and bl>500: el=best
    if not el: return {"title":title,"chapters":[]}
    for br in el.find_all("br"): br.replace_with("\n")
    full = el.get_text(separator="\n")
    matches = list(TITLE_RE.finditer(full))
    chapters = []
    for i,m in enumerate(matches):
        ch_title = m.group().strip()
        s,e = m.end(), matches[i+1].start() if i+1<len(matches) else len(full)
        lines = [l.strip() for l in full[s:e].split("\n") if l.strip()]
        lines = [l for l in lines if not NOISE_RE.search(l)]
        content = "\n\n".join(lines)
        if len(content)>30: chapters.append({"title":ch_title,"content":content})
    result = {"title":title,"chapters":chapters}
    ALLHTML_STORE[url] = result
    cset(url, result, "allhtml_")
    print(f"[OK] {len(chapters)} chapters from all.html")
    return result


# ====================== CHAPTERS ======================
def get_chapters(index_url):
    cached = cget(index_url, "ch_")
    if cached: return cached

    # 69shuba: normalize .htm → /
    if "69shu" in index_url and (index_url.endswith('.htm') or index_url.endswith('.html')):
        index_url = index_url.rsplit('/', 1)[0] + '/'

    soup = fetch_simple(index_url)
    selectors = ["#list a",".listmain a",".chapter-list a",".mulu a","#chapterList a",
                 "dd a",".book-list a",".chapters a","#dir a",".catalog li a",
                 "#catalog a",".mu_contain a",".centent a","ul.mulu_list a"]
    links = []
    for sel in selectors: links.extend(soup.select(sel))
    chapters, seen = [], set()
    for a in links:
        href, title = a.get("href",""), a.get_text(strip=True)
        if href and title and len(title)>2 and re.search(r'[\u4e00-\u9fff]', title):
            full = urljoin(index_url, href)
            if full not in seen: seen.add(full); chapters.append({"title":title,"url":full})
    cset(index_url, chapters, "ch_")
    return chapters


# ====================== CONTENT ======================
def get_content(url):
    cached = cget(url, "raw_")
    if cached: return cached

    soup = fetch_content(url)  # tries simple then aggressive
    for tag in soup.find_all(["script","style","iframe","header","footer","nav"]): tag.decompose()
    selectors = ["#content","#chaptercontent",".chapter-content","#BookText",
                 ".read-content",".txtnav","#txt",".book-content"]
    el = None
    for sel in selectors:
        c = soup.select_one(sel)
        if c and len(c.get_text(strip=True))>100: el=c; break
    if not el: return ""
    for br in el.find_all("br"): br.replace_with("\n")
    lines = [l.strip() for l in el.get_text(separator="\n").split("\n") if l.strip()]
    cleaned = [l for l in lines if not NOISE_RE.search(l)]
    final = "\n\n".join(cleaned)
    cset(url, final, "raw_")
    return final


# ====================== MEMORY ======================
def get_memory(u):
    return cget(u,"mem_") or {"characters":{},"places":{},"terms":{},"summary":"","n":0}

def build_mem_block(mem, glossary=""):
    terms = {}
    for d in [mem.get("characters",{}),mem.get("places",{}),mem.get("terms",{})]: terms.update(d)
    if glossary.strip():
        for line in glossary.strip().split("\n"):
            if "=" in line: k,v=line.split("=",1); terms[k.strip()]=v.strip()
    parts = []
    if terms: parts.append("【THUẬT NGỮ】\n"+"\n".join(f"  {k}={v}" for k,v in terms.items()))
    if mem.get("summary"): parts.append(f"【BỐI CẢNH】{mem['summary']}")
    return "\n\n".join(parts)

def update_mem(cn, vn, mem, url):
    if not AI_CLIENT: return mem
    try:
        r = AI_CLIENT.chat.completions.create(model=MODEL, messages=[{"role":"user","content":
            f'JSON: "characters"(Trung→Việt),"places","terms","summary"(1câu Việt). Biết:{json.dumps(mem.get("characters",{}),ensure_ascii=False)[:300]}\nGỐC:{cn[:1200]}\nDỊCH:{vn[:1200]}\nCHỈ JSON.'}],
            temperature=0.1, max_tokens=1500)
        raw = re.sub(r'^```json\s*','',r.choices[0].message.content.strip())
        raw = re.sub(r'\s*```$','',raw)
        d = json.loads(raw)
        for k in ["characters","places","terms"]: mem.setdefault(k,{}).update(d.get(k,{}))
        mem["summary"]=d.get("summary",mem.get("summary",""))
        mem["n"]=mem.get("n",0)+1
        cset(url,mem,"mem_")
    except: pass
    return mem

def init_mem(url, text):
    mem = get_memory(url)
    if mem.get("n",0)>0 or mem.get("characters"): return mem
    if not AI_CLIENT: return mem
    try:
        r = AI_CLIENT.chat.completions.create(model=MODEL, messages=[{"role":"user","content":
            f'Tiểu thuyết TQ. JSON:"characters"(Trung→Hán-Việt),"places","terms","genre","summary"(1câu Việt),"novel_title_vi"\n{text[:3500]}\nCHỈ JSON.'}],
            temperature=0.1, max_tokens=2000)
        raw = re.sub(r'^```json\s*','',r.choices[0].message.content.strip())
        raw = re.sub(r'\s*```$','',raw)
        d = json.loads(raw)
        for k in ["characters","places","terms"]: mem[k]=d.get(k,{})
        for k in ["genre","novel_title_vi","summary"]: mem[k]=d.get(k,"")
        mem["n"]=0; cset(url,mem,"mem_")
    except Exception as e: print(f"[ERR] mem init: {e}")
    return mem


# ====================== TRANSLATE ======================
STYLES = {
    "cotrang":"Dịch cổ trang, trang trọng, kiếm hiệp.",
    "hiendai":"Dịch hiện đại, tự nhiên, dễ đọc.",
    "langman":"Dịch lãng mạn, trữ tình.",
    "satnghia":"Dịch sát nghĩa, chính xác.",
    "nguyenban":"Giữ phong cách gốc, cân bằng chính xác và tự nhiên.",
}

def translate(text, glossary="", style="nguyenban", custom_prompt="", ch_url="", novel_url=""):
    if not text.strip(): return "Không có nội dung."
    if not AI_CLIENT: return "[Lỗi] Chưa có API key. Thêm GEMINI_API_KEY hoặc XAI_API_KEY."
    ck = f"{ch_url}__s_{style}" if ch_url else ""
    if ck:
        c = cget(ck,"tr_")
        if c: return c
    mem = get_memory(novel_url) if novel_url else {}
    mb = build_mem_block(mem, glossary)
    tone = custom_prompt.strip() if custom_prompt.strip() else STYLES.get(style,STYLES["nguyenban"])
    paras = text.split("\n\n"); chunks,cur = [],""
    for p in paras:
        if len(cur)+len(p)>5000 and cur: chunks.append(cur); cur=p
        else: cur=cur+"\n\n"+p if cur else p
    if cur: chunks.append(cur)
    results = []
    for i,chunk in enumerate(chunks):
        try:
            r = AI_CLIENT.chat.completions.create(model=MODEL, messages=[
                {"role":"system","content":"Dịch giả tiểu thuyết Trung-Việt hàng đầu. Tên/thuật ngữ PHẢI theo bảng."},
                {"role":"user","content":f"Dịch sang Việt. {tone}\n{mb}\nDịch TOÀN BỘ. Hán-Việt nhất quán. CHỈ bản dịch.\n\n{chunk}"}],
                temperature=0.3, max_tokens=8000)
            results.append(r.choices[0].message.content.strip())
        except Exception as e: results.append(f"[Lỗi dịch {i+1}: {e}]")
    final = "\n\n".join(results)
    if ck: cset(ck,final,"tr_")
    if novel_url:
        try: update_mem(text, final, mem, novel_url)
        except: pass
    return final


# ====================== ROUTES ======================
@app.route("/")
def index(): return render_template("index.html")

@app.route("/health")
def health():
    return jsonify({"status":"ok","model":MODEL,"key":"OK" if AI_CLIENT else "MISSING",
                    "allhtml":list(ALLHTML_STORE.keys())})

@app.route("/api/chapters", methods=["POST"])
def api_chapters():
    url = request.json.get("url","").strip()
    if not url: return jsonify({"error":"Nhập URL"}), 400
    try:
        if is_all_page(url):
            data = load_all_html(url)
            chs = data.get("chapters",[])
            if not chs: return jsonify({"error":"Không tìm thấy chương."}), 404
            return jsonify({"chapters":[{"title":c["title"],"url":f"{url}#ch_{i}"} for i,c in enumerate(chs)],
                            "novel_title":data.get("title",""),"mode":"all_html","novel_url":url})
        else:
            chs = get_chapters(url)
            if not chs: return jsonify({"error":"Không tìm thấy mục lục."}), 404
            title = ""
            try: t=fetch_simple(url).find("title"); title=t.get_text(strip=True).split("_")[0].strip() if t else ""
            except: pass
            return jsonify({"chapters":chs,"novel_title":title,"mode":"standard","novel_url":url})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/init_memory", methods=["POST"])
def api_init_memory():
    d = request.json; nu = d.get("novel_url","").strip()
    if not nu: return jsonify({"error":"Thiếu URL"}), 400
    mem = get_memory(nu)
    if mem.get("n",0)>0 or mem.get("characters"):
        return jsonify({"status":"ready","memory":_ms(mem)})
    text = ""
    # For all.html: get first chapter from memory store
    if nu in ALLHTML_STORE and ALLHTML_STORE[nu].get("chapters"):
        text = ALLHTML_STORE[nu]["chapters"][0].get("content","")
    # For standard: try fetching first chapter
    if not text:
        fu = d.get("first_chapter_url","")
        if fu:
            try: text = get_content(fu)
            except: pass
    if text and len(text)>100: mem = init_mem(nu, text)
    return jsonify({"status":"initialized","memory":_ms(mem)})

@app.route("/api/translate", methods=["POST"])
def api_translate():
    d = request.json
    cu = d.get("url","").strip()
    nu = d.get("novel_url","").strip()
    if not cu: return jsonify({"error":"Thiếu URL chương"}), 400

    raw = None

    # ALL.HTML: read from memory store
    if "#ch_" in cu:
        base = cu.split("#ch_")[0]
        try:
            idx = int(cu.split("#ch_")[1])
        except: idx = -1
        # From memory
        if base in ALLHTML_STORE:
            chs = ALLHTML_STORE[base].get("chapters",[])
            if 0<=idx<len(chs): raw = chs[idx].get("content","")
        # From disk cache
        if not raw:
            data = cget(base,"allhtml_")
            if data and data.get("chapters"):
                ALLHTML_STORE[base] = data
                chs = data["chapters"]
                if 0<=idx<len(chs): raw = chs[idx].get("content","")

    # STANDARD: fetch the chapter page (with aggressive retry)
    if not raw and "#ch_" not in cu:
        raw = cget(cu, "raw_")
        if not raw:
            try:
                raw = get_content(cu)
            except Exception as e:
                return jsonify({"error":f"Không tải được chương: {e}"}), 500

    if not raw:
        return jsonify({"error":"Không tìm thấy nội dung chương."}), 500

    vn = translate(raw, d.get("glossary",""), d.get("style","nguyenban"),
                   d.get("custom_prompt",""), cu, nu)
    mem = get_memory(nu) if nu else {}
    return jsonify({"translation":vn,"memory":_ms(mem)})

def _ms(m):
    return {"characters":len(m.get("characters",{})),"places":len(m.get("places",{})),
            "terms":len(m.get("terms",{})),"n":m.get("n",0),
            "genre":m.get("genre",""),"novel_title_vi":m.get("novel_title_vi","")}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
