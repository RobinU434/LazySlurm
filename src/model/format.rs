//! Parsing and formatting of the value shapes Slurm emits.
//!
//! Slurm is inconsistent about units and about what it prints when it has no
//! answer, so each of these accepts the full range of shapes seen in the wild
//! and returns `None`/`0` rather than failing. A parse error here would take
//! down a panel over one odd row.

/// Values Slurm prints in place of a number it does not have.
const MISSING: &[&str] = &["N/A", "Unknown", ""];

/// Additionally, values that are durations syntactically but not quantities.
const NOT_A_DURATION: &[&str] = &["UNLIMITED", "Partition_Limit", "INVALID"];

/// Placeholders Slurm uses for an empty GRES/TRES string.
const EMPTY_SPEC: &[&str] = &["(null)", "N/A", "None"];

/// Parse a Slurm memory string — `1234K`, `512M`, `2.5G` — into bytes.
///
/// Slurm's per-node/per-cpu markers (`64Gn`, `4Gc`) are handled by
/// [`parse_req_mem`]; this takes the plain size only.
pub fn parse_mem_bytes(text: &str) -> Option<f64> {
    let text = text.trim();
    if MISSING.contains(&text) {
        return None;
    }

    let multiplier = text.chars().last().and_then(unit_multiplier);
    match multiplier {
        Some(factor) => {
            let number = &text[..text.len() - 1];
            number.trim().parse::<f64>().ok().map(|n| n * factor)
        }
        None => text.parse::<f64>().ok(),
    }
}

/// The byte multiplier for a size suffix, or `None` if it is not one.
fn unit_multiplier(suffix: char) -> Option<f64> {
    const KIB: f64 = 1024.0;
    match suffix.to_ascii_uppercase() {
        'K' => Some(KIB),
        'M' => Some(KIB * KIB),
        'G' => Some(KIB * KIB * KIB),
        'T' => Some(KIB * KIB * KIB * KIB),
        'P' => Some(KIB * KIB * KIB * KIB * KIB),
        _ => None,
    }
}

/// Seconds from any duration shape sacct emits.
///
/// Handles `1-04:09:36` (with days), `06:31:12`, `00:43.900` (MM:SS with
/// milliseconds) and a bare `43.9`. `UNLIMITED` and `Partition_Limit` are not
/// durations and come back as `None` — a caller must not treat them as zero,
/// which would read as "no time limit used" rather than "no time limit set".
pub fn parse_duration(text: &str) -> Option<f64> {
    let value = text.trim();
    if MISSING.contains(&value) || NOT_A_DURATION.contains(&value) {
        return None;
    }

    // A leading `D-` prefix carries whole days.
    let (days, rest) = match value.split_once('-') {
        Some((head, rest)) => (head.parse::<f64>().ok()?, rest),
        None => (0.0, value),
    };

    let mut numbers: Vec<f64> = Vec::with_capacity(3);
    for part in rest.split(':') {
        numbers.push(part.parse::<f64>().ok()?);
    }
    if numbers.is_empty() || numbers.len() > 3 {
        return None;
    }
    // Pad from the left so `MM:SS` becomes `0:MM:SS` and `SS` becomes `0:0:SS`.
    while numbers.len() < 3 {
        numbers.insert(0, 0.0);
    }

    Some(days * 86_400.0 + numbers[0] * 3600.0 + numbers[1] * 60.0 + numbers[2])
}

/// Total bytes a job asked for, whatever units sacct used.
///
/// Slurm before 21.08 qualifies the number: `64Gn` is per node, `4Gc` is per
/// CPU. Newer versions report the job total with no marker at all, so an
/// unmarked value is taken as-is rather than multiplied — getting this backwards
/// inflates the request by the node count and makes every job look efficient.
pub fn parse_req_mem(raw: &str, alloc_cpus: u32, nnodes: u32) -> Option<f64> {
    let value = raw.trim();
    if MISSING.contains(&value) || value == "0" {
        return None;
    }

    let (per, size_text) = match value.chars().last() {
        Some(marker @ ('n' | 'c')) => (Some(marker), &value[..value.len() - 1]),
        _ => (None, value),
    };

    let size = parse_mem_bytes(size_text)?;
    Some(match per {
        Some('n') => size * f64::from(nnodes.max(1)),
        Some('c') => size * f64::from(alloc_cpus.max(1)),
        _ => size,
    })
}

/// Bytes as the shortest sensible Slurm-style size: `2.6G`, `512M`, `177M`.
///
/// One decimal below ten units, none at or above, so the column stays narrow.
pub fn format_bytes(value: f64) -> String {
    const UNITS: [(&str, f64); 4] = [
        ("T", 1024.0 * 1024.0 * 1024.0 * 1024.0),
        ("G", 1024.0 * 1024.0 * 1024.0),
        ("M", 1024.0 * 1024.0),
        ("K", 1024.0),
    ];
    for (unit, size) in UNITS {
        if value >= size {
            let scaled = value / size;
            return if scaled < 10.0 {
                format!("{scaled:.1}{unit}")
            } else {
                format!("{scaled:.0}{unit}")
            };
        }
    }
    format!("{value:.0}B")
}

/// Seconds as `6:37:27` / `17:02` / `43s`.
pub fn format_duration(seconds: f64) -> String {
    let seconds = seconds as i64;
    let (hours, rest) = (seconds / 3600, seconds % 3600);
    let (minutes, secs) = (rest / 60, rest % 60);
    if hours != 0 {
        format!("{hours}:{minutes:02}:{secs:02}")
    } else if minutes != 0 {
        format!("{minutes}:{secs:02}")
    } else {
        format!("{secs}s")
    }
}

/// Total device count in a GRES string.
///
/// `gpu:a100:8(S:0-1)` → 8, `gpu:a100:7(IDX:0-6)` → 7, `gpu:2` → 2, and
/// `(null)`/`""` → 0. Comma-separated entries are summed.
pub fn gres_count(spec: &str) -> u32 {
    let spec = spec.trim();
    if spec.is_empty() || EMPTY_SPEC.contains(&spec) {
        return 0;
    }

    spec.split(',')
        .filter_map(|entry| {
            // Drop any "(S:0-1)" / "(IDX:0-7)" topology suffix.
            let head = entry.split('(').next().unwrap_or_default();
            let parts: Vec<&str> = head.split(':').filter(|p| !p.is_empty()).collect();
            // A bare name with no count ("gpu") carries no number to add.
            if parts.len() < 2 {
                return None;
            }
            parts.last()?.parse::<u32>().ok()
        })
        .sum()
}

/// Parse Slurm's elapsed format — `[DD-]HH:MM:SS`, `MM:SS`, `N/A` — to seconds.
///
/// Unlike [`parse_duration`] this is integer-only and total-order friendly: it
/// exists to sort and compare elapsed columns, so an unparseable value sorts as
/// zero rather than propagating a `None` into every comparison.
pub fn elapsed_seconds(text: &str) -> i64 {
    let text = text.trim();
    if MISSING.contains(&text) || text == "INVALID" {
        return 0;
    }

    let (days, rest) = match text.split_once('-') {
        Some((head, rest)) => (head.parse::<i64>().unwrap_or(0), rest),
        None => (0, text),
    };

    let mut values: Vec<i64> = Vec::with_capacity(3);
    for part in rest.split(':') {
        match part.parse::<i64>() {
            Ok(value) => values.push(value),
            Err(_) => return 0,
        }
    }
    while values.len() < 3 {
        values.insert(0, 0);
    }
    let tail = &values[values.len() - 3..];

    days * 86_400 + tail[0] * 3600 + tail[1] * 60 + tail[2]
}

#[cfg(test)]
mod tests {
    use super::*;
    use rstest::rstest;

    #[rstest]
    #[case("1024", Some(1024.0))]
    #[case("1K", Some(1024.0))]
    #[case("512M", Some(512.0 * 1024.0 * 1024.0))]
    #[case("2.5G", Some(2.5 * 1024.0 * 1024.0 * 1024.0))]
    #[case("1T", Some(1024.0f64.powi(4)))]
    #[case("N/A", None)]
    #[case("Unknown", None)]
    #[case("", None)]
    #[case("garbage", None)]
    fn parses_memory(#[case] input: &str, #[case] expected: Option<f64>) {
        assert_eq!(parse_mem_bytes(input), expected);
    }

    #[rstest]
    #[case("1-04:09:36", Some(101_376.0))]
    #[case("06:31:12", Some(23_472.0))]
    #[case("00:43.900", Some(43.9))]
    #[case("43.9", Some(43.9))]
    #[case("UNLIMITED", None)]
    #[case("Partition_Limit", None)]
    #[case("INVALID", None)]
    #[case("N/A", None)]
    #[case("1:2:3:4", None)]
    fn parses_duration(#[case] input: &str, #[case] expected: Option<f64>) {
        assert_eq!(parse_duration(input), expected);
    }

    #[rstest]
    // An unmarked value is already the job total and must not be scaled.
    #[case("4G", 8, 2, Some(4.0 * 1024.0 * 1024.0 * 1024.0))]
    #[case("4Gn", 8, 2, Some(8.0 * 1024.0 * 1024.0 * 1024.0))]
    #[case("4Gc", 8, 2, Some(32.0 * 1024.0 * 1024.0 * 1024.0))]
    #[case("0", 1, 1, None)]
    #[case("N/A", 1, 1, None)]
    fn parses_requested_memory(
        #[case] raw: &str,
        #[case] cpus: u32,
        #[case] nodes: u32,
        #[case] expected: Option<f64>,
    ) {
        assert_eq!(parse_req_mem(raw, cpus, nodes), expected);
    }

    #[rstest]
    #[case(2.6 * 1024.0 * 1024.0 * 1024.0, "2.6G")]
    #[case(512.0 * 1024.0 * 1024.0, "512M")]
    #[case(177.0 * 1024.0 * 1024.0, "177M")]
    #[case(1024.0, "1.0K")]
    #[case(512.0, "512B")]
    fn formats_bytes(#[case] value: f64, #[case] expected: &str) {
        assert_eq!(format_bytes(value), expected);
    }

    #[rstest]
    #[case(23_847.0, "6:37:27")]
    #[case(1022.0, "17:02")]
    #[case(43.0, "43s")]
    #[case(0.0, "0s")]
    fn formats_duration(#[case] value: f64, #[case] expected: &str) {
        assert_eq!(format_duration(value), expected);
    }

    #[rstest]
    #[case("gpu:a100:8(S:0-1)", 8)]
    #[case("gpu:a100:7(IDX:0-6)", 7)]
    #[case("gpu:2", 2)]
    #[case("gpu:a100:4,gpu:v100:2", 6)]
    #[case("(null)", 0)]
    #[case("", 0)]
    #[case("gpu", 0)]
    fn counts_gres(#[case] spec: &str, #[case] expected: u32) {
        assert_eq!(gres_count(spec), expected);
    }

    #[rstest]
    #[case("1-02:03:04", 93_784)]
    #[case("02:03:04", 7384)]
    #[case("03:04", 184)]
    #[case("N/A", 0)]
    #[case("", 0)]
    #[case("garbage", 0)]
    fn parses_elapsed(#[case] input: &str, #[case] expected: i64) {
        assert_eq!(elapsed_seconds(input), expected);
    }
}
