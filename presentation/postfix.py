"""pptxgenjs ignores log scale on a scatter X axis; add <c:logBase> to it."""
import sys, zipfile, shutil, os
src = sys.argv[1]; tmp = src + ".tmp"
old = '<c:scaling><c:orientation val="minMax"/><c:max val="100"/><c:min val="0.1"/></c:scaling>'
new = '<c:scaling><c:logBase val="10"/><c:orientation val="minMax"/><c:max val="100"/><c:min val="0.1"/></c:scaling>'
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename.startswith("ppt/charts/chart") and b"scatterChart" in data:
            text = data.decode("utf-8"); assert text.count(old) == 1, "x-axis scaling not found"; data = text.replace(old, new).replace('<c:crosses val="autoZero"/>', '<c:crosses val="min"/>').encode("utf-8")
        zout.writestr(item, data)
shutil.move(tmp, src); print("log x-axis applied")
