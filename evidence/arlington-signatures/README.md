# PDF 2.0 signatures and the Arlington extension switch

ISO 32000-2:2020 defines the document security store and its signature-specific VRI in Tables 261 and 262. Table 260 lists the standard signature subfilter names. A base PDF 2.0 file can use these entries without a catalog extension declaration. A VRI's optional OCSP array, if supplied, must contain indirect references to DER response streams. The example uses an OCSP responder's `unauthorized` response (`30030a0106`). This exercises the storage structure and supplies no affirmative certificate status. [PDF 2.0 corrections, §12.8](https://pdf-issues.pdfa.org/32000-2-2020/clause12.html) · [ETSI EN 319 142-1, §5.4.2.3](https://www.etsi.org/deliver/etsi_en/319100_319199/31914201/01.03.01_60/en_31914201v010301p.pdf)

## Observations

Both veraPDF Arlington 1.28.2 (CI pin) and 1.30.4 (released installer) produced the following results for **both full and incremental saves**. Each file was tested unchanged in each model. The catalog has no `/Extensions`. The VRI key comes from the actual signature contents. Its one OCSP reference and the document-level DSS reference resolve to the same stream with the five bytes above.

| Signed PDF 2.0 input | Base Arlington 2.0 | With ETSI_PAdES enabled |
| --- | --- | --- |
| Detached CAdES, no DSS | SubFilter deviation | Pass |
| Same signature, one VRI OCSP reference | SubFilter and OCSP deviations | Pass |
| Same signature, empty VRI OCSP array | SubFilter and OCSP deviations | OCSP deviation |

The exact IDs are `Signature-SubFilter-Name-ETSI.CAdES.detached-27` and `VRI-OCSP-Array-9`. The empty-array control still fails with the extension. The script checks CMS integrity and trust separately using a locally generated CA. Incremental output preserves the original signature's covered bytes. A full rewrite of that revision invalidates the signature in all three cases.

## Model and consumer

The repository's pinned VRI row permits the key in PDF 2.0, then puts the nonempty-array check under an extension predicate. The base validator consequently flags the present, nonempty reference as well as the empty control. The latest upstream row inspected has the same guard. The model correction in [#926](https://github.com/yhay81/pyropdf/issues/926) should apply the length condition whenever the key is applicable. [Pinned VRI row](https://github.com/pdf-association/arlington-pdf-model/blob/d148fee5a0cb6dcacadf58c09ed842e32847024d/tsv/latest/VRI.tsv) · [Compared VRI row](https://github.com/pdf-association/arlington-pdf-model/blob/cdc485e4238c55bbd6204f6fda31997245d54b71/tsv/latest/VRI.tsv)

The signature row already lists `ETSI.CAdES.detached` twice, for the earlier extension and for base PDF 2.0. The veraPDF generator emits an extension-only rule for the first alternative even where the version alternative admits the same value. The consumer correction in [#927](https://github.com/yhay81/pyropdf/issues/927) belongs in possible-value rule generation. The PDF 1.7 extension-only case should remain conditional. [Signature row](https://github.com/pdf-association/arlington-pdf-model/blob/d148fee5a0cb6dcacadf58c09ed842e32847024d/tsv/latest/Signature.tsv) · [Generator, possible values and extensionValue](https://github.com/veraPDF/veraPDF-arlington-tools/blob/f12c5fa6669f7843af542899f7c5a545e5fc968f/arlington-generator/src/main/java/org/verapdf/arlington/Rules.java#L517)

Keep the base verdicts and the separate extension-enabled comparisons for the affected operations. Adding an extension declaration to the user's PDF would change the file to accommodate an oracle discrepancy. A corrected validator must accept both positive controls under the base model and still reject the empty array. Verify those outcomes before retiring the exact expectations in #845.

## Reproduce

Run from the repository root with the locked development dependencies. The script needs Java and CPython with the signing test dependencies. It generates six PDFs in a temporary directory and checks 24 verdicts using the two jars. No external signing service is involved.

```sh
uv run python evidence/arlington-signatures/compare.py /path/to/greenfield-apps-arlington-1.28.2.jar /path/to/cli-arlington-1.30.4.jar
```

The pinned jar SHA-256 is `a780142be33c9ce00a0fd075c6d7b706074a01e87aa5c7d7328db4ff345a4464`. The 1.30.4 installer archive SHA-256 is `d3e8110259e9baa273cc9e8a507cf4c457a474c1d9a0d1c007dca69f0b00f7e1` and its installed CLI jar is `0715f5e45adc0f06cdff59fff204a18d4e22159deb53c081a6fb14554d29abbf`. [Maven 1.28.2](https://repo.maven.apache.org/maven2/org/verapdf/apps/greenfield-apps-arlington/1.28.2/) · [Official Arlington 1.30 installers](https://software.verapdf.org/releases/arlington/1.30/)
