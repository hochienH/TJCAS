# TJCAS: Taiwanese Judgments Citation Annotating System

> [!NOTE] 
> TLDR: 這是一個針對臺灣司法院公開判決的自動標註系統
> - input(str): 判決檔案名稱
> - output(list): 在這篇判決當中有提及的所有判決檔案名稱（理論）
> - 特色：有別於過去純粹使用 regex 的保守做法，這個專案所採用的做法較為「激進」，使用 regex 拿到 X年Y字第Z號 這樣的資料後，直接透過 NLP 技術比對所有開放資料中最相近的判決，作為 annotation 的結果
> - 缺點：司法院公開資料僅從 1996 年開始，在此之前無法確定全文就只能標註不確定

## Project Status
In Progress

第一階段（regex 抽取）已完成：樣本抽樣、引用 pattern、字別字典與遺漏稽核工具。第二階段（全文比對）的管線已實作完成並通過小規模測試，全語料的大規模執行尚未進行。

## Quick Start

```bash
# 1. 建立環境
virtualenv --python python3.12 .venv
.venv/bin/python -m pip install -e .

# 2. 設定資料位置
echo 'DATA_ROUTE = "/path/to/JRAR"' > .env

# 3. 抽樣（每個月份壓縮檔取最大的一篇判決）
.venv/bin/python scripts/sample_cases.py

# 4. 建字別字典
.venv/bin/python scripts/build_case_types.py

# 5. 跑 pattern 稽核
.venv/bin/python scripts/audit_patterns.py
```


## Motivation
我國的司法院判決撰寫引用判決的格式非常不固定，比如：
- 中規中矩版：最高法院99年度台上字第7939號判決參照
- 頓號連寫版：最高法院69年台上字第3752號原判例、111年度台上字第545號判決意旨參照
- 禮貌引用版：鈞院七十六年判字第四七四號判決
- 原審很多版：原審法院98年度訴字第1866號判決
- 前審判決寫法不固定版：上訴人對於中華民國八十八年六月二十二日臺灣高
等法院第二審判決（八十七年度上國字第一九號）

...等等

regex 的條件寫的一旦多起來，很容易會導致邏輯衝突而匹配到不該匹配的判決，或是又有遺漏（以上的幾種情形在短短的20篇判決內都能找到，可想而知 pattern 可說是不勝枚舉）然而，睽諸不同形式的寫法，恆常不動的就是X年Y字第Z號的段落，因此真正讓 regex 發揮的舞台，應該建構在這些段落之上

然而，如果只使用這樣的方式去判定，那麼很有可能有數篇判決被匹配。此時我們可以透過判決內文的比對來確定實際上被引用的判決應該是哪一篇，至於判決內文比對的方法，從輕量化的 TFIDF-vectorization, wordbag 到很厚重的 embedding system 都可以應用到本系統中。本系統致力在建構一個兩階段框架，我們負責把 regex 做好跟提供一些內文比對的選項，但如果你有更好的解決方案，歡迎你使用我們的系統進行改造。

## Methodology

### 第一階段：regex 抽取

採「寬鬆抽取 + 後處理分類」的兩層做法，regex 本身**不**綁字別白名單，避免為了提高 precision 而牺牲 recall。

核心 pattern 拆成三個欄位：

| 欄位 | 實際遇到的寫法 |
|---|---|
| X（年） | `89`、`八十八`、`一百零四`，`年` 後面可有可無 `度` |
| Y（字別） | `台上`、`重訴`、`簡上`、`調解醫偵` 等 6,864 種 |
| Z（案號） | `7939`、`七四四`、`八Ｏ五二`（以 O 代替零）、`123之1`、頓號連寫 `第一、二、三號` |

判決原文有兩種排版會切斷引用字串，必須先正規化：固定寬度換行插入的 `\r\n` 與縮排（`89\r    年度偵字第4337號`），以及附表的框線（`89年促字第││    26070號`）。

### 字別分類

抽出的引用依字別分成三類，字典來源有三層：

| 來源 | 內容 | 用途 |
|---|---|---|
| `observed` | 從開放資料檔名（`法院,年度,字別,案號,日期,序號`）統計出的 6,864 種字別 | 法院裁判的權威清單，含最高法院、行政法院 |
| `court` | 臺灣高等法院以下各級法院民刑事案件案號字別及案件種類對照表 | 提供字別的語意（民/刑、審級、更審） |
| `prosecution` | 檢察案件編號計數分案報結實施要點的 345 種字別 | 排除清單，`偵`、`他`、`相` 等是檢方卷證而非法院裁判 |

分類優先序：`observed` → `prosecution` → `court` → `unknown`。落在 `unknown` 的就是待檢清單。

### 遺漏偵測

單靠人工看無法確認 regex 有沒漏抓，因此除了終極 pattern 之外另寫了一組刻意寬鬆的 partial pattern（把 X、Y、Z 其中一個欄位放寬成 `.{1,25}?`）。每輪同時跑全部 pattern，**寬鬆 pattern 抓到但終極 pattern 沒抓到**的那些就是遺漏，連同上下文寫到 `pattern_gaps.csv` 供人工檢視，再回頭補 regex。

### 第二階段：案件比對

引用文字前面的法院名稱寫法太發散（`最高法院`、`本院`、`鈞院`、`原審法院`），與其去救它，不如**繞過它**：開放資料的檔名本身就是 `法院,年度,字別,案號,日期,序號.json`，所以 regex 拿到的 `X年Y字第Z號` 可以直接拿去比對全語料的檔名。這一階段不抽樣，全語料跑。

分成三步：

| 階段 | 做法 | 產出 |
|---|---|---|
| 0. 建索引 | `rar lb` 只列檔名不解壓縮，把 `(年, 字別, 案號)` → 檔案 寫進 SQLite | `data/index/corpus.sqlite` |
| 1. 抽引用 | 逐包解壓到暫存區掃完即刪，只留 `(引用者, 年, 字別, 案號, span)` | `data/citations/*.csv.gz` |
| 2. 標候選 | 去重後的 key 查索引，標記命中 0 / 1 / ≥2 筆 | `data/candidates/` |
| 3. 消歧 | 對 ≥2 筆的引用跑三種相似度方法，比較彼此的一致性 | `data/resolution/` |

第 2 步的三種結果各有意義：`none` 是本專案的核心限制（1996 年前的判決不在開放資料中，或該字號其實是檢方案號）；`unique` 已經定案，不需要 NLP；`ambiguous` 才是第 3 步要處理的對象——同一組 `年/字別/案號` 在不同法院、不同審級可能各有一篇（例如 `85年台上字第326號` 民事、刑事各一）。

判決書首頁必然重複自己的案號，那不是引用，第 1 步會依檔名比對把它濾掉。

第 3 步的三種方法都以「字元」為特徵，因此不需要中文斷詞器，差別只在特徵攜帶多少上下文與如何加權：

| 方法 | 特徵 | 加權 |
|---|---|---|
| `bow` | 字元 unigram | 詞頻 |
| `ngram` | 字元 2–4 gram | 詞頻 |
| `tfidf` | 字元 2–4 gram | TF-IDF（sublinear TF） |

vectorizer 只用「引用處上下文 + 該次決策的候選們」來 fit，IDF 因此是**區域性**的：有鑑別力的特徵就是候選彼此之間有差異的那些。三個方法各自給出 top-1 與 margin，最後統計兩兩一致率與三者全體一致率。

## Evaluation

以 363 篇樣本（每個月份最大的一篇判決，1996-01 起）稽核：

| 指標 | 數值 |
|---|---|
| 抽出的引用 | 42,898 |
| 法院裁判 | 17,772 |
| 檢方案號 | 22,274 |
| 未知字別 | 2,198 |

## Limitations

- 司法院開放資料僅自 1996 年起，在此之前的被引用判決無法確認全文，只能標註為不確定
- 引用只給出 `年/字別/案號`，法院名稱刻意不解析，因此同字號跨法院、跨審級的情形必須靠內文相似度裁決，而相似度沒有 ground truth，只能以三個方法的一致率間接評估
- 判決附表會把年份與案號放在**不同欄位**（`│ 年度偵字第5727號`），這類引用需要重建表格結構才能救回
- `unknown` 字別中有舊式或地方自訂寫法（`綠`、`汐雜`、`藍保管`），尚需人工判斷歸類

## Repository Structure

```
src/tjcas/
  config.py        專案路徑與 .env 設定
  sampling.py      從壓縮檔列清單與抽取判決
  case_types.py    字別字典（PDF 解析 + 檔名統計）
  patterns.py      引用 regex：終極 pattern 與 partial pattern
  numbers.py       國字數字 → 阿拉伯數字（位值制與逐字制）
  corpus.py        全語料檔名索引（SQLite）
  citations.py     階段一：全語料引用抽取
  candidates.py    階段二：候選數量標記（none / unique / ambiguous）
  similarity.py    階段三：bow / ngram / tfidf 三種相似度評分
  resolution.py    階段三：上下文擷取、消歧與一致性報表
scripts/
  sample_cases.py        抽樣
  build_case_types.py    建字別字典
  audit_patterns.py      pattern 稽核與遺漏報告
  build_corpus_index.py  建全語料檔名索引
  extract_citations.py   階段一
  match_candidates.py    階段二
  resolve_ambiguous.py   階段三
data/
  samples/         抽出的判決 JSON（不進版控）
  reference/       實施要點 PDF（不進版控）
  manifest/        樣本清單、字別字典、稽核報告
  index/           全語料檔名索引（不進版控，可重建）
  citations/       階段一產出（不進版控）
  candidates/      階段二產出（不進版控）
  resolution/      階段三產出（不進版控）
```

## Requirements

- Python 3.12+
- `python-dotenv`、`pypdf`、`scikit-learn`
- `rar` / `unrar`（用於列清單與抽單檔；實測 `7z` 解這批 RAR 會產生空檔，不可用）

## Prerequisite Data

司法院判決書開放資料的月份 RAR 壓縮檔（`199601.rar` 起），每包約 160 MB、含 7 萬篇判決 JSON，檔名格式為 `法院代碼,年度,字別,案號,日期,序號.json`。將目錄路徑寫入 `.env` 的 `DATA_ROUTE`。

字別字典需要兩份司法院/法務部公開的 PDF，放在 `data/reference/`：
- 民刑事件編號計數分案報結實施要點附件.pdf
- 檢察案件編號計數分案報結實施要點.pdf

## Installation

```bash
virtualenv --python python3.12 .venv
.venv/bin/python -m pip install -e .
```

## Usage

```bash
# 抽樣：每個月份壓縮檔取最大的一篇，寫入 data/samples/ 與 manifest
.venv/bin/python scripts/sample_cases.py [--limit N]

# 字別字典：解析 PDF 並掃描所有壓縮檔的檔名
.venv/bin/python scripts/build_case_types.py [--skip-official] [--skip-observed]

# 稽核：輸出引用數、分類結果與遺漏清單
.venv/bin/python scripts/audit_patterns.py [--limit N]
```

第二階段（全語料，建議在 tmux 裡跑）：

```bash
# 0. 建全語料檔名索引
.venv/bin/python scripts/build_corpus_index.py [--workers N]

# 1. 抽出每篇判決的引用清單（逐包處理，可中斷續跑）
.venv/bin/python scripts/extract_citations.py [--workers N]

# 2. 標記每個引用 key 的候選數量
.venv/bin/python scripts/match_candidates.py

# 3. 對 ≥2 候選的引用做相似度消歧並比較三種方法
.venv/bin/python scripts/resolve_ambiguous.py [--limit-keys N] [--max-per-key N]
```

解壓縮的暫存區預設是系統 temp，可以在 `.env` 設 `WORK_ROUTE` 指到本機磁碟（跑在 NFS 上會慢很多）。

程式中使用：

```python
from tjcas.patterns import find_citations, normalize

for citation in find_citations(normalize(judgment_full_text)):
    print(citation.year, citation.code, citation.number)
```

## Changes of Regex Pattern

每一輪稽核發現的遺漏與對應修正：

| 輪次 | 發現的遺漏 | 修正 |
|---|---|---|
| 1 | `第六六五六、六六五七、七○○九號` 頓號連寫 | Z 支援頓號分隔，拆成多筆 citation |
| 2 | `89年促字第││    26070號` 附表框線切斷 | normalize 時把框線字元換成空白 |
| 2 | `九十年度南簡字第第九五○號` 重複的「第」 | 允許 `第{1,2}` |
| 2 | `第一二四六二、一二七九八等號` | 允許 `等?號` |

## Options of Full Text Similarity Comparison

目前提供三種輕量化方法（見 `src/tjcas/similarity.py` 的 `METHODS`）：`bow`、`ngram`、`tfidf`。新增方法只要在 `METHODS` 裡加一個符合 scikit-learn vectorizer 介面的 factory 即可，報表的一致率會自動把它納入兩兩比較。embedding 方法尚未實作。

## Development

分支策略：`main` ← `develop` ← `feat/*`。開發計畫放在 `develop-plan/`。

註：`data/samples/`、`data/reference/`、`data/manifest/pattern_gaps.csv` 與第二階段的所有產出（`data/index/`、`data/citations/`、`data/candidates/`、`data/resolution/`）不進版控（體積大且可重建）。

## Contributing

## Citation

## License