//! Understanding Slurm job ids.
//!
//! A job id is not just a number. Array tasks carry a suffix (`2736118_11`, or
//! `2736118_[12-40]` while the array is still pending), heterogeneous jobs use
//! `123+0`, and sacct reports steps as `123.batch`. Sorting, grouping and cache
//! naming all depend on taking those apart correctly, so it lives in one place.

/// The separators that can follow the base id, in the order they are tried.
const SUFFIX_SEPARATORS: [char; 3] = ['_', '+', '.'];

/// One contiguous span of array task indices, with its stride.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ArrayRange {
    pub low: u32,
    pub high: u32,
    pub step: u32,
}

impl ArrayRange {
    /// A range covering exactly one index.
    fn single(index: u32) -> Self {
        Self {
            low: index,
            high: index,
            step: 1,
        }
    }

    /// How many task indices this range covers.
    fn count(&self) -> u32 {
        (self.high - self.low) / self.step.max(1) + 1
    }
}

/// The task-index ranges a job id covers.
///
/// `123_[12-40]` → one range, `123_[1,3,5]` → three single ranges, `123_4` → one
/// single range, a non-array id → empty.
///
/// The spec is parsed explicitly rather than by scraping digits, because not
/// every number in it is a task index:
///
/// - `%` starts a concurrency throttle — `[1-4%10]` runs tasks 1-4, ten at a
///   time — and always terminates the spec, so everything after it goes.
/// - `:` starts a stride — `[0-9:2]` is tasks 0, 2, 4, 6, 8 — so the step bounds
///   the range but is not itself an index.
pub fn array_ranges(job_id: &str) -> Vec<ArrayRange> {
    let Some((_, suffix)) = job_id.split_once('_') else {
        return Vec::new();
    };
    if suffix.is_empty() {
        return Vec::new();
    }

    // A bare `123_4` is one task, not a spec.
    if !suffix.starts_with('[') {
        return match suffix.parse::<u32>() {
            Ok(index) => vec![ArrayRange::single(index)],
            Err(_) => Vec::new(),
        };
    }

    let spec = suffix[1..]
        .split(']')
        .next()
        .unwrap_or_default()
        .split('%')
        .next()
        .unwrap_or_default();

    spec.split(',').filter_map(parse_range_part).collect()
}

/// One comma-separated element of an array spec: `12-40`, `0-9:2` or `7`.
fn parse_range_part(part: &str) -> Option<ArrayRange> {
    let (bounds, stride) = match part.trim().split_once(':') {
        Some((bounds, stride)) => (bounds, stride.trim().parse::<u32>().ok().filter(|s| *s > 0)),
        None => (part.trim(), None),
    };
    let step = stride.unwrap_or(1);

    match bounds.trim().split_once('-') {
        Some((low, high)) => {
            let low = low.trim().parse::<u32>().ok()?;
            match high.trim() {
                // `123_[7-]` is not a range Slurm emits, but treat a missing
                // upper bound as a single index rather than dropping the task.
                "" => Some(ArrayRange::single(low)),
                high => Some(ArrayRange {
                    low,
                    high: high.parse::<u32>().ok()?,
                    step,
                }),
            }
        }
        None => bounds.trim().parse::<u32>().ok().map(ArrayRange::single),
    }
}

/// How many array tasks one squeue/sacct row stands for.
///
/// A pending array arrives as a single row covering a range — `123_[12-40]` is
/// 29 tasks. Running tasks arrive one row each, so anything that is not a range
/// counts as 1. This is why the cluster bar's job counts do not equal the number
/// of table rows.
pub fn array_task_count(job_id: &str) -> u32 {
    let total: u32 = array_ranges(job_id).iter().map(ArrayRange::count).sum();
    total.max(1)
}

/// Lowest and highest task index across several array job ids.
pub fn array_index_span<'a>(job_ids: impl IntoIterator<Item = &'a str>) -> Option<(u32, u32)> {
    let ranges: Vec<ArrayRange> = job_ids.into_iter().flat_map(array_ranges).collect();
    let low = ranges.iter().map(|r| r.low).min()?;
    let high = ranges.iter().map(|r| r.high).max()?;
    Some((low, high))
}

/// Reduce any Slurm job id to the base id that owns the batch script.
///
/// All tasks of an array share one script, so `123_11`, `123_[1-40]`, `123+0`
/// and `123.batch` all map to `123`. Returns `None` if no digits remain, which
/// callers treat as "no valid id" — that guard is also what keeps arbitrary text
/// out of cache filenames.
pub fn base_job_id(job_id: &str) -> Option<&str> {
    let mut head = job_id.trim();
    for separator in SUFFIX_SEPARATORS {
        head = head.split(separator).next().unwrap_or_default();
    }
    (!head.is_empty() && head.bytes().all(|b| b.is_ascii_digit())).then_some(head)
}

/// Sort key for a Slurm job id, to be used **descending**.
///
/// A plain numeric sort fails on array tasks, and every task of one array would
/// collapse to the same key — scattering them through the table. Sorting on
/// `(base id, -task index)` keeps each array together in the right place by
/// submission order, with its tasks reading 0, 1, 2, … down the block. Anything
/// unparseable sorts last.
pub fn sort_key(job_id: &str) -> (i64, i64) {
    let head = job_id.trim();

    let (base, task) = match SUFFIX_SEPARATORS
        .iter()
        .find_map(|sep| head.split_once(*sep))
    {
        Some((base, suffix)) => (base, leading_index(suffix)),
        None => (head, 0),
    };

    match base.parse::<i64>() {
        // Negated so that, sorted descending, tasks within one array stay ascending.
        Ok(id) => (id, -task),
        Err(_) => (-1, 0),
    }
}

/// The first task index in a job-id suffix: `[12-40]` → 12, `11` → 11.
fn leading_index(suffix: &str) -> i64 {
    let digits: String = suffix
        .chars()
        .filter(|c| c.is_ascii_digit() || *c == '-')
        .collect();
    digits
        .split('-')
        .next()
        .and_then(|first| first.parse::<i64>().ok())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use rstest::rstest;

    #[rstest]
    #[case("2736118", 1)]
    #[case("2736118_11", 1)]
    #[case("123_[12-40]", 29)]
    #[case("123_[1,3,5]", 3)]
    // `%` is a concurrency throttle, not a task index.
    #[case("123_[1-4%10]", 4)]
    // `:` is a stride: 0, 2, 4, 6, 8.
    #[case("123_[0-9:2]", 5)]
    #[case("123_[0-11]", 12)]
    fn counts_array_tasks(#[case] job_id: &str, #[case] expected: u32) {
        assert_eq!(array_task_count(job_id), expected);
    }

    #[rstest]
    #[case(vec!["123_0", "123_5", "123_11"], Some((0, 11)))]
    #[case(vec!["123_[12-40]"], Some((12, 40)))]
    #[case(vec!["123", "456"], None)]
    fn finds_index_span(#[case] ids: Vec<&str>, #[case] expected: Option<(u32, u32)>) {
        assert_eq!(array_index_span(ids), expected);
    }

    #[rstest]
    #[case("123", Some("123"))]
    #[case("123_11", Some("123"))]
    #[case("123_[1-40]", Some("123"))]
    #[case("123+0", Some("123"))]
    #[case("123.batch", Some("123"))]
    #[case("notajob", None)]
    #[case("", None)]
    fn reduces_to_base_id(#[case] job_id: &str, #[case] expected: Option<&str>) {
        assert_eq!(base_job_id(job_id), expected);
    }

    #[test]
    fn sorts_arrays_ascending_within_a_descending_list() {
        let mut ids = vec!["100", "200_2", "200_0", "200_1", "150"];
        ids.sort_by(|a, b| sort_key(b).cmp(&sort_key(a)));
        assert_eq!(ids, vec!["200_0", "200_1", "200_2", "150", "100"]);
    }

    #[test]
    fn unparseable_ids_sort_last() {
        let mut ids = vec!["garbage", "100", "200"];
        ids.sort_by(|a, b| sort_key(b).cmp(&sort_key(a)));
        assert_eq!(ids, vec!["200", "100", "garbage"]);
    }
}
