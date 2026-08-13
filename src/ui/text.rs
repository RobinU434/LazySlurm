//! Fitting text into a fixed number of terminal columns.
//!
//! Width is measured in **display columns**, not characters. A CJK ideograph
//! occupies two columns, a combining accent none at all, so counting `char`s
//! misplaces every column to the right of a job name that contains either.
//! Slurm itself imposes no restriction on job names, and `sbatch -J` will
//! happily take any UTF-8.

use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

/// The marker appended to text that had to be cut.
const ELLIPSIS: char = '…';

/// How many terminal columns a string occupies.
pub fn width(text: &str) -> usize {
    UnicodeWidthStr::width(text)
}

/// Truncate `text` to `max_width` display columns, marking it with `…`.
///
/// A `max_width` of zero means unlimited, matching the config file's
/// `max_name_width = 0`.
pub fn truncate(text: &str, max_width: usize) -> String {
    if max_width == 0 || width(text) <= max_width {
        return text.to_string();
    }
    // The ellipsis itself needs a column, so the text gets one fewer.
    let budget = max_width.saturating_sub(1);

    let mut out = String::new();
    let mut used = 0;
    for character in text.chars() {
        let advance = UnicodeWidthChar::width(character).unwrap_or(0);
        if used + advance > budget {
            break;
        }
        out.push(character);
        used += advance;
    }
    out.push(ELLIPSIS);
    out
}

/// Break `text` into lines that each fit within `columns` display columns.
///
/// Hard-wraps rather than breaking on word boundaries: job logs are full of
/// paths, stack traces and progress bars, where breaking mid-token loses less
/// than reflowing would. An existing line short enough to fit is left alone.
pub fn wrap(text: &str, columns: usize) -> Vec<String> {
    if columns == 0 {
        return text.lines().map(str::to_string).collect();
    }

    let mut wrapped = Vec::new();
    for line in text.lines() {
        if width(line) <= columns {
            wrapped.push(line.to_string());
            continue;
        }

        let mut current = String::new();
        let mut used = 0;
        for character in line.chars() {
            let advance = UnicodeWidthChar::width(character).unwrap_or(0);
            if used + advance > columns && !current.is_empty() {
                wrapped.push(std::mem::take(&mut current));
                used = 0;
            }
            current.push(character);
            used += advance;
        }
        if !current.is_empty() {
            wrapped.push(current);
        }
    }
    wrapped
}

/// Pad `text` on the right to `columns` display columns.
pub fn pad(text: &str, columns: usize) -> String {
    let mut out = text.to_string();
    out.extend(std::iter::repeat_n(
        ' ',
        columns.saturating_sub(width(text)),
    ));
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use rstest::rstest;

    #[rstest]
    #[case("short", 16, "short")]
    #[case("exactly-sixteen!", 16, "exactly-sixteen!")]
    #[case("a-very-long-job-name", 10, "a-very-lo…")]
    // Zero means unlimited, as the config file documents.
    #[case("a-very-long-job-name", 0, "a-very-long-job-name")]
    fn truncates_to_a_column_budget(
        #[case] text: &str,
        #[case] max_width: usize,
        #[case] expected: &str,
    ) {
        assert_eq!(truncate(text, max_width), expected);
        assert!(width(&truncate(text, max_width)) <= max_width.max(width(expected)));
    }

    #[test]
    fn counts_wide_characters_as_two_columns() {
        // Four ideographs occupy eight columns, not four.
        assert_eq!(width("実験実験"), 8);

        let cut = truncate("実験実験", 5);
        // Two ideographs (4 columns) plus the ellipsis fits in 5; a third would not.
        assert_eq!(cut, "実験…");
        assert_eq!(width(&cut), 5);
    }

    #[test]
    fn never_splits_a_wide_character_across_the_budget() {
        // With 4 columns the budget is 3, which fits one ideograph and leaves a
        // column unused rather than half-drawing the second.
        let cut = truncate("実験実験", 4);
        assert_eq!(cut, "実…");
        assert!(width(&cut) <= 4);
    }

    #[test]
    fn combining_marks_take_no_columns_of_their_own() {
        // "e" + combining acute is one column, so this fits where naive
        // character counting would say it does not.
        let text = "e\u{0301}xperiment";
        assert_eq!(width(text), 10);
        assert_eq!(truncate(text, 10), text);
    }

    #[test]
    fn wraps_long_lines_and_leaves_short_ones() {
        assert_eq!(wrap("short\nalso short", 20), vec!["short", "also short"]);
        assert_eq!(
            wrap("abcdefghij", 4),
            vec!["abcd", "efgh", "ij"],
            "hard-wraps at the column budget"
        );
    }

    #[test]
    fn wrapping_never_splits_a_wide_character() {
        // Three columns cannot hold two ideographs, so the second moves down.
        assert_eq!(wrap("実験実験", 3), vec!["実", "験", "実", "験"]);
        for line in wrap("実験実験", 5) {
            assert!(width(&line) <= 5);
        }
    }

    #[test]
    fn a_zero_width_means_no_wrapping() {
        assert_eq!(wrap("a long line here", 0), vec!["a long line here"]);
    }

    #[test]
    fn wrapping_preserves_blank_lines_between_paragraphs() {
        assert_eq!(wrap("one\n\ntwo", 10), vec!["one", "", "two"]);
    }

    #[test]
    fn pads_to_a_column_count() {
        assert_eq!(pad("ab", 5), "ab   ");
        assert_eq!(pad("実験", 5), "実験 ");
        // Already at or over the width: left alone.
        assert_eq!(pad("abcdef", 3), "abcdef");
    }
}
