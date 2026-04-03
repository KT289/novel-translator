import os
import re
import json
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

print(f"[BOOT] Model={MODEL or 'NONE'}, Key={'SET' if AI_CLIENT else 'MISSING'}")

CACHE = Path("cache")
CACHE.mkdir(exist_ok=True)

# ====================== IN-MEMORY CHAPTER STORE ======================
# With workers=1, this dict persists across ALL requests in the same process.
# This is the PRIMARY store for all.html chapter content.
CHAPTERS_STORE = {}  # { novel_url: [ {"title": "...", "content": "..."}, ... ] }
NOVEL_TITLES = {}    # { novel_url: "title" }


# ====================== DISK CACHE (backup) ======================
def _ck(url): return hashlib.md5(url.encode()).hexdigest()

def disk_get(url, prefix=""):
    p = CACHE / f"{prefix}{_ck(url)}.json"
    try: return json.loads(p.read_text("utf-8")) if p.exists() else None
    except: return None

def disk_set(url, data, prefix=""):
    try: (CACHE / f"{prefix}{_ck(url)}.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    except Exception as e: print(f"[WARN] Disk write fail: {e}")


# ====================== FETCH ======================
def fetch(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,zh-CN;q=0.8",
    }
    r = cffi_requests.get(url, headers=headers, impersonate="chrome", timeout=60)
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}")
    return BeautifulSoup(r.content, "lxml")


# ====================== ALL.HTML PARSING ======================
TITLE_RE = re.compile(r'第[零一二三四五六七八九十百千万\d]{1,10}[章节回集卷]\s*[^\n]{0,60}')
NOISE_RE = re.compile(
    r"(推荐|收藏|上一[章页]|下一[章页]|目录|返回|广告|本站|书签|加入书架|"
    r"投票|打赏|www\.|\.com|\.net|http|最新章节|手机阅读|请牢记|备用域名)"
)

def is_all_page(url):
    return "all.htm" in urlparse(url).path.lower()

def load_all_html(url):
    """Parse all.html and store chapters in memory. Returns chapter count."""
    # Already in memory?
    if url in CHAPTERS_STORE:
        return len(CHAPTERS_STORE[url])

    # Try disk cache
    cached = disk_get(url, "allhtml_")
    if cached and cached.get("chapters"):
        CHAPTERS_STORE[url] = cached["chapters"]
        NOVEL_TITLES[url] = cached.get("title", "")
        print(f"[CACHE] Loaded {len(cached['chapters'])} chapters from disk")
        return len(cached["chapters"])

    # Fetch and parse
    print(f"[FETCH] Loading {url} ...")
    soup = fetch(url)
    title_tag = soup.find("title")
    novel_title = title_tag.get_text(strip=True).split("_")[0].split("-")[0].strip() if title_tag else ""
    for tag in soup.find_all(["script", "style", "iframe", "header", "footer", "nav"]):
        tag.decompose()

    # Find content area
    content_el = None
    for sel in ["#content", "#all", "#at", ".content", "#BookText",
                "#chaptercontent", ".chapter-content", ".txtnav", "#txt"]:
        c = soup.select_one(sel)
        if c and len(c.get_text(strip=True)) > 500:
            content_el = c
            break
    if not content_el:
        best, bl = None, 0
        for c in soup.find_all(["div", "article", "section"]):
            t = c.get_text(strip=True)
            if len(t) > bl: best, bl = c, len(t)
        if best and bl > 500: content_el = best
    if not content_el:
        return 0

    for br in content_el.find_all("br"): br.replace_with("\n")
    full_text = content_el.get_text(separator="\n")

    # Split by chapter titles
    matches = list(TITLE_RE.finditer(full_text))
    if not matches:
        return 0

    chapters = []
    for i, m in enumerate(matches):
        title = m.group().strip()
        start = m.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(full_text)
        lines = [l.strip() for l in full_text[start:end].split("\n") if l.strip()]
        lines = [l for l in lines if not NOISE_RE.search(l)]
        content = "\n\n".join(lines)
        if len(content) > 30:
            chapters.append({"title": title, "content": content})

    # Store in memory (PRIMARY) and disk (backup)
    CHAPTERS_STORE[url] = chapters
    NOVEL_TITLES[url] = novel_title
    disk_set(url, {"title": novel_title, "chapters": chapters}, "allhtml_")
    print(f"[OK] Parsed {len(chapters)} chapters, title='{novel_title}'")
    return len(chapters)


# ====================== STANDARD PARSING ======================
def get_chapters_standard(url):
    cached = disk_get(url, "chapters_")
    if cached: return cached
    soup = fetch(url)
    selectors = ["#list a",".listmain a",".chapter-list a",".mulu a","#chapterList a",
                 "dd a",".book-list a",".chapters a","#dir a",".catalog li a",
                 "#catalog a",".centent a","ul.mulu_list a",".mu_contain a"]
    links = []
    for sel in selectors: links.extend(soup.select(sel))
    chapters, seen = [], set()
    for a in links:
        href, title = a.get("href",""), a.get_text(strip=True)
        if href and title and len(title)>2 and re.search(r'[\u4e00-\u9fff]', title):
            full = urljoin(url, href)
            if full not in seen: seen.add(full); chapters.append({"title":title,"url":full})
    disk_set(url, chapters, "chapters_")
    return chapters

def get_content_standard(url):
    cached = disk_get(url, "raw_")
    if cached: return cached
    soup = fetch(url)
    for t in soup.find_all(["script","style","iframe","header","footer","nav"]): t.decompose()
    for sel in ["#content","#chaptercontent",".chapter-content","#BookText",".read-content",".txtnav","#txt"]:
        c = soup.select_one(sel)
        if c and len(c.get_text(strip=True))>100:
            for br in c.find_all("br"): br.replace_with("\n")
            lines = [l.strip() for l in c.get_text(separator="\n").split("\n") if l.strip()]
            final = "\n\n".join(l for l in lines if not NOISE_RE.search(l))
            disk_set(url, final, "raw_")
            return final
    return ""


# ====================== MEMORY ======================
def get_memory(url):
    return disk_get(url, "memory_") or {"characters":{},"places":{},"terms":{},"summary":"","n":0}

def build_memory_block(mem, glossary=""):
    terms = {}
    for d in [mem.get("characters",{}),mem.get("places",{}),mem.get("terms",{})]: terms.update(d)
    if glossary.strip():
        for line in glossary.strip().split("\n"):
            if "=" in line: k,v=line.split("=",1); terms[k.strip()]=v.strip()
    parts = []
    if terms: parts.append("【THUẬT NGỮ】\n"+"\n".join(f"  {k}={v}" for k,v in terms.items()))
    if mem.get("summary"): parts.append(f"【BỐI CẢNH】 {mem['summary']}")
    return "\n\n".join(parts)

def update_memory(cn, vn, mem, url):
    if not AI_CLIENT: return mem
    try:
        r = AI_CLIENT.chat.completions.create(model=MODEL, messages=[{"role":"user","content":
            f'Trích xuất JSON: "characters"(Trung→Việt),"places","terms","summary"(1câu Việt). Đã biết:{json.dumps(mem.get("characters",{}),ensure_ascii=False)[:400]}\nGỐC:{cn[:1500]}\nDỊCH:{vn[:1500]}\nCHỈ JSON.'}],
            temperature=0.1, max_tokens=1500)
        raw = re.sub(r'^```json\s*','',r.choices[0].message.content.strip())
        raw = re.sub(r'\s*```$','',raw)
        d = json.loads(raw)
        for k in ["characters","places","terms"]: mem.setdefault(k,{}).update(d.get(k,{}))
        mem["summary"] = d.get("summary", mem.get("summary",""))
        mem["n"] = mem.get("n",0)+1
        disk_set(url, mem, "memory_")
    except: pass
    return mem

def init_memory(url, text):
    mem = get_memory(url)
    if mem.get("n",0)>0 or mem.get("characters"): return mem
    if not AI_CLIENT: return mem
    try:
        r = AI_CLIENT.chat.completions.create(model=MODEL, messages=[{"role":"user","content":
            f'Phân tích chương đầu tiểu thuyết TQ. JSON:"characters"(Trung→Hán-Việt),"places","terms","genre","summary"(1câu Việt),"novel_title_vi"\n\n{text[:4000]}\n\nCHỈ JSON.'}],
            temperature=0.1, max_tokens=2000)
        raw = re.sub(r'^```json\s*','',r.choices[0].message.content.strip())
        raw = re.sub(r'\s*```$','',raw)
        d = json.loads(raw)
        for k in ["characters","places","terms"]: mem[k]=d.get(k,{})
        for k in ["genre","novel_title_vi","summary"]: mem[k]=d.get(k,"")
        mem["n"]=0; disk_set(url, mem, "memory_")
    except Exception as e: print(f"[ERR] Memory init: {e}")
    return mem


# ====================== TRANSLATE ======================
STYLES = {
    "cotrang":"Dịch phong cách cổ trang, ngôn ngữ trang trọng, kiếm hiệp.",
    "hiendai":"Dịch tự nhiên, hiện đại, dễ đọc.",
    "langman":"Dịch lãng mạn, trữ tình, giàu cảm xúc.",
    "satnghia":"Dịch sát nghĩa, chính xác từng câu.",
    "nguyenban":"Giữ nguyên phong cách gốc, cân bằng chính xác và tự nhiên.",
}

def translate(text, glossary="", style="nguyenban", custom_prompt="", chapter_url="", novel_url=""):
    if not text.strip(): return "Không có nội dung."
    if not AI_CLIENT: return "[Lỗi] Chưa cấu hình API key."
    ck = f"{chapter_url}__s_{style}" if chapter_url else ""
    if ck:
        c = disk_get(ck, "tr_")
        if c: return c
    mem = get_memory(novel_url) if novel_url else {}
    mb = build_memory_block(mem, glossary)
    tone = custom_prompt.strip() if custom_prompt.strip() else STYLES.get(style, STYLES["nguyenban"])
    paras = text.split("\n\n"); chunks, cur = [], ""
    for p in paras:
        if len(cur)+len(p)>5000 and cur: chunks.append(cur); cur=p
        else: cur = cur+"\n\n"+p if cur else p
    if cur: chunks.append(cur)
    results = []
    for i, chunk in enumerate(chunks):
        try:
            r = AI_CLIENT.chat.completions.create(model=MODEL, messages=[
                {"role":"system","content":"Bạn là dịch giả tiểu thuyết Trung-Việt hàng đầu. Tên/thuật ngữ PHẢI theo bảng thuật ngữ."},
                {"role":"user","content":f"Dịch sang Việt. {tone}\n{mb}\nDịch TOÀN BỘ. Hán-Việt nhất quán. Giữ phân đoạn. CHỈ trả bản dịch.\n\n{chunk}"}],
                temperature=0.3, max_tokens=8000)
            results.append(r.choices[0].message.content.strip())
        except Exception as e: results.append(f"[Lỗi dịch {i+1}: {e}]")
    final = "\n\n".join(results)
    if ck: disk_set(ck, final, "tr_")
    if novel_url:
        try: update_memory(text, final, mem, novel_url)
        except: pass
    return final

def translate_titles(titles, novel_url=""):
    if not AI_CLIENT: return titles
    ck = f"{novel_url}__titles"
    cached = disk_get(ck, "titlevn_")
    if cached: return cached
    mem = get_memory(novel_url) if novel_url else {}
    mb = build_memory_block(mem)
    all_vn = []
    for start in range(0, len(titles), 80):
        batch = titles[start:start+80]
        numbered = "\n".join(f"{i+1}. {t}" for i,t in enumerate(batch))
        try:
            r = AI_CLIENT.chat.completions.create(model=MODEL, messages=[{"role":"user","content":
                f"Dịch tiêu đề chương sang Việt (Hán-Việt cho tên riêng).\n{mb}\nTrả về ĐÚNG số dòng: số. tiêu đề\n\n{numbered}"}],
                temperature=0.1, max_tokens=4000)
            for line in r.choices[0].message.content.strip().split("\n"):
                line = line.strip()
                if line and line[0].isdigit():
                    parts = line.split(".",1); all_vn.append(parts[1].strip() if len(parts)>1 else line)
                elif line: all_vn.append(line)
        except: all_vn.extend(batch)
    while len(all_vn)<len(titles): all_vn.append(titles[len(all_vn)])
    result = all_vn[:len(titles)]
    disk_set(ck, result, "titlevn_")
    return result


# ====================== ROUTES ======================
@app.route("/")
def index(): return render_template("index.html")

@app.route("/health")
def health():
    return jsonify({"status":"ok","model":MODEL,"key":"set" if AI_CLIENT else "MISSING",
                    "novels_in_memory":list(CHAPTERS_STORE.keys())})

@app.route("/api/chapters", methods=["POST"])
def api_chapters():
    url = request.json.get("url","").strip()
    if not url: return jsonify({"error":"Vui lòng nhập URL"}), 400

    try:
        if is_all_page(url):
            count = load_all_html(url)
            if count == 0:
                return jsonify({"error":"Không tìm thấy chương nào trong trang all.html"}), 404
            chs = CHAPTERS_STORE[url]
            ch_list = [{"title":ch["title"],"url":f"{url}#ch_{i}"} for i,ch in enumerate(chs)]
            return jsonify({"chapters":ch_list,"novel_title":NOVEL_TITLES.get(url,""),
                            "mode":"all_html","novel_url":url})
        else:
            # Standard chapter listing (individual URLs)
            chs = get_chapters_standard(url)
            if not chs: return jsonify({"error":"Không tìm thấy mục lục."}), 404
            title = ""
            try: t=fetch(url).find("title"); title=t.get_text(strip=True).split("_")[0].strip() if t else ""
            except: pass
            return jsonify({"chapters":chs,"novel_title":title,"mode":"standard","novel_url":url})
    except Exception as e:
        msg = str(e)
        # Helpful error messages for known blocked sites
        host = urlparse(url).hostname or ""
        if "403" in msg and ("69shuba" in host or "69shu" in host):
            msg = ("69shuba.com chặn truy cập từ server. "
                   "Hãy dùng link all.html từ 69read.net thay thế. "
                   "VD: https://www.69read.net/txt/194249/all.html")
        return jsonify({"error": msg}), 500

@app.route("/api/init_memory", methods=["POST"])
def api_init_memory():
    d = request.json; nu = d.get("novel_url","").strip()
    if not nu: return jsonify({"error":"Thiếu URL"}), 400
    mem = get_memory(nu)
    if mem.get("n",0)>0 or mem.get("characters"):
        return jsonify({"status":"ready","memory":_ms(mem)})
    text = ""
    if nu in CHAPTERS_STORE and CHAPTERS_STORE[nu]:
        text = CHAPTERS_STORE[nu][0].get("content","")
    if text and len(text)>100:
        mem = init_memory(nu, text)
    return jsonify({"status":"initialized","memory":_ms(mem)})

@app.route("/api/translate_titles", methods=["POST"])
def api_translate_titles():
    d = request.json; titles=d.get("titles",[]); nu=d.get("novel_url","")
    if not titles: return jsonify({"error":"Thiếu titles"}), 400
    return jsonify({"titles_vi": translate_titles(titles, nu)})

@app.route("/api/translate", methods=["POST"])
def api_translate():
    d = request.json
    cu = d.get("url","").strip()
    nu = d.get("novel_url","").strip()
    if not cu: return jsonify({"error":"Thiếu URL chương"}), 400

    raw = None

    # === ALL.HTML MODE: read from in-memory store ===
    if "#ch_" in cu:
        try:
            base = cu.split("#ch_")[0]
            idx = int(cu.split("#ch_")[1])
            # Primary: in-memory
            if base in CHAPTERS_STORE:
                chs = CHAPTERS_STORE[base]
                if 0 <= idx < len(chs):
                    raw = chs[idx].get("content","")
                    if raw:
                        print(f"[OK] Ch {idx} from memory ({len(raw)} chars)")
            # Backup: disk cache
            if not raw:
                data = disk_get(base, "allhtml_")
                if data and data.get("chapters"):
                    CHAPTERS_STORE[base] = data["chapters"]  # restore memory
                    if 0 <= idx < len(data["chapters"]):
                        raw = data["chapters"][idx].get("content","")
                        if raw: print(f"[OK] Ch {idx} from disk ({len(raw)} chars)")
            # Last resort: re-fetch all.html
            if not raw:
                print(f"[WARN] Re-fetching {base} for ch {idx}")
                load_all_html(base)
                if base in CHAPTERS_STORE:
                    chs = CHAPTERS_STORE[base]
                    if 0 <= idx < len(chs):
                        raw = chs[idx].get("content","")
        except Exception as e:
            print(f"[ERR] Chapter content lookup: {e}")

    # === STANDARD MODE: fetch individual URL ===
    if not raw and "#ch_" not in cu:
        raw = disk_get(cu, "raw_")
        if not raw:
            try: raw = get_content_standard(cu)
            except Exception as e: return jsonify({"error":str(e)}), 500

    if not raw:
        return jsonify({"error":"Không tìm thấy nội dung. Thử quay lại và tải lại truyện."}), 500

    try:
        vn = translate(raw, d.get("glossary",""), d.get("style","nguyenban"),
                       d.get("custom_prompt",""), cu, nu)
        mem = get_memory(nu) if nu else {}
        return jsonify({"translation":vn, "memory":_ms(mem)})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

def _ms(m):
    return {"characters":len(m.get("characters",{})),"places":len(m.get("places",{})),
            "terms":len(m.get("terms",{})),"n":m.get("n",0),
            "genre":m.get("genre",""),"novel_title_vi":m.get("novel_title_vi","")}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
