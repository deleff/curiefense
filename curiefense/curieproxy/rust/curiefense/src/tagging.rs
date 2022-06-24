use crate::config::globalfilter::{
    GlobalFilterEntry, GlobalFilterEntryE, GlobalFilterSSection, GlobalFilterSection, PairEntry, SingleEntry,
};
use crate::config::raw::Relation;
use crate::interface::stats::{BStageMapped, BStageSecpol, StatsCollect};
use crate::interface::{BlockReason, Location, SimpleActionT, SimpleDecision, Tags};
use crate::requestfields::RequestField;
use crate::utils::RequestInfo;
use std::collections::HashSet;
use std::net::IpAddr;

struct MatchResult {
    matched: HashSet<Location>,
    matching: bool,
}

fn check_relation<A, F>(rinfo: &RequestInfo, tags: &Tags, rel: Relation, elems: &[A], checker: F) -> MatchResult
where
    F: Fn(&RequestInfo, &Tags, &A) -> MatchResult,
{
    let mut matched = HashSet::new();
    let mut matching = match rel {
        Relation::And => true,
        Relation::Or => false,
    };
    for sub in elems {
        let mtch = checker(rinfo, tags, sub);
        matched.extend(mtch.matched);
        matching = match rel {
            Relation::And => matching && mtch.matching,
            Relation::Or => matching || mtch.matching,
        };
    }
    MatchResult { matched, matching }
}

fn check_pair<F>(pr: &PairEntry, s: &RequestField, locf: F) -> Option<HashSet<Location>>
where
    F: Fn(&str) -> Location,
{
    s.get(&pr.key).and_then(|v| {
        if &pr.exact == v || pr.re.as_ref().map(|re| re.is_match(v)).unwrap_or(false) {
            Some(std::iter::once(locf(v)).collect())
        } else {
            None
        }
    })
}

fn check_single(pr: &SingleEntry, s: &str, loc: Location) -> Option<HashSet<Location>> {
    if pr.exact == s || pr.re.as_ref().map(|re| re.is_match(s)).unwrap_or(false) {
        Some(std::iter::once(loc).collect())
    } else {
        None
    }
}

fn check_entry(rinfo: &RequestInfo, tags: &Tags, sub: &GlobalFilterEntry) -> MatchResult {
    fn bool(loc: Location, b: bool) -> Option<HashSet<Location>> {
        if b {
            Some(std::iter::once(loc).collect())
        } else {
            None
        }
    }
    fn mbool(loc: Location, mb: Option<bool>) -> Option<HashSet<Location>> {
        bool(loc, mb.unwrap_or(false))
    }
    let r = match &sub.entry {
        GlobalFilterEntryE::Ip(addr) => mbool(Location::Ip, rinfo.rinfo.geoip.ip.map(|i| &i == addr)),
        GlobalFilterEntryE::Network(net) => mbool(Location::Ip, rinfo.rinfo.geoip.ip.map(|i| net.contains(&i))),
        GlobalFilterEntryE::Range4(net4) => bool(
            Location::Ip,
            match rinfo.rinfo.geoip.ip {
                Some(IpAddr::V4(ip4)) => net4.contains(&ip4),
                _ => false,
            },
        ),
        GlobalFilterEntryE::Range6(net6) => bool(
            Location::Ip,
            match rinfo.rinfo.geoip.ip {
                Some(IpAddr::V6(ip6)) => net6.contains(&ip6),
                _ => false,
            },
        ),
        GlobalFilterEntryE::Path(pth) => check_single(pth, &rinfo.rinfo.qinfo.qpath, Location::Path),
        GlobalFilterEntryE::Query(qry) => check_single(qry, &rinfo.rinfo.qinfo.query, Location::Path),
        GlobalFilterEntryE::Uri(uri) => check_single(uri, &rinfo.rinfo.qinfo.uri, Location::Uri),
        GlobalFilterEntryE::Country(cty) => rinfo
            .rinfo
            .geoip
            .country_iso
            .as_ref()
            .and_then(|ccty| check_single(cty, ccty.to_lowercase().as_ref(), Location::Ip)),
        GlobalFilterEntryE::Region(cty) => rinfo
            .rinfo
            .geoip
            .region
            .as_ref()
            .and_then(|ccty| check_single(cty, ccty.to_lowercase().as_ref(), Location::Ip)),
        GlobalFilterEntryE::SubRegion(cty) => rinfo
            .rinfo
            .geoip
            .subregion
            .as_ref()
            .and_then(|ccty| check_single(cty, ccty.to_lowercase().as_ref(), Location::Ip)),
        GlobalFilterEntryE::Method(mtd) => check_single(mtd, &rinfo.rinfo.meta.method, Location::Request),
        GlobalFilterEntryE::Header(hdr) => check_pair(hdr, &rinfo.headers, |h| {
            Location::HeaderValue(hdr.key.clone(), h.to_string())
        }),
        GlobalFilterEntryE::Args(arg) => check_pair(arg, &rinfo.rinfo.qinfo.args, |a| {
            Location::UriArgumentValue(arg.key.clone(), a.to_string())
        }),
        GlobalFilterEntryE::Cookies(arg) => check_pair(arg, &rinfo.cookies, |c| {
            Location::CookieValue(arg.key.clone(), c.to_string())
        }),
        GlobalFilterEntryE::Asn(asn) => mbool(Location::Ip, rinfo.rinfo.geoip.asn.map(|casn| casn == *asn)),
        GlobalFilterEntryE::Company(cmp) => rinfo
            .rinfo
            .geoip
            .company
            .as_ref()
            .and_then(|ccmp| check_single(cmp, ccmp.as_str(), Location::Ip)),
        GlobalFilterEntryE::Authority(at) => check_single(at, &rinfo.rinfo.host, Location::Request),
        GlobalFilterEntryE::Tag(tg) => tags.get(&tg.exact).cloned(),
    };
    match r {
        Some(matched) => MatchResult {
            matched,
            matching: !sub.negated,
        },
        None => MatchResult {
            matched: HashSet::new(),
            matching: sub.negated,
        },
    }
}

fn check_subsection(rinfo: &RequestInfo, tags: &Tags, sub: &GlobalFilterSSection) -> MatchResult {
    check_relation(rinfo, tags, sub.relation, &sub.entries, check_entry)
}

pub fn tag_request(
    stats: StatsCollect<BStageSecpol>,
    is_human: bool,
    globalfilters: &[GlobalFilterSection],
    rinfo: &RequestInfo,
) -> (Tags, SimpleDecision, StatsCollect<BStageMapped>) {
    let mut tags = Tags::default();
    if is_human {
        tags.insert("human", Location::Request);
    } else {
        tags.insert("bot", Location::Request);
    }
    tags.insert_qualified("headers", &rinfo.headers.len().to_string(), Location::Headers);
    tags.insert_qualified("cookies", &rinfo.cookies.len().to_string(), Location::Cookies);
    tags.insert_qualified("args", &rinfo.rinfo.qinfo.args.len().to_string(), Location::Request);
    tags.insert_qualified("host", &rinfo.rinfo.host, Location::Request);
    tags.insert_qualified("ip", &rinfo.rinfo.geoip.ipstr, Location::Request);
    tags.insert_qualified(
        "geo-continent-name",
        rinfo.rinfo.geoip.continent_name.as_deref().unwrap_or("nil"),
        Location::Request,
    );
    tags.insert_qualified(
        "geo-continent-code",
        rinfo.rinfo.geoip.continent_code.as_deref().unwrap_or("nil"),
        Location::Request,
    );
    tags.insert_qualified(
        "geo-city",
        rinfo.rinfo.geoip.city_name.as_deref().unwrap_or("nil"),
        Location::Request,
    );
    tags.insert_qualified(
        "geo-country",
        rinfo.rinfo.geoip.country_name.as_deref().unwrap_or("nil"),
        Location::Request,
    );
    tags.insert_qualified(
        "geo-region",
        rinfo.rinfo.geoip.region.as_deref().unwrap_or("nil"),
        Location::Request,
    );
    tags.insert_qualified(
        "geo-subregion",
        rinfo.rinfo.geoip.subregion.as_deref().unwrap_or("nil"),
        Location::Request,
    );
    match rinfo.rinfo.geoip.asn {
        None => {
            tags.insert_qualified("geo-asn", "nil", Location::Request);
        }
        Some(asn) => {
            let sasn = format!("{}", asn);
            tags.insert_qualified("geo-asn", &sasn, Location::Request);
        }
    }
    let mut matched = 0;
    for psection in globalfilters {
        let mtch = check_relation(rinfo, &tags, psection.relation, &psection.sections, check_subsection);
        if mtch.matching {
            matched += 1;
            let rtags = psection.tags.clone().with_locs(&mtch.matched);
            tags.extend(rtags);
            if let Some(a) = &psection.action {
                if a.atype == SimpleActionT::Monitor || (a.atype == SimpleActionT::Challenge && is_human) {
                    continue;
                } else {
                    return (
                        tags.clone(),
                        SimpleDecision::Action(
                            a.clone(),
                            vec![BlockReason::global_filter(psection.id.clone(), psection.name.clone())],
                        ),
                        stats.mapped(globalfilters.len(), matched),
                    );
                }
            }
        }
    }
    (tags, SimpleDecision::Pass, stats.mapped(globalfilters.len(), matched))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::globalfilter::optimize_ipranges;
    use crate::logs::Logs;
    use crate::utils::map_request;
    use crate::utils::RawRequest;
    use crate::utils::RequestMeta;
    use regex::Regex;
    use std::collections::HashMap;

    fn mk_rinfo() -> RequestInfo {
        let raw_headers = [
            ("content-type", "/sson"),
            ("x-forwarded-for", "52.78.12.56"),
            (":method", "GET"),
            (":authority", "localhost:30081"),
            (":path", "/adminl%20e?lol=boo&bar=bze&%20encoded=%20%20%20"),
            ("x-forwarded-proto", "http"),
            ("x-request-id", "af36dcec-524d-4d21-b90e-22d5798a6300"),
            ("accept", "*/*"),
            ("user-agent", "curl/7.58.0"),
            ("x-envoy-internal", "true"),
        ];
        let mut headers = HashMap::<String, String>::new();
        let mut attrs = HashMap::<String, String>::new();

        for (k, v) in raw_headers.iter() {
            match k.strip_prefix(':') {
                None => {
                    headers.insert(k.to_string(), v.to_string());
                }
                Some(ak) => {
                    attrs.insert(ak.to_string(), v.to_string());
                }
            }
        }
        let meta = RequestMeta::from_map(attrs).unwrap();
        let mut logs = Logs::default();
        map_request(
            &mut logs,
            &[],
            &[],
            500,
            &RawRequest {
                ipstr: "52.78.12.56".to_string(),
                headers,
                meta,
                mbody: None,
            },
        )
    }

    fn t_check_entry(negated: bool, entry: GlobalFilterEntryE) -> MatchResult {
        check_entry(&mk_rinfo(), &Tags::default(), &GlobalFilterEntry { negated, entry })
    }

    fn single_re(input: &str) -> SingleEntry {
        SingleEntry {
            exact: input.to_string(),
            re: Regex::new(input).ok(),
        }
    }

    fn double_re(key: &str, input: &str) -> PairEntry {
        PairEntry {
            key: key.to_string(),
            exact: input.to_string(),
            re: Regex::new(input).ok(),
        }
    }

    #[test]
    fn check_entry_ip_in() {
        let r = t_check_entry(false, GlobalFilterEntryE::Ip("52.78.12.56".parse().unwrap()));
        assert!(r.matching);
    }
    #[test]
    fn check_entry_ip_in_neg() {
        let r = t_check_entry(true, GlobalFilterEntryE::Ip("52.78.12.56".parse().unwrap()));
        assert!(!r.matching);
    }
    #[test]
    fn check_entry_ip_out() {
        let r = t_check_entry(false, GlobalFilterEntryE::Ip("52.78.12.57".parse().unwrap()));
        assert!(!r.matching);
    }

    #[test]
    fn check_path_in() {
        let r = t_check_entry(false, GlobalFilterEntryE::Path(single_re(".*adminl%20e.*")));
        assert!(r.matching);
    }

    #[test]
    fn check_path_in_not_partial_match() {
        let r = t_check_entry(false, GlobalFilterEntryE::Path(single_re("adminl%20e")));
        assert!(r.matching);
    }

    #[test]
    fn check_path_out() {
        let r = t_check_entry(false, GlobalFilterEntryE::Path(single_re(".*adminl e.*")));
        assert!(!r.matching);
    }

    #[test]
    fn check_headers_exact() {
        let r = t_check_entry(false, GlobalFilterEntryE::Header(double_re("accept", "*/*")));
        assert!(r.matching);
    }

    #[test]
    fn check_headers_match() {
        let r = t_check_entry(false, GlobalFilterEntryE::Header(double_re("user-agent", "^curl.*")));
        assert!(r.matching);
    }

    fn mk_globalfilterentries(lst: &[&str]) -> Vec<GlobalFilterEntry> {
        lst.iter()
            .map(|e| match e.strip_prefix('!') {
                None => GlobalFilterEntry {
                    negated: false,
                    entry: GlobalFilterEntryE::Network(e.parse().unwrap()),
                },
                Some(sub) => GlobalFilterEntry {
                    negated: true,
                    entry: GlobalFilterEntryE::Network(sub.parse().unwrap()),
                },
            })
            .collect()
    }

    fn optimize(ss: &GlobalFilterSSection) -> GlobalFilterSSection {
        GlobalFilterSSection {
            relation: ss.relation,
            entries: optimize_ipranges(ss.relation, ss.entries.clone()),
        }
    }

    fn check_iprange(rel: Relation, input: &[&str], samples: &[(&str, bool)]) {
        let entries = mk_globalfilterentries(input);
        let ssection = GlobalFilterSSection { entries, relation: rel };
        let optimized = optimize(&ssection);
        let tags = Tags::default();

        let mut ri = mk_rinfo();
        for (ip, expected) in samples {
            ri.rinfo.geoip.ip = Some(ip.parse().unwrap());
            println!("UN {} {:?}", ip, ssection);
            assert_eq!(check_subsection(&ri, &tags, &ssection).matching, *expected);
            println!("OP {} {:?}", ip, optimized);
            assert_eq!(check_subsection(&ri, &tags, &optimized).matching, *expected);
        }
    }

    #[test]
    fn ipranges_simple() {
        let entries = ["192.168.1.0/24"];
        let samples = [
            ("10.0.4.1", false),
            ("192.168.0.23", false),
            ("192.168.1.23", true),
            ("192.170.2.45", false),
        ];
        check_iprange(Relation::And, &entries, &samples);
    }

    #[test]
    fn ipranges_intersected() {
        let entries = ["192.168.0.0/23", "192.168.1.0/24"];
        let samples = [
            ("10.0.4.1", false),
            ("192.168.0.23", false),
            ("192.168.1.23", true),
            ("192.170.2.45", false),
        ];
        check_iprange(Relation::And, &entries, &samples);
    }

    #[test]
    fn ipranges_simple_substraction() {
        let entries = ["192.168.0.0/23", "!192.168.1.0/24"];
        let samples = [
            ("10.0.4.1", false),
            ("192.168.0.23", true),
            ("192.168.1.23", false),
            ("192.170.2.45", false),
        ];
        check_iprange(Relation::And, &entries, &samples);
    }

    #[test]
    fn ipranges_simple_union() {
        let entries = ["192.168.0.0/24", "192.168.1.0/24"];
        let samples = [
            ("10.0.4.1", false),
            ("192.168.0.23", true),
            ("192.168.1.23", true),
            ("192.170.2.45", false),
        ];
        check_iprange(Relation::Or, &entries, &samples);
    }

    #[test]
    fn ipranges_larger_union() {
        let entries = ["192.168.0.0/24", "192.168.2.0/24", "10.1.0.0/16", "10.4.0.0/16"];
        let samples = [
            ("10.4.4.1", true),
            ("10.2.2.1", false),
            ("192.168.0.23", true),
            ("192.168.1.23", false),
            ("192.170.2.45", false),
        ];
        check_iprange(Relation::Or, &entries, &samples);
    }
}
