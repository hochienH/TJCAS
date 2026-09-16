# TJCAS: Taiwanese Judgments Citation Annotating System

> [!NOTE] 
> TLDR: 這是一個針對臺灣司法院公開判決的自動標註系統
> - input(str): 判決檔案名稱
> - output(list): 在這篇判決當中有提及的所有判決檔案名稱（理論）
> - 特色：有別於過去純粹使用 regex 的保守做法，這個專案所採用的做法較為「激進」，使用 regex 拿到 X年Y字第Z號 這樣的資料後，直接透過 NLP 技術比對所有開放資料中最相近的判決，作為 annotation 的結果
> - 缺點：司法院公開資料僅從 1996 年開始，在此之前無法確定全文就只能標註不確定

## Project Status
In Progress

## Quick Start


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

## Evaluation

## Limitations

## Repository Structure

## Requirements

## Prerequisite Data

## Installation

## Usage

## Changes of Regex Pattern

## Options of Full Text Similarity Comparison

## Development

## Contributing

## Citation

## License