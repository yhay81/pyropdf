Labels: `quality/*.json`; pyropdf commit 20f2a0e1 with #1153.

| Page | Engine | Anchors found | Anchor pairs in order | Words placed | Mean word IoU | Table rows | Cells right | Columns |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815-p1 | PyMuPDF 2.1 | 12/12 | 11/11 | 6/6 | 0.77 | | | |
| bill-hr815-p1 | PyMuPDF 2.1 sort=True | 12/12 | 11/11 | 6/6 | 0.77 | | | |
| bill-hr815-p1 | pyropdf 0.4.0 | 12/12 | 11/11 | 6/6 | 0.76 | | | |
| f1040-p1 | PyMuPDF 2.1 | 18/18 | 17/17 | 6/6 | 0.72 | | | |
| f1040-p1 | PyMuPDF 2.1 sort=True | 18/18 | 17/17 | 6/6 | 0.72 | | | |
| f1040-p1 | pyropdf 0.4.0 | 18/18 | 17/17 | 6/6 | 0.84 | | | |
| f1040-p2 | PyMuPDF 2.1 | 18/18 | 17/17 | 6/6 | 0.65 | | | |
| f1040-p2 | PyMuPDF 2.1 sort=True | 18/18 | 17/17 | 6/6 | 0.65 | | | |
| f1040-p2 | pyropdf 0.4.0 | 18/18 | 17/17 | 6/6 | 0.77 | | | |
| mhlw-doc-p1 | PyMuPDF 2.1 | 11/11 | 9/10 | 6/6 | 0.90 | | | |
| mhlw-doc-p1 | PyMuPDF 2.1 sort=True | 11/11 | 10/10 | 6/6 | 0.90 | | | |
| mhlw-doc-p1 | pyropdf 0.4.0 | 11/11 | 10/10 | 6/6 | 0.82 | | | |
| mhlw-doc-p2 | PyMuPDF 2.1 | 5/5 | 4/4 | 6/6 | 0.88 | | | |
| mhlw-doc-p2 | PyMuPDF 2.1 sort=True | 5/5 | 4/4 | 6/6 | 0.88 | | | |
| mhlw-doc-p2 | pyropdf 0.4.0 | 5/5 | 4/4 | 6/6 | 0.80 | | | |
| nics-background-checks-2015-11-p1 | PyMuPDF 2.1 | 11/11 | 9/10 | 6/6 | 0.73 | 1/3 | 14/75 | 25/25 |
| nics-background-checks-2015-11-p1 | PyMuPDF 2.1 sort=True | 11/11 | 10/10 | 6/6 | 0.73 | 1/3 | 14/75 | 25/25 |
| nics-background-checks-2015-11-p1 | pyropdf 0.4.0 | 11/11 | 10/10 | 6/6 | 0.73 | 3/3 | 75/75 | 25/25 |
| patent-us223898-p2 | PyMuPDF 2.1 | 9/9 | 7/8 | 0/0 |  | | | |
| patent-us223898-p2 | PyMuPDF 2.1 sort=True | 9/9 | 7/8 | 0/0 |  | | | |
| patent-us223898-p2 | pyropdf 0.4.0 | 9/9 | 8/8 | 0/0 |  | | | |
| patent-us223898-p3 | PyMuPDF 2.1 | 14/14 | 12/13 | 0/0 |  | | | |
| patent-us223898-p3 | PyMuPDF 2.1 sort=True | 14/14 | 12/13 | 0/0 |  | | | |
| patent-us223898-p3 | pyropdf 0.4.0 | 14/14 | 13/13 | 0/0 |  | | | |
| senate-expenditures-p1 | PyMuPDF 2.1 | 16/16 | 11/15 | 0/6 | 0.00 | 0/3 | 0/21 | 7/7 |
| senate-expenditures-p1 | PyMuPDF 2.1 sort=True | 6/16 | 4/5 | 0/6 | 0.00 | 0/3 | 0/21 | 7/7 |
| senate-expenditures-p1 | pyropdf 0.4.0 | 16/16 | 15/15 | 6/6 | 0.70 | 3/3 | 21/21 | 7/7 |
| usrguide-p1 | PyMuPDF 2.1 | 14/14 | 13/13 | 6/6 | 0.75 | | | |
| usrguide-p1 | PyMuPDF 2.1 sort=True | 14/14 | 13/13 | 6/6 | 0.75 | | | |
| usrguide-p1 | pyropdf 0.4.0 | 14/14 | 13/13 | 6/6 | 0.75 | | | |
| usrguide-p2 | PyMuPDF 2.1 | 12/12 | 11/11 | 6/6 | 0.71 | | | |
| usrguide-p2 | PyMuPDF 2.1 sort=True | 12/12 | 11/11 | 6/6 | 0.71 | | | |
| usrguide-p2 | pyropdf 0.4.0 | 12/12 | 11/11 | 6/6 | 0.71 | | | |

## Failures

- mhlw-doc-p1, PyMuPDF 2.1: `裁判例資料の作成方法` comes before `（第１回労働基準法における「労働者」に関する研究会資料３－１を更新）`
- nics-background-checks-2015-11-p1, PyMuPDF 2.1: `Alabama` comes before `November - 2015`
- patent-us223898-p2, PyMuPDF 2.1: `dimensions and good conductors, and a glas` comes before `he lar, the lealing-wires must be of largn`
- patent-us223898-p2, PyMuPDF 2.1 sort=True: `dimensions and good conductors, and a glas` comes before `he lar, the lealing-wires must be of largn`
- patent-us223898-p3, PyMuPDF 2.1: `material be molded around it in the act of car` comes before `EST ^VAILABLE COPY`
- patent-us223898-p3, PyMuPDF 2.1 sort=True: `vacuum has been reached, is heronetically` comes before `a nercury-pump.`
- senate-expenditures-p1, PyMuPDF 2.1: `DISTRICT DIRECTOR` comes before `BAIN, J MATTHEW`
- senate-expenditures-p1, PyMuPDF 2.1: `37,499.96` comes before `DISTRICT DIRECTOR`
- senate-expenditures-p1, PyMuPDF 2.1: `FIELD REPRESENTATIVE FROM SEP. 16` comes before `GRUENDER, BENJAMIN L`
- senate-expenditures-p1, PyMuPDF 2.1: `1,458.33` comes before `FIELD REPRESENTATIVE FROM SEP. 16`
- senate-expenditures-p1, PyMuPDF 2.1: no word `OBLIGATION/SERVICE` covers its ink
- senate-expenditures-p1, PyMuPDF 2.1: no word `37,499.96` covers its ink
- senate-expenditures-p1, PyMuPDF 2.1: no word `CLINTON` covers its ink
- senate-expenditures-p1, PyMuPDF 2.1: no word `DHAW20190005` covers its ink
- senate-expenditures-p1, PyMuPDF 2.1: no word `HARTMAN` covers its ink
- senate-expenditures-p1, PyMuPDF 2.1: no word `463.30` covers its ink
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: anchor not found: `DOCUMENT NO.`
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: anchor not found: `PAYEE NAME`
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: anchor not found: `AMOUNT ($)`
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: anchor not found: `BAIN, J MATTHEW`
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: anchor not found: `DISTRICT DIRECTOR`
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: anchor not found: `GRUENDER, BENJAMIN L`
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: anchor not found: `FIELD REPRESENTATIVE FROM SEP. 16`
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: anchor not found: `AIRFARE FOR E JOHNSON KANSAS CITY TO WASHINGTON DC AND RETURN`
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: anchor not found: `TRAIN FARE FOR SEN HAWLEY WASHINGTON DC TO NEW YORK NY AND RETURN`
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: anchor not found: `AIRFARE FOR C WEIHS WASHINGTON DC TO SPRINGFIELD AND RETURN`
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: `37,499.96` comes before `OBLIGATION/SERVICE`
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: no word `OBLIGATION/SERVICE` covers its ink
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: no word `37,499.96` covers its ink
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: no word `CLINTON` covers its ink
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: no word `DHAW20190005` covers its ink
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: no word `HARTMAN` covers its ink
- senate-expenditures-p1, PyMuPDF 2.1 sort=True: no word `463.30` covers its ink
