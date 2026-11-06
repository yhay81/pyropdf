timeout --signal=KILL 10s prlimit --as=1073741824 -- python - <<'PY'
import pyropdf

with pyropdf.open("input.pdf", limits=pyropdf.DocumentLimits.web()) as doc:
    print(len(doc.pages[0].words()))
PY
