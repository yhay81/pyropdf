use hayro::hayro_interpret::{InterpreterSettings, color::CmykProfile};
use hayro::hayro_syntax::Pdf;
use hayro::{CmykPixmapSettings, RenderCache, RenderSettings, render_cmyk};
use std::{env, fs};

fn main() {
    let mut args = env::args_os().skip(1);
    let output = args.next().expect("output CMYKA path");
    let profile = args.next().expect("ICC profile path or none");
    let input = args.next().expect("input PDF");
    let options = CmykPixmapSettings {
        profile: (profile != "none")
            .then(|| CmykProfile::new(&fs::read(profile).unwrap()).unwrap()),
        ..Default::default()
    };
    let pdf = Pdf::new(fs::read(input).unwrap()).unwrap();
    let result = render_cmyk(
        &pdf.pages()[0],
        &RenderCache::new(),
        &InterpreterSettings::default(),
        &RenderSettings::default(),
        &options,
    );
    assert_eq!(
        result.data.len(),
        usize::from(result.width) * usize::from(result.height) * 5
    );
    fs::write(output, result.data).unwrap();
}
