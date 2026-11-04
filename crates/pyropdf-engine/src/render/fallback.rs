//! The faces that draw text in the CJK fonts that a document does not embed
//! (yhay81/pyropdf#600), picked as pylopdf picks them
//! (rust/src/document/fonts.rs at 4ff4a266): the language from the font's
//! character collection, or from its name for Adobe-Identity, and the serif
//! face for a font whose name marks it as serif, such as a Mincho, or the
//! sans face for any other.
//!
//! hayro asks its font resolver for a font that a document does not embed,
//! and its default resolver answers with a Standard 14 font, which has no
//! CJK glyphs. These faces answer first.

use std::fs::File;
use std::io::Read;
use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

use hayro::hayro_interpret::InterpreterSettings;
use hayro::hayro_interpret::font::{FallbackFontQuery, FontData, FontQuery};
use hayro::hayro_interpret::hayro_cmap::CidFamily;

use crate::fonts::embed::Face;

/// The languages of the four Adobe CJK character collections.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Language {
    /// Adobe-Japan1, tagged `ja`.
    Japanese,
    /// Adobe-Korea1, tagged `ko`.
    Korean,
    /// Adobe-GB1, tagged `zh-CN`.
    SimplifiedChinese,
    /// Adobe-CNS1, tagged `zh-TW`.
    TraditionalChinese,
}

/// Lowercase parts of the names of Japanese fonts.
const JAPANESE: [&str; 12] = [
    "mincho", "gothic", "ryumin", "kozmin", "kozgo", "kozuka", "meiryo", "yugoth", "yumin",
    "hiragino", "ipaex", "ipam",
];

/// Lowercase parts of the names of Korean fonts.
const KOREAN: [&str; 7] = [
    "batang",
    "gulim",
    "dotum",
    "malgun",
    "nanum",
    "hygothic",
    "hysmyeongjo",
];

/// Lowercase parts of the names of Simplified Chinese fonts.
const SIMPLIFIED_CHINESE: [&str; 8] = [
    "simsun", "simhei", "fangsong", "kaiti", "yahei", "songti", "heiti", "cjk-sc",
];

/// Lowercase parts of the names of Traditional Chinese fonts.
const TRADITIONAL_CHINESE: [&str; 6] = [
    "mingliu", "pmingliu", "jhenghei", "dfkai", "cjk-tc", "cjk-hk",
];

/// Lowercase parts of the names of serif fonts.
const SERIF: [&str; 12] = [
    "mincho", "ryumin", "kozmin", "yumin", "serif", "batang", "myeongjo", "simsun", "song", "ming",
    "kaiti", "kai",
];

impl Language {
    /// The language of a BCP 47 tag: `ja`, `ko`, `zh-CN`, or `zh-TW`.
    #[must_use]
    pub fn from_tag(tag: &str) -> Option<Self> {
        match tag {
            "ja" => Some(Self::Japanese),
            "ko" => Some(Self::Korean),
            "zh-CN" => Some(Self::SimplifiedChinese),
            "zh-TW" => Some(Self::TraditionalChinese),
            _ => None,
        }
    }

    /// The language that a lowercase font name names, trying Japanese,
    /// Korean, Simplified, and Traditional Chinese in turn.
    fn of_name(name: &str) -> Option<Self> {
        [
            (Self::Japanese, &JAPANESE[..]),
            (Self::Korean, &KOREAN[..]),
            (Self::SimplifiedChinese, &SIMPLIFIED_CHINESE[..]),
            (Self::TraditionalChinese, &TRADITIONAL_CHINESE[..]),
        ]
        .into_iter()
        .find(|(_, parts)| parts.iter().any(|part| name.contains(part)))
        .map(|(language, _)| language)
    }
}

/// A font file that is read once, the first time that a page needs it, as
/// the faces of the installed font packages are: loading every installed
/// face up front would read tens of megabytes for documents that need none.
pub struct FontFile {
    path: PathBuf,
    max_size: Option<u64>,
    data: OnceLock<Option<FontData>>,
}

impl FontFile {
    /// The file at `path`, which is read only when it holds at most
    /// `max_size` bytes.
    #[must_use]
    pub const fn new(path: PathBuf, max_size: Option<u64>) -> Self {
        Self {
            path,
            max_size,
            data: OnceLock::new(),
        }
    }

    /// The bytes of the file, or `None` when it cannot be read or is
    /// larger than its limit, in which case its text draws as it would
    /// without it. Threads that need it at once read it once.
    fn data(&self) -> Option<FontData> {
        self.data
            .get_or_init(|| {
                let limit = self.max_size.unwrap_or(u64::MAX);
                let mut data = Vec::new();
                File::open(&self.path)
                    .ok()?
                    .take(limit.saturating_add(1))
                    .read_to_end(&mut data)
                    .ok()?;
                (data.len() as u64 <= limit).then(|| Arc::new(data) as FontData)
            })
            .clone()
    }
}

/// A loaded face's program, which hayro reads without a copy.
struct Program(Arc<Face>);

impl AsRef<[u8]> for Program {
    fn as_ref(&self) -> &[u8] {
        &self.0.program
    }
}

/// Where a fallback face comes from.
#[derive(Clone)]
pub enum Source {
    /// A face that a program loaded, such as `Font.from_file`'s.
    Face(Arc<Face>),
    /// A font file, read the first time that a page needs it.
    File(Arc<FontFile>),
}

impl Source {
    fn data(&self) -> Option<FontData> {
        match self {
            Self::Face(face) => Some(Arc::new(Program(Arc::clone(face)))),
            Self::File(file) => file.data(),
        }
    }

    /// Whether both are the same face or the same file.
    fn same(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Face(a), Self::Face(b)) => Arc::ptr_eq(a, b),
            (Self::File(a), Self::File(b)) => Arc::ptr_eq(a, b),
            _ => false,
        }
    }
}

/// The faces that draw the CJK fonts that a document does not embed: a
/// sans and a serif face for each language, any of them absent. When one
/// of a language's two is absent, the other draws both kinds of font.
#[derive(Clone, Default)]
pub struct Fallback {
    /// By language, in the order of [`Language`], then sans and serif.
    faces: [[Option<Source>; 2]; 4],
}

impl Fallback {
    /// Sets the sans or the serif face of `language`, or removes it.
    pub fn set(&mut self, language: Language, serif: bool, source: Option<Source>) {
        self.faces[language as usize][usize::from(serif)] = source;
    }

    /// Whether no language has a face.
    fn is_empty(&self) -> bool {
        self.faces.iter().flatten().all(Option::is_none)
    }

    /// Whether `other` holds the same faces, so that the fonts that hayro
    /// resolved through one draw as the other would draw them.
    pub(crate) fn same(&self, other: &Self) -> bool {
        self.faces
            .iter()
            .flatten()
            .zip(other.faces.iter().flatten())
            .all(|pair| match pair {
                (None, None) => true,
                (Some(a), Some(b)) => a.same(b),
                _ => false,
            })
    }

    /// The face that draws a font that a document does not embed, as
    /// `query` describes it; `None` when the font is not CJK or its
    /// language has no face.
    fn choose(&self, query: &FallbackFontQuery) -> Option<&Source> {
        let name = query
            .post_script_name
            .as_deref()
            .unwrap_or_default()
            .to_ascii_lowercase();
        let language = match query.character_collection.as_ref().map(|cc| &cc.family) {
            Some(CidFamily::AdobeJapan1) => Language::Japanese,
            Some(CidFamily::AdobeKorea1) => Language::Korean,
            Some(CidFamily::AdobeGB1) => Language::SimplifiedChinese,
            Some(CidFamily::AdobeCNS1) => Language::TraditionalChinese,
            // Adobe-Identity names no characters, so the name tells.
            _ => Language::of_name(&name)?,
        };
        let serif = SERIF.iter().any(|part| name.contains(part));
        let [sans_face, serif_face] = &self.faces[language as usize];
        let (preferred, other) = if serif {
            (serif_face, sans_face)
        } else {
            (sans_face, serif_face)
        };
        preferred.as_ref().or(other.as_ref())
    }

    /// hayro's default settings for interpreting a page, with a font
    /// resolver that answers a query for a CJK font that the document
    /// does not embed with these faces, and every other query as hayro's
    /// default resolver does.
    #[must_use]
    pub(crate) fn settings(&self) -> InterpreterSettings {
        let mut settings = InterpreterSettings::default();
        if self.is_empty() {
            return settings;
        }
        let faces = self.clone();
        let default = Arc::clone(&settings.font_resolver);
        settings.font_resolver = Arc::new(move |query| {
            // A face is the whole of its data, so its index is 0.
            if let FontQuery::Fallback(fallback) = query
                && let Some(data) = faces.choose(fallback).and_then(Source::data)
            {
                return Some((data, 0));
            }
            default(query)
        });
        settings
    }
}

#[cfg(test)]
mod tests {
    use std::fs;

    use hayro::hayro_interpret::hayro_cmap::CharacterCollection;

    use super::*;

    const SUBSET: &[u8] = include_bytes!("../fonts/testdata/NotoSansJP-subset.otf");

    /// A query for a font that a document does not embed, named `name`,
    /// of the character collection `family`.
    fn query(name: &str, family: Option<CidFamily>) -> FallbackFontQuery {
        FallbackFontQuery {
            post_script_name: Some(name.to_owned()),
            character_collection: family.map(|family| CharacterCollection {
                family,
                supplement: 2,
            }),
            ..FallbackFontQuery::default()
        }
    }

    fn face() -> Source {
        Source::Face(Arc::new(Face::read(SUBSET, 0).unwrap()))
    }

    /// A sans and a serif face for every language.
    fn every_face() -> (Fallback, Vec<(Language, Source, Source)>) {
        let mut fallback = Fallback::default();
        let mut faces = Vec::new();
        for language in [
            Language::Japanese,
            Language::Korean,
            Language::SimplifiedChinese,
            Language::TraditionalChinese,
        ] {
            let (sans, serif) = (face(), face());
            fallback.set(language, false, Some(sans.clone()));
            fallback.set(language, true, Some(serif.clone()));
            faces.push((language, sans, serif));
        }
        (fallback, faces)
    }

    /// Whether `fallback` draws a font named `name` of `family` with `face`.
    fn draws(fallback: &Fallback, name: &str, family: Option<CidFamily>, face: &Source) -> bool {
        fallback
            .choose(&query(name, family))
            .is_some_and(|chosen| chosen.same(face))
    }

    /// Whether `fallback` leaves a font named `name` of `family` to hayro.
    fn draws_none(fallback: &Fallback, name: &str, family: Option<CidFamily>) -> bool {
        fallback.choose(&query(name, family)).is_none()
    }

    /// The bytes of a font file, as rendering reads them.
    fn read(file: &FontFile) -> Option<Vec<u8>> {
        file.data().map(|data| (*data).as_ref().to_vec())
    }

    #[test]
    fn the_character_collection_picks_the_language() {
        let (fallback, faces) = every_face();
        let families = [
            CidFamily::AdobeJapan1,
            CidFamily::AdobeKorea1,
            CidFamily::AdobeGB1,
            CidFamily::AdobeCNS1,
        ];
        for ((_, sans, serif), family) in faces.iter().zip(families) {
            // The collection decides even against a name of another language.
            assert!(draws(&fallback, "SimHei", Some(family.clone()), sans));
            assert!(draws(&fallback, "MS-Mincho", Some(family), serif));
        }
    }

    #[test]
    fn the_name_picks_the_language_of_an_identity_font() {
        let (fallback, faces) = every_face();
        let face = |language: Language, serif: bool| {
            let (_, sans, serif_face) = faces.iter().find(|(l, ..)| *l == language).unwrap();
            if serif { serif_face } else { sans }
        };
        for (name, language, serif) in [
            ("MS-Gothic", Language::Japanese, false),
            ("ABCDEF+MS-Mincho", Language::Japanese, true),
            ("KozMinPr6N-Regular", Language::Japanese, true),
            // Japanese names are tried first, as pylopdf tries them.
            ("Malgun Gothic", Language::Japanese, false),
            ("Batang", Language::Korean, true),
            ("Gulim", Language::Korean, false),
            ("SimHei", Language::SimplifiedChinese, false),
            ("SimSun", Language::SimplifiedChinese, true),
            ("MingLiU", Language::TraditionalChinese, true),
            ("Microsoft JhengHei", Language::TraditionalChinese, false),
        ] {
            for family in [None, Some(CidFamily::AdobeIdentity)] {
                assert!(
                    draws(&fallback, name, family, face(language, serif)),
                    "{name}"
                );
            }
        }
        // A name of no CJK font, or none, picks no face.
        assert!(draws_none(&fallback, "Helvetica", None));
        assert!(draws_none(
            &fallback,
            "ArialMT",
            Some(CidFamily::AdobeIdentity)
        ));
        assert!(fallback.choose(&FallbackFontQuery::default()).is_none());
    }

    #[test]
    fn one_face_draws_both_kinds_and_other_languages_draw_none() {
        let (sans, serif) = (face(), face());
        let japan = || Some(CidFamily::AdobeJapan1);
        let mut fallback = Fallback::default();
        fallback.set(Language::Japanese, false, Some(sans.clone()));
        assert!(draws(&fallback, "MS-Mincho", japan(), &sans));
        assert!(draws(&fallback, "MS-Gothic", japan(), &sans));
        fallback.set(Language::Japanese, false, None);
        fallback.set(Language::Japanese, true, Some(serif.clone()));
        assert!(draws(&fallback, "MS-Gothic", japan(), &serif));
        for family in [
            CidFamily::AdobeKorea1,
            CidFamily::AdobeGB1,
            CidFamily::AdobeCNS1,
        ] {
            assert!(draws_none(&fallback, "MS-Mincho", Some(family)));
        }
        fallback.set(Language::Japanese, true, None);
        assert!(fallback.is_empty());
    }

    #[test]
    fn the_same_faces_are_the_same_fallback() {
        let (fallback, _) = every_face();
        assert!(fallback.same(&fallback.clone()));
        assert!(Fallback::default().same(&Fallback::default()));
        let mut other = fallback.clone();
        other.set(Language::Korean, true, Some(face()));
        assert!(!fallback.same(&other));
        other.set(Language::Korean, true, None);
        assert!(!fallback.same(&other));
        let file = Source::File(Arc::new(FontFile::new(PathBuf::from("absent"), None)));
        let mut files = Fallback::default();
        files.set(Language::Korean, true, Some(file.clone()));
        let mut again = Fallback::default();
        again.set(Language::Korean, true, Some(file));
        assert!(files.same(&again));
        assert!(!files.same(&Fallback::default()));
    }

    #[test]
    fn a_font_file_is_read_once_within_its_limit() {
        let name = format!("pyropdf-fallback-{}", std::process::id());
        let directory = std::env::temp_dir().join(name);
        fs::create_dir_all(&directory).unwrap();
        let path = directory.join("face.otf");
        fs::write(&path, b"12345").unwrap();
        let file = FontFile::new(path.clone(), Some(5));
        assert_eq!(read(&file).unwrap(), b"12345");
        // The bytes stay those of the first read.
        fs::write(&path, b"6789").unwrap();
        assert_eq!(read(&file).unwrap(), b"12345");
        assert!(read(&FontFile::new(path.clone(), Some(3))).is_none());
        assert_eq!(read(&FontFile::new(path, None)).unwrap(), b"6789");
        assert!(read(&FontFile::new(directory.join("absent.otf"), None)).is_none());
        fs::remove_dir_all(&directory).unwrap();
    }
}
