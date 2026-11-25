// Independent reader for the original shared-forms.pdf control and its saved revisions.
// Run with Java 17+ and Apache PDFBox 3.0.5, as described in README.md.
import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.apache.pdfbox.Loader;
import org.apache.pdfbox.contentstream.operator.Operator;
import org.apache.pdfbox.cos.*;
import org.apache.pdfbox.pdmodel.*;
import org.apache.pdfbox.pdmodel.documentinterchange.markedcontent.PDPropertyList;
import org.apache.pdfbox.pdmodel.font.PDFont;
import org.apache.pdfbox.pdmodel.graphics.form.PDFormXObject;
import org.apache.pdfbox.text.PDFTextStripper;
import org.apache.pdfbox.util.Matrix;
import org.apache.pdfbox.util.Vector;

class FormText extends PDFTextStripper {
    record Source(int page, long form, int mcid) {}
    record Mark(Source source, boolean artifact) {}

    final Map<Source, StringBuilder> text = new HashMap<>();
    final Map<Source, String> expanded = new HashMap<>();
    final Map<Source, String> replacement = new HashMap<>();
    final Deque<Mark> marks = new ArrayDeque<>();
    long form;
    int depth;
    int operators;
    int glyphs;

    @Override
    public void showForm(PDFormXObject child) throws IOException {
        if (++depth > 8) throw new IOException("Fixture form nesting exceeds eight levels");
        long parent = form;
        form = child.getCOSObject().getKey().getNumber();
        try {
            super.showForm(child);
        } finally {
            form = parent;
            depth--;
        }
    }

    @Override
    protected void processOperator(Operator op, List<COSBase> args) throws IOException {
        if (++operators > 2048) throw new IOException("Fixture exceeds 2048 operators");
        switch (op.getName()) {
            case "BDC", "BMC" -> {
                COSDictionary properties = null;
                if (args.size() == 2 && args.get(1) instanceof COSDictionary direct) {
                    properties = direct;
                } else if (args.size() == 2 && args.get(1) instanceof COSName name) {
                    PDPropertyList resolved = getResources().getProperties(name);
                    if (resolved != null) properties = resolved.getCOSObject();
                }
                Source source = null;
                if (properties != null && properties.containsKey(COSName.MCID)) {
                    source = new Source(getCurrentPageNo(), form, properties.getInt(COSName.MCID));
                    text.computeIfAbsent(source, key -> new StringBuilder());
                    if (properties.containsKey(COSName.E)) expanded.put(source, properties.getString(COSName.E));
                    if (properties.containsKey(COSName.ACTUAL_TEXT)) replacement.put(source, properties.getString(COSName.ACTUAL_TEXT));
                }
                marks.push(new Mark(source, COSName.ARTIFACT.equals(args.get(0))));
            }
            case "EMC" -> marks.pop();
        }
        super.processOperator(op, args);
    }

    @Override
    protected void showGlyph(Matrix matrix, PDFont font, int code, Vector advance) throws IOException {
        if (++glyphs > 4096) throw new IOException("Fixture exceeds 4096 glyphs");
        if (marks.stream().anyMatch(Mark::artifact)) return;
        for (Mark mark : marks) {
            if (mark.source() != null) text.get(mark.source()).append(font.toUnicode(code));
        }
    }

    void report(PDDocument doc) throws IOException {
        getText(doc);
        COSDictionary root = doc.getDocumentCatalog().getStructureTreeRoot().getCOSObject();
        COSDictionary document = root.getCOSDictionary(COSName.K);
        for (COSBase ref : document.getCOSArray(COSName.K)) {
            COSDictionary element = (COSDictionary)((COSObject)ref).getObject();
            int page = 1 + doc.getPages().indexOf(new PDPage(element.getCOSDictionary(COSName.PG)));
            COSBase kid = element.getDictionaryObject(COSName.K);
            Source source;
            if (kid instanceof COSInteger id) {
                source = new Source(page, 0, id.intValue());
            } else {
                COSDictionary mcr = (COSDictionary)kid;
                COSBase stream = mcr.getDictionaryObject("Stm");
                source = new Source(page, stream.getKey().getNumber(), mcr.getInt(COSName.MCID));
            }
            String actual = element.getString(COSName.ACTUAL_TEXT);
            String plain = actual != null ? actual : replacement.getOrDefault(source, text.get(source).toString());
            String expansion = actual != null ? actual : expanded.getOrDefault(source, plain);
            System.out.printf("%d\t%d\t%d\t%s\t%s%n", page, source.form(), source.mcid(), plain, expansion);
        }
    }

    public static void main(String[] args) throws IOException {
        File input = new File(args[0]);
        if (Files.size(input.toPath()) > 262144) throw new IOException("Fixture exceeds 256 KiB");
        try (PDDocument document = Loader.loadPDF(input)) {
            if (document.getNumberOfPages() != 2) throw new IOException("Expected two fixture pages");
            new FormText().report(document);
        }
    }
}
