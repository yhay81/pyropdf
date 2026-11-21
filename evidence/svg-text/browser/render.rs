use hayro_interpret::InterpreterSettings;
use hayro_svg::{RenderCache, SvgRenderSettings};
use hayro_syntax::Pdf;
use std::{
    env, fs,
    path::{Path, PathBuf},
    sync::Arc,
};

fn main() {
    let output = PathBuf::from(env::args().nth(1).unwrap());
    for name in env::args().skip(2) {
        let input = Path::new(&name);
        let stem = input.file_stem().unwrap().to_str().unwrap();
        let pdf = Pdf::new(Arc::new(fs::read(input).unwrap())).unwrap();
        for (page_index, page) in pdf.pages().iter().enumerate().take(1) {
            for (mode, text_layer) in [("paths", false), ("layer", true)] {
                let svg = hayro_svg::convert(
                    page,
                    &RenderCache::new(),
                    &InterpreterSettings::default(),
                    &SvgRenderSettings {
                        text_layer,
                        ..Default::default()
                    },
                );
                fs::write(output.join(format!("{stem}-{page_index}-{mode}.svg")), svg).unwrap();
            }
        }
    }
}
