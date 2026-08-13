//! The job-table filter language.
//!
//! A query is a list of terms, ANDed together. Each is either a bare word that
//! searches the whole row, or a `key:value` term that searches one field:
//!
//! ```text
//! train state:pend part:gpu gpu:>=2
//! ```
//!
//! The overriding rule is that **nothing the user types may break the filter**.
//! An unrecognised key, an unbalanced quote, a non-numeric comparison — each
//! degrades to something harmless rather than erroring, because this runs on
//! every keystroke and half-typed input is the normal state.

use crate::model::gres_count;

/// Which field a term searches.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Field {
    State,
    Partition,
    Name,
    Id,
    Gpu,
}

/// Key spellings accepted for each field, including the short forms people
/// actually type.
const KEYS: &[(&str, Field)] = &[
    ("state", Field::State),
    ("st", Field::State),
    ("s", Field::State),
    ("part", Field::Partition),
    ("partition", Field::Partition),
    ("p", Field::Partition),
    ("name", Field::Name),
    ("n", Field::Name),
    ("id", Field::Id),
    ("job", Field::Id),
    ("gpu", Field::Gpu),
    ("gpus", Field::Gpu),
    ("gres", Field::Gpu),
];

/// How a term compares. `Contains` is the default for text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Op {
    Contains,
    Equal,
    NotEqual,
    Greater,
    GreaterEqual,
    Less,
    LessEqual,
}

/// Comparison prefixes, longest first so `>=` is not read as `>`.
const COMPARISONS: &[(&str, Op)] = &[
    (">=", Op::GreaterEqual),
    ("<=", Op::LessEqual),
    ("!=", Op::NotEqual),
    (">", Op::Greater),
    ("<", Op::Less),
    ("=", Op::Equal),
];

impl Op {
    /// Apply this comparison to two numbers.
    fn compare(self, actual: f64, expected: f64) -> bool {
        match self {
            Self::Greater => actual > expected,
            Self::GreaterEqual => actual >= expected,
            Self::Less => actual < expected,
            Self::LessEqual => actual <= expected,
            Self::NotEqual => actual != expected,
            // A bare `gpu:2` means equality, as does an explicit `=`.
            Self::Equal | Self::Contains => actual == expected,
        }
    }
}

/// One parsed search term. `field` is `None` for a bare substring.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Term {
    pub field: Option<Field>,
    pub op: Op,
    pub value: String,
}

impl Term {
    /// A bare word, searching the whole row.
    fn bare(value: &str) -> Self {
        Self {
            field: None,
            op: Op::Contains,
            value: value.to_lowercase(),
        }
    }
}

/// Split a search string into terms, to be ANDed together.
///
/// A `key:` prefix that is not a known field stays a plain substring, so
/// `foo:bar` simply searches for "foo:bar" and `12:30` searches for a time.
pub fn parse_query(text: &str) -> Vec<Term> {
    // An unbalanced quote is normal while typing, so fall back to whitespace
    // splitting rather than reporting an error the user cannot act on yet.
    let tokens = shell_words::split(text)
        .unwrap_or_else(|_| text.split_whitespace().map(String::from).collect());

    tokens
        .iter()
        .filter(|token| !token.is_empty())
        .map(|token| parse_term(token))
        .collect()
}

fn parse_term(token: &str) -> Term {
    let Some((key, value)) = token.split_once(':') else {
        return Term::bare(token);
    };
    let Some(field) = lookup_field(key) else {
        return Term::bare(token);
    };

    let (op, value) = split_comparison(value);
    Term {
        field: Some(field),
        op,
        value: value.trim().to_lowercase(),
    }
}

fn lookup_field(key: &str) -> Option<Field> {
    let key = key.to_lowercase();
    KEYS.iter()
        .find(|(name, _)| *name == key)
        .map(|(_, field)| *field)
}

/// Split a leading comparison operator off a term's value.
fn split_comparison(value: &str) -> (Op, &str) {
    for (prefix, op) in COMPARISONS {
        if let Some(rest) = value.strip_prefix(prefix) {
            return (*op, rest);
        }
    }
    (Op::Contains, value)
}

/// A row the filter can be applied to.
///
/// Implemented by both job types so the table logic is written once. `gres`
/// returns `None` for rows that have no GRES column at all — sacct rows — which
/// is different from having none allocated.
pub trait Filterable {
    fn job_id(&self) -> &str;
    fn name(&self) -> &str;
    fn partition(&self) -> &str;
    fn state(&self) -> &str;
    fn gres(&self) -> Option<&str>;

    /// The fields a bare (non-`key:`) term searches.
    fn search_fields(&self) -> Vec<&str>;
}

impl Filterable for crate::model::RunningJob {
    fn job_id(&self) -> &str {
        &self.job_id
    }
    fn name(&self) -> &str {
        &self.name
    }
    fn partition(&self) -> &str {
        &self.partition
    }
    fn state(&self) -> &str {
        &self.state
    }
    fn gres(&self) -> Option<&str> {
        Some(&self.gres)
    }
    fn search_fields(&self) -> Vec<&str> {
        vec![&self.job_id, &self.name, &self.partition]
    }
}

impl Filterable for crate::model::CompletedJob {
    fn job_id(&self) -> &str {
        &self.job_id
    }
    fn name(&self) -> &str {
        &self.name
    }
    fn partition(&self) -> &str {
        &self.partition
    }
    fn state(&self) -> &str {
        &self.state
    }
    fn gres(&self) -> Option<&str> {
        // sacct reports no GRES column at all — which is not the same as
        // reporting zero, and a gpu: filter must not pretend otherwise.
        None
    }
    fn search_fields(&self) -> Vec<&str> {
        // The state is searchable here because it is a column the user sees.
        vec![&self.job_id, &self.name, &self.partition, &self.state]
    }
}

/// Whether a row satisfies every term.
pub fn matches(job: &impl Filterable, terms: &[Term]) -> bool {
    terms.iter().all(|term| matches_term(job, term))
}

fn matches_term(job: &impl Filterable, term: &Term) -> bool {
    let Some(field) = term.field else {
        return job
            .search_fields()
            .iter()
            .any(|text| text.to_lowercase().contains(&term.value));
    };

    match field {
        // A prefix match, so "fail" finds FAILED and "pend" finds PENDING.
        Field::State => job.state().to_lowercase().starts_with(&term.value),
        Field::Partition => job.partition().to_lowercase().contains(&term.value),
        Field::Name => job.name().to_lowercase().contains(&term.value),
        Field::Id => job.job_id().to_lowercase().contains(&term.value),
        Field::Gpu => {
            // Only squeue rows carry GRES; a gpu: filter matches nothing in the
            // terminated table rather than pretending every job has zero.
            let Some(gres) = job.gres() else {
                return false;
            };
            let wanted = if term.value.is_empty() {
                0.0
            } else {
                match term.value.parse::<f64>() {
                    Ok(number) => number,
                    // "gpu:>abc" matches nothing, rather than everything.
                    Err(_) => return false,
                }
            };
            term.op.compare(f64::from(gres_count(gres)), wanted)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{CompletedJob, RunningJob};
    use rstest::rstest;

    fn running(id: &str, name: &str, partition: &str, state: &str, gres: &str) -> RunningJob {
        RunningJob {
            job_id: id.into(),
            name: name.into(),
            partition: partition.into(),
            state: state.into(),
            gres: gres.into(),
            ..RunningJob::default()
        }
    }

    fn jobs() -> Vec<RunningJob> {
        vec![
            running("100", "train-a", "gpu", "RUNNING", "gres/gpu:2"),
            running("101", "train-b", "gpu", "PENDING", "gres/gpu:1"),
            running("102", "prep", "cpu", "RUNNING", "None"),
            running("103", "train-c", "cpu", "PENDING", "None"),
        ]
    }

    fn completed() -> Vec<CompletedJob> {
        vec![
            CompletedJob {
                job_id: "90".into(),
                name: "train-old".into(),
                state: "COMPLETED".into(),
                partition: "gpu".into(),
                elapsed: "1:00".into(),
                ..CompletedJob::default()
            },
            CompletedJob {
                job_id: "91".into(),
                name: "sweep".into(),
                state: "FAILED".into(),
                partition: "cpu".into(),
                elapsed: "0:10".into(),
                ..CompletedJob::default()
            },
            CompletedJob {
                job_id: "92".into(),
                name: "sweep".into(),
                state: "OUT_OF_MEMORY".into(),
                partition: "gpu".into(),
                elapsed: "0:20".into(),
                ..CompletedJob::default()
            },
        ]
    }

    /// The ids of the jobs a query keeps.
    fn kept<T: Filterable>(items: &[T], query: &str) -> Vec<String> {
        let terms = parse_query(query);
        items
            .iter()
            .filter(|job| matches(*job, &terms))
            .map(|job| job.job_id().to_string())
            .collect()
    }

    #[test]
    fn splits_fields_and_bare_words() {
        assert_eq!(
            parse_query("train state:run gpu:>0"),
            vec![
                Term::bare("train"),
                Term {
                    field: Some(Field::State),
                    op: Op::Contains,
                    value: "run".into()
                },
                Term {
                    field: Some(Field::Gpu),
                    op: Op::Greater,
                    value: "0".into()
                },
            ]
        );
    }

    #[rstest]
    #[case("state:x", Field::State)]
    #[case("st:x", Field::State)]
    #[case("s:x", Field::State)]
    #[case("part:x", Field::Partition)]
    #[case("partition:x", Field::Partition)]
    #[case("p:x", Field::Partition)]
    #[case("name:x", Field::Name)]
    #[case("n:x", Field::Name)]
    #[case("id:x", Field::Id)]
    #[case("job:x", Field::Id)]
    #[case("gpu:1", Field::Gpu)]
    #[case("gpus:1", Field::Gpu)]
    #[case("gres:1", Field::Gpu)]
    fn accepts_every_alias(#[case] text: &str, #[case] expected: Field) {
        assert_eq!(parse_query(text)[0].field, Some(expected));
    }

    #[rstest]
    #[case(">=", Op::GreaterEqual)]
    #[case("<=", Op::LessEqual)]
    #[case("!=", Op::NotEqual)]
    #[case(">", Op::Greater)]
    #[case("<", Op::Less)]
    #[case("=", Op::Equal)]
    fn parses_comparisons(#[case] text: &str, #[case] expected: Op) {
        let term = &parse_query(&format!("gpu:{text}2"))[0];
        assert_eq!(term.op, expected);
        assert_eq!(term.value, "2");
    }

    #[test]
    fn keeps_unknown_keys_as_text() {
        // Nothing a user types may break the filter.
        assert_eq!(parse_query("foo:bar"), vec![Term::bare("foo:bar")]);
        assert_eq!(parse_query("12:30"), vec![Term::bare("12:30")]);
    }

    #[test]
    fn survives_a_half_typed_quote() {
        assert_eq!(
            parse_query("name:\"my job"),
            vec![
                Term {
                    field: Some(Field::Name),
                    op: Op::Contains,
                    value: "\"my".into()
                },
                Term::bare("job"),
            ]
        );
        // A closed quote parses as one value.
        assert_eq!(
            parse_query("name:\"my job\""),
            vec![Term {
                field: Some(Field::Name),
                op: Op::Contains,
                value: "my job".into()
            }]
        );
    }

    #[test]
    fn an_empty_query_has_no_terms() {
        assert!(parse_query("").is_empty());
        assert!(parse_query("   ").is_empty());
    }

    #[test]
    fn terms_are_anded_together() {
        assert_eq!(kept(&jobs(), "state:pend part:gpu"), vec!["101"]);
        assert!(kept(&completed(), "state:pend part:gpu").is_empty());
    }

    #[test]
    fn bare_words_search_the_whole_row() {
        assert_eq!(kept(&jobs(), "train"), vec!["100", "101", "103"]);
    }

    #[test]
    fn bare_and_field_terms_mix() {
        assert_eq!(kept(&jobs(), "train state:run"), vec!["100"]);
    }

    #[test]
    fn state_is_a_prefix_match() {
        assert_eq!(kept(&completed(), "state:fail"), vec!["91"]);
        assert_eq!(kept(&completed(), "state:out"), vec!["92"]);
    }

    #[test]
    fn matching_is_case_insensitive() {
        assert_eq!(kept(&completed(), "state:FaIl"), vec!["91"]);
    }

    #[rstest]
    #[case("gpu:>0", vec!["100", "101"])]
    #[case("gpu:>=2", vec!["100"])]
    #[case("gpu:2", vec!["100"])]
    #[case("gpu:0", vec!["102", "103"])]
    #[case("gpu:<1", vec!["102", "103"])]
    fn compares_gpu_counts(#[case] query: &str, #[case] expected: Vec<&str>) {
        assert_eq!(kept(&jobs(), query), expected);
    }

    #[test]
    fn a_gpu_term_matches_nothing_in_the_terminated_table() {
        // sacct rows carry no GRES, so there is nothing to compare against.
        assert!(kept(&completed(), "gpu:>0").is_empty());
    }

    #[test]
    fn a_gpu_term_with_a_junk_value_matches_nothing() {
        assert!(kept(&jobs(), "gpu:>abc").is_empty());
    }

    #[test]
    fn matches_ids_and_names() {
        assert_eq!(kept(&jobs(), "id:10"), vec!["100", "101", "102", "103"]);
        assert_eq!(kept(&jobs(), "id:101"), vec!["101"]);
        assert_eq!(kept(&jobs(), "name:prep"), vec!["102"]);
    }

    #[test]
    fn an_unknown_key_falls_back_to_a_substring_search() {
        // Matched as text, and nothing contains it.
        assert!(kept(&jobs(), "foo:bar").is_empty());
    }

    #[test]
    fn the_terminated_table_searches_its_state_column() {
        // A bare word finds a state there, which the active table does not do.
        assert_eq!(kept(&completed(), "memory"), vec!["92"]);
    }
}
