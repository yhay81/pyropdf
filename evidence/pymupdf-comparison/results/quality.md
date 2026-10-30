Labels: `quality/*.json`; pyropdf commit 3115d2c with #460.

| Page | Engine | Anchors found | Anchor pairs in order | Words placed | Mean word IoU | Table rows | Cells right | Columns |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815-p1 | PyMuPDF 2.0 | 12/12 | 11/11 | 6/6 | 0.81 | | | |
| bill-hr815-p1 | PyMuPDF 2.0 sort=True | 12/12 | 11/11 | 6/6 | 0.81 | | | |
| bill-hr815-p1 | pyropdf 0.2.0 | 12/12 | 11/11 | 6/6 | 0.76 | | | |
| f1040-p1 | PyMuPDF 2.0 | 18/18 | 17/17 | 6/6 | 0.72 | | | |
| f1040-p1 | PyMuPDF 2.0 sort=True | 18/18 | 17/17 | 6/6 | 0.72 | | | |
| f1040-p1 | pyropdf 0.2.0 | 18/18 | 17/17 | 6/6 | 0.84 | | | |
| mhlw-doc-p1 | PyMuPDF 2.0 | 11/11 | 9/10 | 6/6 | 0.90 | | | |
| mhlw-doc-p1 | PyMuPDF 2.0 sort=True | 11/11 | 10/10 | 6/6 | 0.90 | | | |
| mhlw-doc-p1 | pyropdf 0.2.0 | 11/11 | 10/10 | 6/6 | 0.82 | | | |
| nics-background-checks-2015-11-p1 | PyMuPDF 2.0 | 11/11 | 9/10 | 6/6 | 0.73 | 1/3 | 14/75 | 25/25 |
| nics-background-checks-2015-11-p1 | PyMuPDF 2.0 sort=True | 11/11 | 10/10 | 6/6 | 0.73 | 1/3 | 14/75 | 25/25 |
| nics-background-checks-2015-11-p1 | pyropdf 0.2.0 | 11/11 | 10/10 | 6/6 | 0.73 | 3/3 | 75/75 | 25/25 |
| patent-us223898-p2 | PyMuPDF 2.0 | 9/9 | 7/8 | 0/0 |  | | | |
| patent-us223898-p2 | PyMuPDF 2.0 sort=True | 9/9 | 7/8 | 0/0 |  | | | |
| patent-us223898-p2 | pyropdf 0.2.0 | 9/9 | 8/8 | 0/0 |  | | | |
| usrguide-p2 | PyMuPDF 2.0 | 12/12 | 11/11 | 6/6 | 0.72 | | | |
| usrguide-p2 | PyMuPDF 2.0 sort=True | 12/12 | 11/11 | 6/6 | 0.72 | | | |
| usrguide-p2 | pyropdf 0.2.0 | 12/12 | 11/11 | 6/6 | 0.71 | | | |

## Failures

- mhlw-doc-p1, PyMuPDF 2.0: `裁判例資料の作成方法` comes before `（第１回労働基準法における「労働者」に関する研究会資料３－１を更新）`
- nics-background-checks-2015-11-p1, PyMuPDF 2.0: `Alabama` comes before `November - 2015`
- patent-us223898-p2, PyMuPDF 2.0: `dimensions and good conductors, and a glas` comes before `he lar, the lealing-wires must be of largn`
- patent-us223898-p2, PyMuPDF 2.0 sort=True: `dimensions and good conductors, and a glas` comes before `he lar, the lealing-wires must be of largn`
