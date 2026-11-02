"""
build_rag_data.py — 把相機說明書 PDF 轉成 RAG 用的文字切片，並打包成 rag-data.zip

流程：
  1. 讀取：掃描指定資料夾裡所有 PDF，用 PyMuPDF 萃取文字
  2. 切分：優先依 PDF 書籤（目錄）切成章節；太長的再依段落拆、太短的合併，
           讓每段落在約 500–1000 字
  3. 打包：每段存成一個 .md（開頭附 metadata），連同 metadata.jsonl 壓成 rag-data.zip

用法：
  pip install pymupdf
  python build_rag_data.py                       # 預設讀 ./pdfs，輸出 ./rag-data.zip
  python build_rag_data.py -i 某資料夾 -o out.zip --min 500 --max 1000
"""

import argparse
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("找不到 PyMuPDF，請先執行：pip install pymupdf")

# 這些書籤是目錄／索引頁，內容只有頁碼，對檢索沒幫助，直接略過
SKIP_TITLES = {"章節索引", "目錄", "索引", "Contents", "Index"}

# 判斷「頁碼／章節編號」這種頁首頁尾雜訊：純數字或羅馬數字
PAGE_NUM_RE = re.compile(r"^\s*([0-9]{1,4}|[ivxlcdmIVXLCDM]{1,6})\s*$")
# 私有區字元（說明書裡的小圖示，轉成文字後是亂碼）
PUA_RE = re.compile(r"[-]")
# 目錄的點點引導線「........」
DOT_LEADER_RE = re.compile(r"\s*\.{4,}\s*")
# 句子結尾，拆長段落時優先在這些位置斷開
SENTENCE_END_RE = re.compile(r"(?<=[。！？；!?])")


# ---------- 1. 讀取與清理 ----------

def open_pdf(path: Path) -> "fitz.Document":
    doc = fitz.open(path)
    # 有些說明書有「擁有者密碼」（禁止編輯但可閱讀），用空密碼即可解開
    if doc.needs_pass and not doc.authenticate(""):
        raise RuntimeError(f"{path.name} 需要密碼才能開啟")
    return doc


def is_cjk(ch: str) -> bool:
    return "⺀" <= ch <= "鿿" or "＀" <= ch <= "￯" or "　" <= ch <= "〿"


def join_lines(lines: List[str]) -> str:
    """PDF 會把一句話硬斷成好幾行，這裡把它接回來。
    中文之間直接相連；英數之間補一個空格；遇到項目符號「•」就保留換行。
    上一行很短或以句號結尾時，多半是表格欄位或段落結束，也保留換行。"""
    out, prev = "", ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not out:
            out = line
        elif (line.startswith("•") or re.match(r"^\d+[.)]\s", line)
              or len(prev) < 14 or prev[-1] in "。！？:："):
            out += "\n" + line
        elif is_cjk(out[-1]) or is_cjk(line[0]):
            out += line
        else:
            out += " " + line
        prev = line
    return out


def clean_block(text: str) -> str:
    text = PUA_RE.sub("", text).replace("\xa0", " ")
    text = DOT_LEADER_RE.sub(" … p.", text)
    lines = [l.strip() for l in text.split("\n")]
    # 說明書常用「重疊兩次文字」做粗體，會變成連續重複的行，去掉
    dedup: List[str] = []
    for l in lines:
        if l and (not dedup or dedup[-1] != l):
            dedup.append(l)
    return join_lines(dedup)


def page_blocks(page: "fitz.Page") -> List[Dict]:
    """取出一頁的文字區塊（由上到下），並過濾頁首、頁尾、頁碼。"""
    h = page.rect.height
    blocks = []
    for x0, y0, x1, y1, text, _no, btype in page.get_text("blocks", sort=True):
        if btype != 0:  # 0 = 文字，1 = 圖片
            continue
        raw = text.strip()
        if not raw or PAGE_NUM_RE.match(raw):
            continue
        # 頁面最上緣的短字串是「頁首章節名」，每頁都重複，略過
        if y1 < h * 0.06 and len(raw) < 30:
            continue
        cleaned = clean_block(raw)
        if cleaned:
            blocks.append({"y": y0, "text": cleaned})
    return blocks


def normalize(s: str) -> str:
    return re.sub(r"\s+", "", PUA_RE.sub("", s))


def find_heading_y(blocks: List[Dict], title: str) -> float:
    """在頁面中找到書籤標題出現的位置（y 座標），找不到就當作從頁首開始。"""
    key = normalize(title)
    for b in blocks:
        first_line = normalize(b["text"].split("\n")[0])
        if first_line.startswith(key) or key in first_line[: len(key) + 4]:
            return b["y"]
    return 0.0


# ---------- 2. 依章節切分 ----------

def extract_sections(doc: "fitz.Document") -> List[Dict]:
    """依 PDF 書籤把全文切成章節。每個書籤負責「自己標題 → 下一個書籤標題」之間的文字。"""
    toc = doc.get_toc(simple=True)  # [層級, 標題, 頁碼(從 1 起算)]
    pages = [page_blocks(p) for p in doc]

    if not toc:
        # 沒有書籤的 PDF：退而求其次，每頁當一段，之後再合併／拆分
        return [
            {"path": [f"第 {i + 1} 頁"], "pages": [i + 1], "text": "\n".join(b["text"] for b in blks)}
            for i, blks in enumerate(pages) if blks
        ]

    # 算出每個書籤的起點（頁索引, y）
    marks = []
    for level, title, pno in toc:
        idx = max(0, min(pno - 1, doc.page_count - 1))
        marks.append({"level": level, "title": title.strip(), "page": idx,
                      "y": find_heading_y(pages[idx], title)})
    # 最後一個書籤之後的終點
    marks.append({"level": 1, "title": "__END__", "page": doc.page_count - 1, "y": float("inf")})

    sections = []
    stack: List[str] = []  # 目前的章節路徑，例如 [準備, 相機部件, 模式轉盤]
    for cur, nxt in zip(marks, marks[1:]):
        stack = stack[: cur["level"] - 1] + [cur["title"]]
        if any(t in SKIP_TITLES for t in stack):
            continue
        texts = []
        for pi in range(cur["page"], nxt["page"] + 1):
            for b in pages[pi]:
                after_start = pi > cur["page"] or b["y"] >= cur["y"]
                before_end = pi < nxt["page"] or b["y"] < nxt["y"]
                if after_start and before_end:
                    texts.append(b["text"])
        # 第一塊通常就是標題本身，去掉避免重複
        if texts and normalize(texts[0]).startswith(normalize(cur["title"])):
            first = texts[0].split("\n", 1)
            texts[0] = first[1] if len(first) > 1 else ""
            if len(normalize(texts[0])) <= len(normalize(cur["title"])):
                texts[0] = texts[0].replace(cur["title"], "", 1)
        body = "\n".join(t for t in texts if t.strip()).strip()
        sections.append({
            "path": list(stack),
            "pages": list(range(cur["page"] + 1, nxt["page"] + 2)),
            "text": body,
        })
    return sections


def split_long(text: str, max_chars: int) -> List[str]:
    """把超過 max_chars 的文字依段落 → 句子切開，每塊不超過 max_chars。"""
    if len(text) <= max_chars:
        return [text]
    pieces: List[str] = []
    for para in text.split("\n"):
        if len(para) <= max_chars:
            pieces.append(para)
        else:
            # 單一段落就超長：改用句號切
            pieces.extend(s for s in SENTENCE_END_RE.split(para) if s)

    chunks, buf = [], ""
    for p in pieces:
        while len(p) > max_chars:  # 連一句都超長（極少見），只好硬切
            chunks.append(p[:max_chars])
            p = p[max_chars:]
        if buf and len(buf) + 1 + len(p) > max_chars:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def build_chunks(sections: List[Dict], min_chars: int, max_chars: int) -> List[Dict]:
    """長的章節拆開、短的章節（同一大章內）合併，讓每段落在 min~max 字之間。"""
    # 先把每個章節拆成不超過 max 的小片，每片帶上自己的小標題
    units = []
    for sec in sections:
        if not sec["text"]:
            continue
        heading = sec["path"][-1]
        for part in split_long(sec["text"], max_chars - len(heading) - 5):
            units.append({"chapter": sec["path"][0], "sections": [" > ".join(sec["path"])],
                          "pages": list(sec["pages"]), "text": f"## {heading}\n{part}"})

    # 只有一兩句話的小節（常是大節的開場白），直接併到下一個小節前面
    i = 0
    while i < len(units) - 1:
        u, nxt = units[i], units[i + 1]
        if (len(u["text"]) < 150 and u["chapter"] == nxt["chapter"]
                and len(u["text"]) + len(nxt["text"]) + 2 <= max_chars):
            nxt["text"] = u["text"] + "\n\n" + nxt["text"]
            nxt["sections"] = u["sections"] + nxt["sections"]
            nxt["pages"] = sorted(set(u["pages"]) | set(nxt["pages"]))
            units.pop(i)
        else:
            i += 1

    chunks: List[Dict] = []
    for u in units:
        last: Optional[Dict] = chunks[-1] if chunks else None
        can_merge = (
            last is not None
            and last["chapter"] == u["chapter"]          # 不跨大章合併
            and len(last["text"]) < min_chars             # 上一段還太短
            and len(last["text"]) + len(u["text"]) + 2 <= max_chars
        )
        if can_merge:
            last["text"] += "\n\n" + u["text"]
            last["sections"] += u["sections"]
            last["pages"] = sorted(set(last["pages"]) | set(u["pages"]))
        else:
            chunks.append(u)
    return chunks


# ---------- 3. 輸出與打包 ----------

def slugify(s: str) -> str:
    return re.sub(r"[^\w一-鿿-]+", "_", s).strip("_")[:40]


def safe_title(s: str) -> str:
    """把章節標題變成合法檔名：去空白、把 / 等檔名禁用字元換成 -"""
    s = re.sub(r"\s+", "", PUA_RE.sub("", s))
    return re.sub(r'[\\/:*?"<>|]+', "-", s)[:30]


def write_chunks(pdf_name: str, chunks: List[Dict], out_dir: Path, meta_rows: List[Dict]) -> None:
    start = len(meta_rows)  # 多份 PDF 時編號接續
    for i, c in enumerate(chunks, start + 1):
        # 檔名格式比照教材：「01_章節名稱.txt」，取這段第一個小節的標題
        title = safe_title(c["sections"][0].split(" > ")[-1])
        chunk_id = f"{i:02d}_{title}"
        fname = f"{chunk_id}.txt"
        meta = {
            "id": chunk_id,
            "source": pdf_name,
            "chapter": c["chapter"],
            "sections": c["sections"],
            "pages": f"{c['pages'][0]}-{c['pages'][-1]}",
            "chars": len(c["text"]),
        }
        # 純文字 .txt（教材的 RAG 程式只讀 TXT）；完整 metadata 另存在 metadata.jsonl
        # 內文最上方放章名與來源頁碼，讓切片單獨被檢索到時仍知道上下文
        context = f"# {c['chapter']}\n來源：{pdf_name}（PDF 第 {meta['pages']} 頁）"
        (out_dir / fname).write_text(f"{context}\n\n{c['text']}\n", encoding="utf-8")
        meta_rows.append({**meta, "file": fname, "text": c["text"]})


def zip_dir(src: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src))


def main() -> None:
    ap = argparse.ArgumentParser(description="PDF 說明書 → RAG 切片 → rag-data.zip")
    ap.add_argument("-i", "--input", default="pdfs", help="放 PDF 的資料夾（預設 ./pdfs）")
    ap.add_argument("-o", "--output", default="rag-data.zip", help="輸出壓縮檔名（預設 rag-data.zip）")
    ap.add_argument("--min", type=int, default=500, help="每段最少字數（預設 500）")
    ap.add_argument("--max", type=int, default=1000, help="每段最多字數（預設 1000）")
    ap.add_argument("--keep", action="store_true", help="保留中間產物資料夾 rag-data/ 方便檢查")
    args = ap.parse_args()

    in_dir = Path(args.input)
    pdfs = sorted(in_dir.glob("*.pdf")) + sorted(in_dir.glob("*.PDF"))
    if not pdfs:
        sys.exit(f"在 {in_dir.resolve()} 找不到 PDF")

    work = Path("rag-data")
    if work.exists():
        shutil.rmtree(work)  # 只清掉這支腳本自己產生的暫存資料夾
    # TXT 直接放在壓縮檔最外層，避免教材程式找不到子資料夾裡的檔案
    chunk_dir = work
    chunk_dir.mkdir(parents=True)

    meta_rows: List[Dict] = []
    for pdf in pdfs:
        doc = open_pdf(pdf)
        sections = extract_sections(doc)
        chunks = build_chunks(sections, args.min, args.max)
        write_chunks(pdf.name, chunks, chunk_dir, meta_rows)
        sizes = [len(c["text"]) for c in chunks]
        print(f"✓ {pdf.name}：{doc.page_count} 頁 → {len(sections)} 個章節 → {len(chunks)} 段"
              f"（字數 {min(sizes)}–{max(sizes)}，平均 {sum(sizes) // len(sizes)}）")

    # 比照教材的輸出格式，列出所有 TXT 檔名
    print(f"教材： {args.output}")
    print(f"TXT 數量： {len(meta_rows)}")
    for row in meta_rows:
        print(f"• {row['file']}")

    # metadata.jsonl：一行一段，含全文，寫向量庫匯入程式時直接逐行讀就好
    with open(work / "metadata.jsonl", "w", encoding="utf-8") as f:
        for row in meta_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    zip_dir(work, Path(args.output))
    if not args.keep:
        shutil.rmtree(work)
    print(f"📦 已輸出 {Path(args.output).resolve()}（共 {len(meta_rows)} 個檔案）")


if __name__ == "__main__":
    main()
