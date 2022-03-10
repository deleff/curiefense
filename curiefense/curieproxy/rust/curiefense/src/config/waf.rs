use crate::config::raw::{RawWafEntryMatch, RawWafProfile, RawWafProperties, WafSignature};
use crate::logs::Logs;

use hyperscan::prelude::{pattern, Builder, CompileFlags, Pattern, Patterns, VectoredDatabase};
use hyperscan::Vectored;
use regex::Regex;
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::iter::FromIterator;

#[derive(Debug, Clone)]
pub struct Section<A> {
    pub headers: A,
    pub cookies: A,
    pub args: A,
    pub path: A,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Transformation {
    Base64Decode,
    HtmlEntitiesDecode,
    UnicodeDecode,
    UrlDecode,
}

impl Transformation {
    pub const DEFAULTPOLICY: [Transformation; 4] = [
        Transformation::Base64Decode,
        Transformation::HtmlEntitiesDecode,
        Transformation::UnicodeDecode,
        Transformation::UrlDecode,
    ];
}

// TODO: undefined data structures
#[derive(Debug, Clone)]
pub struct WafProfile {
    pub id: String,
    pub name: String,
    pub ignore_alphanum: bool,
    pub sections: Section<WafSection>,
    pub decoding: Vec<Transformation>,
}

impl Default for WafProfile {
    fn default() -> Self {
        WafProfile {
            id: "__default__".to_string(),
            name: "default waf".to_string(),
            ignore_alphanum: true,
            sections: Section {
                headers: WafSection {
                    max_count: 42,
                    max_length: 1024,
                    names: HashMap::new(),
                    regex: Vec::new(),
                },
                args: WafSection {
                    max_count: 512,
                    max_length: 1024,
                    names: HashMap::new(),
                    regex: Vec::new(),
                },
                cookies: WafSection {
                    max_count: 42,
                    max_length: 1024,
                    names: HashMap::new(),
                    regex: Vec::new(),
                },
                path: WafSection {
                    max_count: 42,
                    max_length: 2048,
                    names: HashMap::new(),
                    regex: Vec::new(),
                },
            },
            decoding: Transformation::DEFAULTPOLICY.to_vec(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct WafSection {
    pub max_count: usize,
    pub max_length: usize,
    pub names: HashMap<String, WafEntryMatch>,
    pub regex: Vec<(Regex, WafEntryMatch)>,
}

#[derive(Debug, Clone)]
pub struct WafEntryMatch {
    pub reg: Option<Regex>,
    pub restrict: bool,
    pub mask: bool,
    pub exclusions: HashSet<String>,
}

#[derive(Debug, Clone, Eq, Serialize, PartialEq, Copy)]
#[serde(rename_all = "snake_case")]
pub enum SectionIdx {
    Headers,
    Cookies,
    Args,
    Path,
}

impl<A> Section<A> {
    pub fn get(&self, idx: SectionIdx) -> &A {
        match idx {
            SectionIdx::Headers => &self.headers,
            SectionIdx::Cookies => &self.cookies,
            SectionIdx::Args => &self.args,
            SectionIdx::Path => &self.path,
        }
    }

    pub fn at(&mut self, idx: SectionIdx) -> &mut A {
        match idx {
            SectionIdx::Headers => &mut self.headers,
            SectionIdx::Cookies => &mut self.cookies,
            SectionIdx::Args => &mut self.args,
            SectionIdx::Path => &mut self.path,
        }
    }
}

impl<A> Default for Section<A>
where
    A: Default,
{
    fn default() -> Self {
        Section {
            headers: Default::default(),
            cookies: Default::default(),
            args: Default::default(),
            path: Default::default(),
        }
    }
}

pub struct WafSignatures {
    pub db: VectoredDatabase,
    pub ids: Vec<WafSignature>,
}

impl WafSignatures {
    pub fn empty() -> Self {
        let pattern: Pattern = pattern! { "^TEST$" };
        WafSignatures {
            db: pattern.build().unwrap(),
            ids: Vec::new(),
        }
    }
}

fn mk_entry_match(em: RawWafEntryMatch) -> anyhow::Result<(String, WafEntryMatch)> {
    let reg = match em.reg {
        None => None,
        Some(s) => {
            // an empty string means that there is no regex set
            if s.is_empty() {
                None
            } else {
                Some(Regex::new(&s)?)
            }
        }
    };
    Ok((
        em.key,
        WafEntryMatch {
            restrict: em.restrict,
            mask: em.mask.unwrap_or(false),
            exclusions: em
                .exclusions
                .into_iter()
                .map(|mp| mp.into_iter().map(|(a, _)| a))
                .flatten()
                .collect(),
            reg,
        },
    ))
}

fn mk_section(props: RawWafProperties, max_length: usize, max_count: usize) -> anyhow::Result<WafSection> {
    let mnames: anyhow::Result<HashMap<String, WafEntryMatch>> = props.names.into_iter().map(mk_entry_match).collect();
    let mregex: anyhow::Result<Vec<(Regex, WafEntryMatch)>> = props
        .regex
        .into_iter()
        .map(|e| {
            let (s, v) = mk_entry_match(e)?;
            let re = Regex::new(&s)?;
            Ok((re, v))
        })
        .collect();
    Ok(WafSection {
        max_count,
        max_length,
        names: mnames?,
        regex: mregex?,
    })
}

fn convert_entry(entry: RawWafProfile) -> anyhow::Result<(String, WafProfile)> {
    Ok((
        entry.id.clone(),
        WafProfile {
            id: entry.id,
            name: entry.name,
            ignore_alphanum: entry.ignore_alphanum,
            sections: Section {
                headers: mk_section(entry.headers, entry.max_header_length, entry.max_headers_count)?,
                cookies: mk_section(entry.cookies, entry.max_cookie_length, entry.max_cookies_count)?,
                args: mk_section(entry.args, entry.max_arg_length, entry.max_args_count)?,
                path: mk_section(entry.path, entry.max_arg_length, entry.max_args_count)?,
            },
            decoding: vec![
                Transformation::Base64Decode,
                Transformation::UrlDecode,
                Transformation::HtmlEntitiesDecode,
                Transformation::UnicodeDecode,
            ],
        },
    ))
}

impl WafProfile {
    pub fn resolve(logs: &mut Logs, raw: Vec<RawWafProfile>) -> HashMap<String, WafProfile> {
        let mut out = HashMap::new();
        for rp in raw {
            let id = rp.id.clone();
            match convert_entry(rp) {
                Ok((k, v)) => {
                    out.insert(k, v);
                }
                Err(rr) => logs.error(format!("waf id {}: {:?}", id, rr)),
            }
        }
        out
    }
}

fn convert_signature(entry: &WafSignature) -> anyhow::Result<Pattern> {
    Pattern::with_flags(
        &entry.operand,
        CompileFlags::MULTILINE | CompileFlags::DOTALL | CompileFlags::CASELESS,
    )
}

pub fn resolve_signatures(raws: Vec<WafSignature>) -> anyhow::Result<WafSignatures> {
    let patterns: anyhow::Result<Vec<Pattern>> = raws.iter().map(convert_signature).collect();
    let ptrns: Patterns = Patterns::from_iter(patterns?);
    Ok(WafSignatures {
        db: ptrns.build::<Vectored>()?,
        ids: raws,
    })
}
