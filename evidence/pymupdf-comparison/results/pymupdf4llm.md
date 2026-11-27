# Markdown and retrieval chunk comparison: pyropdf vs pymupdf4llm (4e0e3c00)

Measured on native-text labeled pages (`evidence/pymupdf-comparison/quality/*.json`), including
Japanese business forms (`mhlw-deposit-report-p1`, `mhlw-doc-p1`, `mhlw-doc-p2`), tax forms,
wide and rotated tables, two-column scans with OCR layers, and prose. `pymupdf4llm` runs strictly
in an isolated temporary venv outside pyropdf's dependencies.

## Summary across all scored pages

| Engine | MD anchors | Ordered pairs | Chunk anchors | MD table cells | Headings | Lists | Pipe rows | MD ms | Chunks ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| pyropdf 0.4.1 | 147/152 | 134/135 | 147/152 | 93/102 | 55 | 19 | 111 | 755.6 | 740.1 |
| pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 132/152 | 110/120 | 132/152 | 25/102 | 8 | 11 | 116 | 9044.0 | 9044.6 |
| pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 112/152 | 98/100 | 112/152 | 23/102 | 12 | 53 | 292 | 3925.0 | 3908.5 |

## Per-page results

| Page | Engine | MD anchors | Ordered pairs | Chunk anchors | Table cells | Headings | Pipe rows | Chunks | MD ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815-p1 | pyropdf 0.4.1 | 12/12 | 11/11 | 12/12 | — | 5 | 0 | 5 | 8.1 |
| bill-hr815-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 11/12 | 10/10 | 11/12 | — | 2 | 0 | 1 | 573.6 |
| bill-hr815-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 12/12 | 11/11 | 12/12 | — | 2 | 0 | 1 | 79.2 |
| f1040-p1 | pyropdf 0.4.1 | 18/18 | 17/17 | 18/18 | — | 8 | 8 | 9 | 235.1 |
| f1040-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 18/18 | 17/17 | 18/18 | — | 0 | 41 | 1 | 2053.1 |
| f1040-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 8/18 | 7/7 | 8/18 | — | 0 | 55 | 1 | 612.1 |
| f1040-p2 | pyropdf 0.4.1 | 18/18 | 17/17 | 18/18 | — | 7 | 0 | 8 | 243.9 |
| f1040-p2 | pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 17/18 | 16/16 | 17/18 | — | 0 | 39 | 1 | 1217.6 |
| f1040-p2 | pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 8/18 | 7/7 | 8/18 | — | 0 | 62 | 1 | 478.2 |
| mhlw-deposit-report-p1 | pyropdf 0.4.1 | 9/12 | 8/8 | 9/12 | 0/6 | 15 | 11 | 17 | 33.0 |
| mhlw-deposit-report-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 8/12 | 4/7 | 8/12 | 0/6 | 0 | 18 | 1 | 414.3 |
| mhlw-deposit-report-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 9/12 | 8/8 | 9/12 | 0/6 | 0 | 0 | 1 | 112.6 |
| mhlw-doc-p1 | pyropdf 0.4.1 | 11/11 | 9/10 | 11/11 | — | 0 | 0 | 1 | 61.5 |
| mhlw-doc-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 11/11 | 10/10 | 11/11 | — | 0 | 0 | 1 | 41.2 |
| mhlw-doc-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 11/11 | 10/10 | 11/11 | — | 0 | 0 | 1 | 64.5 |
| mhlw-doc-p2 | pyropdf 0.4.1 | 3/5 | 2/2 | 3/5 | — | 0 | 0 | 1 | 18.6 |
| mhlw-doc-p2 | pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 3/5 | 2/2 | 3/5 | — | 0 | 0 | 1 | 19.6 |
| mhlw-doc-p2 | pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 3/5 | 2/2 | 3/5 | — | 0 | 0 | 1 | 48.5 |
| nics-background-checks-2015-11-p1 | pyropdf 0.4.1 | 11/11 | 10/10 | 11/11 | 75/75 | 0 | 58 | 2 | 88.3 |
| nics-background-checks-2015-11-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 11/11 | 10/10 | 11/11 | 25/75 | 0 | 14 | 1 | 3028.7 |
| nics-background-checks-2015-11-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 11/11 | 10/10 | 11/11 | 5/75 | 2 | 57 | 1 | 1134.8 |
| patent-us223898-p2 | pyropdf 0.4.1 | 9/9 | 8/8 | 9/9 | — | 7 | 0 | 8 | 14.3 |
| patent-us223898-p2 | pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 9/9 | 7/8 | 9/9 | — | 2 | 0 | 1 | 356.7 |
| patent-us223898-p2 | pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 9/9 | 7/8 | 9/9 | — | 1 | 53 | 1 | 571.6 |
| patent-us223898-p3 | pyropdf 0.4.1 | 14/14 | 13/13 | 14/14 | — | 2 | 0 | 4 | 16.2 |
| patent-us223898-p3 | pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 14/14 | 11/13 | 14/14 | — | 0 | 0 | 1 | 319.1 |
| patent-us223898-p3 | pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 12/14 | 10/11 | 12/14 | — | 1 | 0 | 1 | 378.6 |
| senate-expenditures-p1 | pyropdf 0.4.1 | 16/16 | 15/15 | 16/16 | 18/21 | 0 | 34 | 2 | 13.0 |
| senate-expenditures-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 16/16 | 11/15 | 16/16 | 0/21 | 0 | 4 | 1 | 691.8 |
| senate-expenditures-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 14/16 | 13/13 | 14/16 | 18/21 | 0 | 37 | 1 | 201.6 |
| usrguide-p1 | pyropdf 0.4.1 | 14/14 | 13/13 | 14/14 | — | 6 | 0 | 6 | 11.8 |
| usrguide-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 8/14 | 7/7 | 8/14 | — | 2 | 0 | 1 | 167.6 |
| usrguide-p1 | pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 4/14 | 3/3 | 4/14 | — | 2 | 23 | 1 | 130.5 |
| usrguide-p2 | pyropdf 0.4.1 | 12/12 | 11/11 | 12/12 | — | 5 | 0 | 6 | 11.9 |
| usrguide-p2 | pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout) | 6/12 | 5/5 | 6/12 | — | 2 | 0 | 1 | 160.7 |
| usrguide-p2 | pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout) | 11/12 | 10/10 | 11/12 | — | 4 | 5 | 1 | 112.9 |

## Missing anchors and order inversions

- bill-hr815-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `(1) DIVISION A.—Israel Security Supplemental Appropria-`
- f1040-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `U.S. Individual Income Tax Return`
- f1040-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `For the year Jan. 1–Dec. 31, 2025, or other tax year beginning`
- f1040-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Your first name and middle initial`
- f1040-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `If joint return, spouse’s first name and middle initial`
- f1040-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Home address (number and street).`
- f1040-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `City, town, or post office.`
- f1040-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Foreign country name`
- f1040-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Household employee wages not reported on Form(s) W-2`
- f1040-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Additional income from Schedule 1, line 10`
- f1040-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Subtract line 10 from line 9. This is your adjusted gross income`
- f1040-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `Go to www.irs.gov/Form1040 for instructions and the latest information.`
- f1040-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Form 1040 (2025)`
- f1040-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Standard deduction or itemized deductions (from Schedule A)`
- f1040-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Subtract line 14 from line 11b. If zero or less, enter -0-. This is your taxable income`
- f1040-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Add lines 22 and 23. This is your total tax`
- f1040-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Add lines 25d, 26, and 32. These are your total payments`
- f1040-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `If line 33 is more than line 24, subtract line 24 from line 33. This is the amount you overpaid`
- f1040-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Subtract line 33 from line 24. This is the amount you owe.`
- f1040-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Do you want to allow another person to discuss this return with the IRS? See instructions.`
- f1040-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Under penalties of perjury, I declare that I have examined this return`
- f1040-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Go to www.irs.gov/Form1040 for instructions and the latest information.`
- mhlw-deposit-report-p1, pyropdf 0.4.1: missing anchor `貯蓄金管理協定に定められた預金の利率`
- mhlw-deposit-report-p1, pyropdf 0.4.1: missing anchor `当該年度当初の預金額(a)`
- mhlw-deposit-report-p1, pyropdf 0.4.1: missing anchor `当該年度末の預金額(a+b-c)`
- mhlw-deposit-report-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `預金の状況`
- mhlw-deposit-report-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `貯蓄金管理協定に定められた預金の利率`
- mhlw-deposit-report-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `当該年度当初の預金額(a)`
- mhlw-deposit-report-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `当該年度末の預金額(a+b-c)`
- mhlw-deposit-report-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): `預金管理状況報告` precedes `様式第24号(第57条関係)`
- mhlw-deposit-report-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): `事業の名称` precedes `受付印`
- mhlw-deposit-report-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): `労働保険番号` precedes `事業の名称`
- mhlw-deposit-report-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `貯蓄金管理協定に定められた預金の利率`
- mhlw-deposit-report-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `当該年度当初の預金額(a)`
- mhlw-deposit-report-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `当該年度末の預金額(a+b-c)`
- mhlw-doc-p1, pyropdf 0.4.1: `裁判例資料の作成方法` precedes `（第１回労働基準法における「労働者」に関する研究会資料３－１を更新）`
- mhlw-doc-p2, pyropdf 0.4.1: missing anchor `＜裁判例の評釈について＞
○　評釈の選定方法`
- mhlw-doc-p2, pyropdf 0.4.1: missing anchor `○　留意点
評釈がある裁判例については、資料２－２の１頁目の表の「評釈」の項目に「○」を付けている。`
- mhlw-doc-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `＜裁判例の評釈について＞
○　評釈の選定方法`
- mhlw-doc-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `○　留意点
評釈がある裁判例については、資料２－２の１頁目の表の「評釈」の項目に「○」を付けている。`
- mhlw-doc-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `＜裁判例の評釈について＞
○　評釈の選定方法`
- mhlw-doc-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `○　留意点
評釈がある裁判例については、資料２－２の１頁目の表の「評釈」の項目に「○」を付けている。`
- patent-us223898-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): `dimensions and good conductors, and a glas` precedes `he lar, the lealing-wires must be of largn`
- patent-us223898-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): `dimensions and good conductors, and a glas` precedes `he lar, the lealing-wires must be of largn`
- patent-us223898-p3, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): `vacuum has been reached, is heronetically` precedes `a nercury-pump.`
- patent-us223898-p3, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): `JoEN F. RANDOLPE.` precedes `TOMAS A. CDSON.`
- patent-us223898-p3, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `In the drawings, Figare 1 shows the lamp`
- patent-us223898-p3, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `I clain as my invention`
- patent-us223898-p3, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): `vacuum has been reached, is heronetically` precedes `a nercury-pump.`
- senate-expenditures-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): `DISTRICT DIRECTOR` precedes `BAIN, J MATTHEW`
- senate-expenditures-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): `37,499.96` precedes `DISTRICT DIRECTOR`
- senate-expenditures-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): `FIELD REPRESENTATIVE FROM SEP. 16` precedes `GRUENDER, BENJAMIN L`
- senate-expenditures-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): `1,458.33` precedes `FIELD REPRESENTATIVE FROM SEP. 16`
- senate-expenditures-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `OBLIGATION/SERVICE`
- senate-expenditures-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `AMOUNT ($)`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `Copyright 2020–2025, LATEX Project Team.`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `1 Introduction`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `2 Creating document commands and environments`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `3 Copying and showing (robust) commands and environments`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `4 Preconstructing command names`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `the source usrguide.tex for full details.`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `LATEX for authors`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Copyright 2020–2025, LATEX Project Team.`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `1 Introduction`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `2 Creating document commands and environments`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `2.5 Optional arguments`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `2.10 Argument processors`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `2.19 Creating new argument processors`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `3 Copying and showing (robust) commands and environments`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `4 Preconstructing command names`
- usrguide-p1, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `the source usrguide.tex for full details.`
- usrguide-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `Expandable floating point (and other) calculations`
- usrguide-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `Expandable \input equivalent`
- usrguide-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `Case changing`
- usrguide-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `Text sub- and superscripts`
- usrguide-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `Support for problem solving`
- usrguide-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, without pymupdf-layout): missing anchor `Describing argument types`
- usrguide-p2, pymupdf4llm 2.1 (PyMuPDF 2.1, with pymupdf-layout): missing anchor `Expandable \input equivalent`
