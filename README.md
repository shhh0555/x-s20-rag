# 📷 問問小富｜FUJIFILM X-S20 相機說明書 RAG 對話機器人

114207442 地政二 陳奕安｜第七週作業：打造自己的 RAG 對話機器人

## 檔案說明

| 檔案 | 內容 |
|---|---|
| `rag-data.zip` | **RAG01 產出的向量資料庫**（`index.faiss`、`documents.json`、`index_doc_ids.json`、`config.json`），RAG02 直接從本 repo 下載使用 |
| `text-chunks.zip` | RAG01 的輸入：說明書切成的 78 段 `.txt`（SHA-256 與 `config.json` 記錄的 `source_archive_sha256` 一致） |
| `build_rag_data.py` | 把說明書 PDF 切成 78 段 TXT 並打包的前處理腳本 |

## 給助教：RAG02 讀取網址

```
https://github.com/shhh0555/x-s20-rag/raw/main/rag-data.zip
```

RAG02 第 3 段會自動從這個網址下載並解壓縮，不需要手動上傳；只需要在 Colab「密鑰」設定 `Groq` 即可執行。

## 資料來源

FUJIFILM X-S20 繁體中文基本操作手冊（`x-s20_manual_zht_s_f.pdf`，160 頁），官方下載：<https://fujifilm-dsc.com/en-int/manual/>

## 處理流程

1. **前處理**：`build_rag_data.py` 用 PyMuPDF 抽出 PDF 文字，依 PDF 書籤（章節目錄）切段，太長的依段落拆開、太短的同章合併，每段約 500–1000 字，共 78 段。
2. **RAG01**：用 `google/embeddinggemma-2` 把 78 段轉成 768 維向量，存成 FAISS `IndexFlatIP`（正規化後內積 = 餘弦相似度），匯出 `rag-data.zip`。
3. **RAG02**：下載 `rag-data.zip` → 問題向量化 → FAISS 找最相近的 3 段 → 交給 Groq `openai/gpt-oss-120b` 依「小富」人設回答 → Gradio 展示。
