# TJCAS 人工標註樣本（2026-10-28）

TJCAS 沒有 ground truth，之前的數字都是對 JURIX regex 的 agreement。這三份樣本是第一次人工檢核，
由 `scripts/sample_for_annotation.py draw`（固定 seed，reservoir sampling，每份 100 筆）抽出，
`fetch_contexts.py`（hostD，讀 work DB 的 contexts）補上下文，`build` 產出下列檔案。

| 檔案 | 內容 | 要填的欄位 |
|---|---|---|
| `items_A.tsv` | **難案例消歧**：T∩J 裡候選法院 ≥3 的 occurrence（文字有寫法院名，JURIX 讀到了） | `gold_court`、`gold_note` |
| `items_B.tsv` | **盲消歧**：候選法院 ≥2、但 JURIX 沒抓到這條邊的 occurrence（文字多半沒寫法院名，只能從上下文推） | `gold_court`、`gold_note` |
| `items_C.tsv` | **抽取精確率**：階段一抓到、年字號在語料內、JURIX 沒有的引用（T\J，含 unique 與 ambiguous） | `is_citation`、`correct_key`、`gold_note` |
| `answers_A.tsv`、`answers_B.tsv` | 模型答案：TJCAS 三方法與多數決選的法院／檔案、JURIX 讀到的法院、全部候選檔名 | 不要看，標完再對 |
| `draw_*.jsonl`、`draw_stats.json` | 抽樣原始資料與 eligible 母體大小 | — |

## 怎麼標

- `context` 欄是正規化後的判決文字，被引用的字號用【】框起來，前後各 500 字。
- A、B 的 `gold_court`：從 `candidate_courts` 欄（法院代碼×檔案數）選一個四碼法院代碼（第四碼 M＝刑事、V＝民事、A＝行政）。
  上下文看不出來填 `unclear`；確定被引的判決不在候選內（例如引的是更早或別院的案件）填 `none`。
- C 的 `is_citation`：`Y` 這段【】真的在引用一件法院裁判或檢方案件；`N` 不是（例如金額、條號、表格欄位被誤抓）；
  `P` 是引用但年字號抓錯（抓到一半、年份接錯），這時在 `correct_key` 填正確的 `年,字,號`。
- `gold_note` 自由填，特別是「本院」「原審」這類要靠上下文推法院的情形，寫一下你怎麼推的。
- 不用一次標完；標幾筆就能先對 `answers_*.tsv` 算 accuracy。標完請把檔案放回同一目錄並 commit，
  不要改 `item` 欄和其他欄位的順序。

## 之後怎麼算

`item` 對 `answers_*.tsv` 的同名欄：A、B 算 TJCAS strict／loose 多數決與三個單一方法各自的 accuracy，
並和 JURIX 的法院比（A 才有）；C 算精確率 = Y／(Y+N+P)，P 另外列出讓階段一改 pattern。
