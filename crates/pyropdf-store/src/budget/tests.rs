//! Every charge beyond a limit refuses with the limit's code and leaves no
//! partial result (DESIGN.md section 9).

use std::thread;
use std::time::{Duration, Instant};

use proptest::prelude::*;

use super::*;

const MIB: u64 = 1024 * 1024;

#[test]
fn every_code_refuses_a_charge_beyond_its_limit_and_keeps_its_meter() {
    for code in LimitCode::ALL {
        let budget = Budget::default().with_limit(code, Some(10));
        assert_eq!(budget.charge(code, 4), Ok(()), "{code}");
        assert_eq!(budget.charge(code, 6), Ok(()), "{code}");
        assert_eq!(
            budget.charge(code, 1),
            Err(LimitExceeded::over(code, 10, 11)),
            "{code}"
        );
        assert_eq!(budget.used(code), 10, "{code}");
        assert_eq!(budget.remaining(code), Some(0), "{code}");
        budget.release(code, 3);
        assert_eq!(
            budget.charge(code, 4),
            Err(LimitExceeded::over(code, 10, 11)),
            "{code}"
        );
        assert_eq!(budget.charge(code, 3), Ok(()), "{code}");
        assert_eq!(budget.used(code), 10, "{code}");
    }
}

#[test]
fn every_code_refuses_a_measurement_beyond_its_limit_without_charging_it() {
    for code in LimitCode::ALL {
        let budget = Budget::default().with_limit(code, Some(10));
        assert_eq!(budget.check(code, 10), Ok(()), "{code}");
        assert_eq!(
            budget.check(code, 11),
            Err(LimitExceeded::over(code, 10, 11)),
            "{code}"
        );
        assert_eq!(budget.used(code), 0, "{code}");
    }
}

#[test]
fn charges_that_would_overflow_refuse() {
    let code = LimitCode::MemorySize;
    let budget = Budget::default().with_limit(code, Some(u64::MAX));
    assert_eq!(budget.charge(code, u64::MAX), Ok(()));
    assert_eq!(
        budget.charge(code, 1),
        Err(LimitExceeded::over(code, u64::MAX, u64::MAX))
    );
    assert_eq!(budget.used(code), u64::MAX);
}

#[test]
fn a_code_without_a_limit_counts_and_saturates() {
    let code = LimitCode::MemorySize;
    let budget = Budget::default();
    assert_eq!(budget.limit(code), None);
    assert_eq!(budget.remaining(code), None);
    assert_eq!(budget.charge(code, u64::MAX - 1), Ok(()));
    assert_eq!(budget.charge(code, 5), Ok(()));
    assert_eq!(budget.used(code), u64::MAX);
    assert_eq!(budget.check(code, u64::MAX), Ok(()));
    budget.release(code, u64::MAX);
    budget.release(code, 1);
    assert_eq!(budget.used(code), 0);
}

#[test]
fn codes_index_the_tables_of_a_budget_in_the_order_of_the_registry() {
    for (index, code) in LimitCode::ALL.into_iter().enumerate() {
        assert_eq!(code.index(), index);
        assert_eq!(code.to_string(), code.as_str());
    }
    let mut names: Vec<_> = LimitCode::ALL.iter().map(|code| code.as_str()).collect();
    names.sort_unstable();
    names.dedup();
    assert_eq!(names.len(), CODES);
}

#[test]
fn the_registry_defaults_apply_until_a_limit_replaces_them() {
    let budget = Budget::default();
    for code in LimitCode::ALL {
        assert_eq!(budget.limit(code), code.default_limit(), "{code}");
    }
    assert_eq!(budget.limit(LimitCode::StreamFilterCount), Some(16));
    assert_eq!(budget.limit(LimitCode::PasswordInputSize), Some(127));
    assert_eq!(budget.limit(LimitCode::ObjectValueDepth), Some(32));
    assert_eq!(budget.limit(LimitCode::StreamDataSize), Some(64 * MIB));
    assert_eq!(budget.limit(LimitCode::PixmapOutputSize), Some(64 * MIB));
    assert_eq!(budget.limit(LimitCode::PdfOutputSize), Some(512 * MIB));
    let budget = budget
        .with_limit(LimitCode::PdfOutputSize, None)
        .with_limit(LimitCode::StreamDataSize, Some(MIB));
    assert_eq!(budget.limit(LimitCode::PdfOutputSize), None);
    assert_eq!(budget.limit(LimitCode::StreamDataSize), Some(MIB));
    // pylopdf accepts 16 chained filters and a password of 127 UTF-8 bytes.
    assert_eq!(budget.check(LimitCode::StreamFilterCount, 16), Ok(()));
    assert_eq!(
        budget.check(LimitCode::StreamFilterCount, 17),
        Err(LimitExceeded::over(LimitCode::StreamFilterCount, 16, 17))
    );
    let password = "é".repeat(64);
    assert_eq!(
        budget.check(LimitCode::PasswordInputSize, password.len() as u64),
        Err(LimitExceeded::over(LimitCode::PasswordInputSize, 127, 128))
    );
    assert_eq!(budget.check(LimitCode::PasswordInputSize, 127), Ok(()));
}

#[test]
fn document_limits_set_the_codes_that_they_name() {
    let limits = DocumentLimits {
        max_file_size: Some(1),
        max_pages: Some(2),
        max_objects: Some(3),
        max_decompressed_size: Some(4),
        max_page_content_size: Some(5),
        max_total_decompressed_size: Some(6),
        max_object_depth: Some(7),
        max_interpretation_size: Some(8),
        max_memory_size: Some(9),
        ..DocumentLimits::default()
    };
    let budget = Budget::new(&limits);
    let set = [
        (LimitCode::FileSize, 1),
        (LimitCode::PageCount, 2),
        (LimitCode::ObjectCount, 3),
        (LimitCode::DecompressedSize, 4),
        (LimitCode::PageContentSize, 5),
        (LimitCode::TotalDecompressedSize, 6),
        (LimitCode::ObjectDepth, 7),
        (LimitCode::InterpretationSize, 8),
        (LimitCode::MemorySize, 9),
    ];
    for code in LimitCode::ALL {
        let expected = set
            .iter()
            .find(|&&(set_code, _)| set_code == code)
            .map_or(code.default_limit(), |&(_, limit)| Some(limit));
        assert_eq!(budget.limit(code), expected, "{code}");
    }
    assert_eq!(budget.deadline(), None);
}

#[test]
fn the_web_profile_is_pylopdfs() {
    let limits = DocumentLimits::web();
    assert_eq!(limits.max_file_size, Some(10 * MIB));
    assert_eq!(limits.max_pages, Some(200));
    assert_eq!(limits.max_objects, Some(100_000));
    assert_eq!(limits.max_decompressed_size, Some(64 * MIB));
    assert_eq!(limits.max_page_content_size, Some(10 * MIB));
    assert_eq!(limits.max_total_decompressed_size, Some(128 * MIB));
    assert_eq!(limits.max_object_depth, Some(64));
    assert_eq!(limits.max_interpretation_size, Some(64 * MIB));
    assert_eq!(limits.max_duration, None);
    assert_eq!(limits.cancellation, None);
    assert_eq!(
        DocumentLimits::default(),
        DocumentLimits {
            max_file_size: None,
            max_pages: None,
            max_objects: None,
            max_decompressed_size: None,
            max_page_content_size: None,
            max_total_decompressed_size: None,
            max_object_depth: None,
            max_interpretation_size: None,
            max_memory_size: None,
            max_duration: None,
            cancellation: None,
        }
    );
}

#[test]
fn a_passed_deadline_refuses_every_charge_and_check() {
    let budget = Budget::default().with_timeout(Duration::ZERO);
    let refusal = budget.poll().unwrap_err();
    assert_eq!(refusal.code(), LimitCode::Deadline);
    assert_eq!(refusal.limit(), Some(0));
    assert!(refusal.amount() >= refusal.limit());
    let code = LimitCode::ObjectCount;
    assert_eq!(
        budget.charge(code, 1).map_err(|refusal| refusal.code()),
        Err(LimitCode::Deadline)
    );
    assert_eq!(budget.used(code), 0);
    assert_eq!(
        budget.check(code, 0).map_err(|refusal| refusal.code()),
        Err(LimitCode::Deadline)
    );
}

#[test]
fn a_deadline_reports_its_length_in_milliseconds() {
    let start = Instant::now();
    let budget = Budget::default().with_deadline(start + Duration::from_millis(1500));
    assert_eq!(budget.poll(), Ok(()));
    let budget = Budget::default().with_timeout(Duration::from_millis(1));
    thread::sleep(Duration::from_millis(5));
    let refusal = budget.poll().unwrap_err();
    assert_eq!(refusal.code(), LimitCode::Deadline);
    assert_eq!(refusal.limit(), Some(1));
    assert!(refusal.amount() >= Some(5), "{refusal:?}");
}

#[test]
fn the_earliest_deadline_applies() {
    let now = Instant::now();
    let hour = Duration::from_secs(3600);
    let budget = Budget::default()
        .with_deadline(now + 2 * hour)
        .with_deadline(now + hour)
        .with_deadline(now + 3 * hour);
    assert_eq!(budget.deadline(), Some(now + hour));
    assert_eq!(budget.poll(), Ok(()));
    let limits = DocumentLimits {
        max_duration: Some(hour),
        ..DocumentLimits::default()
    };
    let budget = Budget::new(&limits);
    let deadline = budget.deadline().unwrap();
    assert!(deadline >= now + hour && deadline <= Instant::now() + hour);
    let budget = budget.with_timeout(Duration::ZERO);
    assert_eq!(
        budget.poll().map_err(|refusal| refusal.code()),
        Err(LimitCode::Deadline)
    );
}

#[test]
fn a_timeout_beyond_the_clock_sets_no_deadline() {
    let budget = Budget::default().with_timeout(Duration::MAX);
    assert_eq!(budget.deadline(), None);
    assert_eq!(budget.poll(), Ok(()));
}

#[test]
fn a_cancelled_token_refuses_every_charge_and_check() {
    let token = CancellationToken::new();
    let budget = Budget::default().with_cancellation(token.clone());
    let code = LimitCode::PageCount;
    assert_eq!(budget.charge(code, 1), Ok(()));
    assert!(!token.is_cancelled());
    token.cancel();
    assert!(token.is_cancelled());
    let cancelled = Err(LimitExceeded::new(LimitCode::Cancelled));
    assert_eq!(budget.poll(), cancelled);
    assert_eq!(budget.charge(code, 1), cancelled);
    assert_eq!(budget.check(code, 0), cancelled);
    assert_eq!(budget.used(code), 1);
}

#[test]
fn any_of_several_tokens_cancels() {
    let first = CancellationToken::new();
    let second = CancellationToken::new();
    let budget = Budget::default()
        .with_cancellation(first)
        .with_cancellation(second.clone());
    assert_eq!(budget.poll(), Ok(()));
    second.cancel();
    assert_eq!(budget.poll(), Err(LimitExceeded::new(LimitCode::Cancelled)));
}

#[test]
fn document_limits_carry_their_token_to_every_budget() {
    let token = CancellationToken::new();
    let limits = DocumentLimits {
        cancellation: Some(token.clone()),
        ..DocumentLimits::default()
    };
    assert_eq!(limits.cancellation, Some(token.clone()));
    assert_ne!(limits.cancellation, Some(CancellationToken::new()));
    let budgets = [Budget::new(&limits), Budget::new(&limits.clone())];
    token.cancel();
    for budget in budgets {
        assert_eq!(budget.poll(), Err(LimitExceeded::new(LimitCode::Cancelled)));
    }
}

#[test]
fn cancellation_comes_before_the_deadline_and_the_limit() {
    let token = CancellationToken::new();
    token.cancel();
    let code = LimitCode::ObjectCount;
    let budget = Budget::default()
        .with_limit(code, Some(0))
        .with_timeout(Duration::ZERO)
        .with_cancellation(token);
    assert_eq!(
        budget.charge(code, 1),
        Err(LimitExceeded::new(LimitCode::Cancelled))
    );
}

#[test]
fn threads_never_charge_beyond_a_limit() {
    let code = LimitCode::ObjectCount;
    let budget = Budget::default().with_limit(code, Some(10_000));
    let charged: usize = thread::scope(|scope| {
        let workers: Vec<_> = (0..8)
            .map(|_| {
                scope.spawn(|| {
                    (0..2_000)
                        .filter(|_| budget.charge(code, 1).is_ok())
                        .count()
                })
            })
            .collect();
        workers
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .sum()
    });
    assert_eq!(charged, 10_000);
    assert_eq!(budget.used(code), 10_000);
}

#[test]
fn a_stream_refuses_beyond_its_own_limit_and_keeps_its_size() {
    let budget = Budget::new(&DocumentLimits {
        max_decompressed_size: Some(100),
        ..DocumentLimits::default()
    });
    let mut stream = budget.decode(StreamKind::Any);
    assert_eq!(stream.remaining(), Some(100));
    assert_eq!(stream.charge(60), Ok(()));
    assert_eq!(
        stream.charge(41),
        Err(LimitExceeded::over(LimitCode::DecompressedSize, 100, 101))
    );
    assert_eq!(stream.size(), 60);
    assert_eq!(budget.used(LimitCode::TotalDecompressedSize), 60);
    assert_eq!(stream.charge(40), Ok(()));
    let mut other = budget.decode(StreamKind::Any);
    assert_eq!(other.charge(100), Ok(()));
    assert_eq!(budget.used(LimitCode::TotalDecompressedSize), 200);
}

#[test]
fn a_page_content_stream_also_meets_the_page_content_limit() {
    let budget = Budget::new(&DocumentLimits {
        max_decompressed_size: Some(100),
        max_page_content_size: Some(50),
        ..DocumentLimits::default()
    });
    let mut content = budget.decode(StreamKind::PageContent);
    assert_eq!(content.remaining(), Some(50));
    assert_eq!(
        content.charge(51),
        Err(LimitExceeded::over(LimitCode::PageContentSize, 50, 51))
    );
    assert_eq!(content.size(), 0);
    let mut other = budget.decode(StreamKind::Any);
    assert_eq!(other.charge(51), Ok(()));
}

#[test]
fn the_total_refuses_across_streams() {
    let budget = Budget::new(&DocumentLimits {
        max_decompressed_size: Some(100),
        max_total_decompressed_size: Some(150),
        ..DocumentLimits::default()
    });
    let mut first = budget.decode(StreamKind::Any);
    assert_eq!(first.charge(100), Ok(()));
    let mut second = budget.decode(StreamKind::Any);
    assert_eq!(second.remaining(), Some(50));
    assert_eq!(
        second.charge(51),
        Err(LimitExceeded::over(
            LimitCode::TotalDecompressedSize,
            150,
            151
        ))
    );
    assert_eq!(second.size(), 0);
    assert_eq!(budget.used(LimitCode::TotalDecompressedSize), 100);
}

#[test]
fn a_refusal_names_the_limit_that_the_bytes_cross_first() {
    let limits = DocumentLimits {
        max_decompressed_size: Some(100),
        max_page_content_size: Some(80),
        max_total_decompressed_size: Some(150),
        ..DocumentLimits::default()
    };
    // The page content limit has the least room.
    let budget = Budget::new(&limits);
    let mut content = budget.decode(StreamKind::PageContent);
    assert_eq!(
        content.charge(120),
        Err(LimitExceeded::over(LimitCode::PageContentSize, 80, 120))
    );
    // After another stream, the total has the least room.
    let mut other = budget.decode(StreamKind::Any);
    assert_eq!(other.charge(100), Ok(()));
    assert_eq!(content.remaining(), Some(50));
    assert_eq!(
        content.charge(120),
        Err(LimitExceeded::over(
            LimitCode::TotalDecompressedSize,
            150,
            220
        ))
    );
    // A limit on the stream wins a tie with the total.
    let budget = Budget::new(&DocumentLimits {
        max_decompressed_size: Some(100),
        max_total_decompressed_size: Some(100),
        ..DocumentLimits::default()
    });
    let mut stream = budget.decode(StreamKind::Any);
    assert_eq!(
        stream.charge(101),
        Err(LimitExceeded::over(LimitCode::DecompressedSize, 100, 101))
    );
    // The per-stream limit wins a tie with the page content limit.
    let budget = Budget::new(&DocumentLimits {
        max_decompressed_size: Some(50),
        max_page_content_size: Some(50),
        ..DocumentLimits::default()
    });
    let mut content = budget.decode(StreamKind::PageContent);
    assert_eq!(
        content.charge(51),
        Err(LimitExceeded::over(LimitCode::DecompressedSize, 50, 51))
    );
}

#[test]
fn a_stream_without_limits_is_unbounded() {
    let budget = Budget::default();
    let mut stream = budget.decode(StreamKind::PageContent);
    assert_eq!(stream.remaining(), None);
    assert_eq!(stream.charge(u64::MAX), Ok(()));
    assert_eq!(stream.size(), u64::MAX);
}

#[test]
fn decoding_stops_when_the_operation_is_cancelled() {
    let token = CancellationToken::new();
    let budget = Budget::default().with_cancellation(token.clone());
    let mut stream = budget.decode(StreamKind::Any);
    assert_eq!(stream.charge(10), Ok(()));
    token.cancel();
    assert_eq!(
        stream.charge(10),
        Err(LimitExceeded::new(LimitCode::Cancelled))
    );
    assert_eq!(stream.size(), 10);
}

#[test]
fn a_counting_vector_refuses_an_element_beyond_the_limit() {
    let budget = Budget::new(&DocumentLimits {
        max_objects: Some(3),
        ..DocumentLimits::default()
    });
    let mut entries = BudgetVec::counting(&budget, LimitCode::ObjectCount);
    for entry in 0..3 {
        assert_eq!(entries.try_push(entry), Ok(()));
    }
    assert_eq!(
        entries.try_push(3),
        Err(LimitExceeded::over(LimitCode::ObjectCount, 3, 4))
    );
    assert_eq!(entries.as_slice(), [0, 1, 2]);
    assert_eq!(budget.used(LimitCode::ObjectCount), 3);
}

#[test]
fn a_length_limit_applies_before_reserving_or_relocating_byte_output() {
    const LIMIT: usize = 40_000;
    let code = LimitCode::InterpretationSize;
    let budget = Budget::default().with_limit(code, Some(LIMIT as u64));
    let mut out = BudgetVec::with_length_limit(&budget, code);
    out.try_extend_from_slice(&vec![0u8; 24_000]).unwrap();
    let capacity = out.capacity();
    let extra = LIMIT - out.len();
    let refused = Err(LimitExceeded::over(code, LIMIT as u64, LIMIT as u64 + 1));
    assert_eq!(out.try_reserve_exact(extra + 1), refused);
    assert_eq!(out.try_reserve_polled(extra + 1), refused);
    assert_eq!(out.try_reserve(usize::MAX).unwrap_err().code(), code);
    assert_eq!(out.capacity(), capacity);
    // Geometric growth would exceed the limit; the exact output still fits.
    out.try_reserve_polled(extra).unwrap();
    out.try_extend_from_slice(&vec![1; extra]).unwrap();
    assert_eq!(out.try_push(2), refused);
    assert_eq!(out.len(), LIMIT);
    assert_eq!(budget.used(LimitCode::MemorySize), LIMIT as u64);
    assert_eq!(budget.used(code), 0);
}

#[test]
fn an_extension_adds_all_of_its_items_or_none() {
    let code = LimitCode::ObjectCount;
    let budget = Budget::default().with_limit(code, Some(5));
    let mut entries = BudgetVec::counting(&budget, code);
    assert_eq!(entries.try_extend_from_slice(&[0, 1]), Ok(()));
    assert_eq!(
        entries.try_extend_from_slice(&[2, 3, 4, 5]),
        Err(LimitExceeded::over(code, 5, 6))
    );
    assert_eq!(entries.as_slice(), [0, 1]);
    assert_eq!(budget.used(code), 2);
    assert_eq!(
        entries.try_extend(2..6),
        Err(LimitExceeded::over(code, 5, 6))
    );
    assert_eq!(entries.as_slice(), [0, 1]);
    assert_eq!(budget.used(code), 2);
    assert_eq!(entries.try_extend(2..5), Ok(()));
    assert_eq!(entries.as_slice(), [0, 1, 2, 3, 4]);
    assert_eq!(budget.used(code), 5);
}

#[test]
fn growth_beyond_the_memory_limit_refuses_and_keeps_the_buffer() {
    let code = LimitCode::MemorySize;
    let budget = Budget::default().with_limit(code, Some(64));
    let mut bytes = BudgetVec::new(&budget);
    assert_eq!(bytes.try_extend_from_slice(&[7_u8; 64]), Ok(()));
    assert_eq!(budget.used(code), 64);
    let capacity = bytes.capacity();
    assert_eq!(bytes.try_push(8), Err(LimitExceeded::over(code, 64, 65)));
    assert_eq!(bytes.try_reserve(1), Err(LimitExceeded::over(code, 64, 65)));
    assert_eq!(
        bytes.try_reserve_exact(1),
        Err(LimitExceeded::over(code, 64, 65))
    );
    assert_eq!(bytes.len(), 64);
    assert_eq!(bytes.capacity(), capacity);
    assert_eq!(budget.used(code), 64);
}

#[test]
fn growth_falls_back_to_the_exact_size_when_doubling_does_not_fit() {
    let code = LimitCode::MemorySize;
    let budget = Budget::default().with_limit(code, Some(100));
    let mut bytes = BudgetVec::new(&budget);
    assert_eq!(bytes.try_extend_from_slice(&[0_u8; 60]), Ok(()));
    assert_eq!(budget.used(code), 60);
    assert_eq!(bytes.try_push(1), Ok(()));
    assert_eq!(budget.used(code), 61);
    assert_eq!(bytes.try_reserve_exact(39), Ok(()));
    assert_eq!(budget.used(code), 100);
}

#[test]
fn growth_doubles_when_the_budget_allows_it() {
    let code = LimitCode::MemorySize;
    let budget = Budget::default();
    let mut words = BudgetVec::new(&budget);
    for word in 0_u32..5 {
        assert_eq!(words.try_push(word), Ok(()));
    }
    // Four elements first, as the standard library starts, then eight.
    assert_eq!(budget.used(code), 8 * 4);
    assert!(words.capacity() >= 8);
}

#[test]
fn a_refused_allocation_releases_its_charge() {
    let code = LimitCode::MemorySize;
    let budget = Budget::default();
    let mut bytes = BudgetVec::<u8>::new(&budget);
    assert_eq!(bytes.try_push(1), Ok(()));
    let used = budget.used(code);
    // The allocator cannot provide `isize::MAX` bytes.
    let refusal = bytes
        .try_reserve_exact(isize::MAX.unsigned_abs() - 1)
        .unwrap_err();
    assert_eq!(refusal.code(), code);
    assert_eq!(refusal.limit(), None);
    assert_eq!(budget.used(code), used);
    // No allocation holds more than `isize::MAX` bytes, which is refused
    // before anything is charged.
    let refusal = bytes.try_reserve(usize::MAX).unwrap_err();
    assert_eq!(refusal, LimitExceeded::new(code));
    let mut words = BudgetVec::<u64>::new(&budget);
    let refusal = words.try_reserve(usize::MAX / 2).unwrap_err();
    assert_eq!(refusal.code(), code);
    assert_eq!(budget.used(code), used);
    assert_eq!(bytes.as_slice(), [1]);
}

#[test]
fn removing_elements_releases_their_count() {
    let code = LimitCode::PageCount;
    let budget = Budget::default();
    let mut pages = BudgetVec::counting(&budget, code);
    assert_eq!(pages.try_extend(0..6), Ok(()));
    assert_eq!(budget.used(code), 6);
    assert_eq!(pages.pop(), Some(5));
    assert_eq!(budget.used(code), 5);
    pages.truncate(2);
    assert_eq!(budget.used(code), 2);
    pages.truncate(10);
    assert_eq!(budget.used(code), 2);
    pages.clear();
    assert_eq!(budget.used(code), 0);
    assert_eq!(pages.pop(), None);
    assert!(pages.is_empty());
    assert!(budget.used(LimitCode::MemorySize) > 0);
}

#[test]
fn dropping_a_vector_releases_its_charges() {
    let budget = Budget::default();
    {
        let mut pages = BudgetVec::counting(&budget, LimitCode::PageCount);
        assert_eq!(pages.try_extend(0_u64..100), Ok(()));
        assert_eq!(budget.used(LimitCode::PageCount), 100);
        assert!(budget.used(LimitCode::MemorySize) >= 800);
    }
    assert_eq!(budget.used(LimitCode::PageCount), 0);
    assert_eq!(budget.used(LimitCode::MemorySize), 0);
}

#[test]
fn into_vec_leaves_the_charges_with_the_budget() {
    let budget = Budget::default();
    let mut pages = BudgetVec::counting(&budget, LimitCode::PageCount);
    assert_eq!(pages.try_extend(0_u64..10), Ok(()));
    let memory = budget.used(LimitCode::MemorySize);
    let pages = pages.into_vec();
    assert_eq!(pages, (0..10).collect::<Vec<_>>());
    assert_eq!(budget.used(LimitCode::PageCount), 10);
    assert_eq!(budget.used(LimitCode::MemorySize), memory);
}

#[test]
fn zero_sized_elements_charge_no_memory() {
    let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
    let mut units = BudgetVec::counting(&budget, LimitCode::ObjectCount);
    assert_eq!(units.try_extend(std::iter::repeat_n((), 1000)), Ok(()));
    assert_eq!(units.len(), 1000);
    assert_eq!(budget.used(LimitCode::MemorySize), 0);
    assert_eq!(budget.used(LimitCode::ObjectCount), 1000);
}

#[test]
fn a_vector_reads_and_changes_like_a_slice() {
    let budget = Budget::default();
    let mut numbers = BudgetVec::new(&budget);
    assert_eq!(numbers.try_extend([3, 1, 2]), Ok(()));
    numbers.sort_unstable();
    for number in &mut numbers {
        *number *= 10;
    }
    assert_eq!(numbers.iter().sum::<i32>(), 60);
    assert_eq!(
        (&numbers).into_iter().copied().collect::<Vec<_>>(),
        [10, 20, 30]
    );
    assert_eq!(numbers.as_mut_slice(), [10, 20, 30]);
    assert_eq!(format!("{numbers:?}"), "[10, 20, 30]");
    assert!(std::ptr::eq(numbers.budget(), &budget));
}

/// A change to the meters of three codes.
#[derive(Clone, Debug)]
enum MeterOp {
    Charge(usize, u64),
    Release(usize, u64),
    Check(usize, u64),
}

fn meter_op() -> impl Strategy<Value = MeterOp> {
    prop_oneof![
        (0..3_usize, 0..20_u64).prop_map(|(code, amount)| MeterOp::Charge(code, amount)),
        (0..3_usize, 0..20_u64).prop_map(|(code, amount)| MeterOp::Release(code, amount)),
        (0..3_usize, 0..40_u64).prop_map(|(code, value)| MeterOp::Check(code, value)),
    ]
}

/// A change to a vector of numbers.
#[derive(Clone, Debug)]
enum VecOp {
    Push(u32),
    Extend(Vec<u32>),
    Pop,
    Truncate(usize),
}

fn vec_op() -> impl Strategy<Value = VecOp> {
    prop_oneof![
        any::<u32>().prop_map(VecOp::Push),
        proptest::collection::vec(any::<u32>(), 0..6).prop_map(VecOp::Extend),
        Just(VecOp::Pop),
        (0..8_usize).prop_map(VecOp::Truncate),
    ]
}

proptest! {
    #[test]
    fn meters_follow_a_model(
        limits in proptest::collection::vec(proptest::option::of(0..40_u64), 3),
        ops in proptest::collection::vec(meter_op(), 0..64),
    ) {
        let codes = [LimitCode::FileSize, LimitCode::ObjectCount, LimitCode::MemorySize];
        let mut budget = Budget::default();
        for (&code, &limit) in codes.iter().zip(&limits) {
            budget = budget.with_limit(code, limit);
        }
        let mut used = [0_u64; 3];
        for op in ops {
            match op {
                MeterOp::Charge(index, amount) => {
                    let total = used[index] + amount;
                    let expected = match limits[index] {
                        Some(limit) if total > limit => Err(LimitExceeded::over(codes[index], limit, total)),
                        _ => {
                            used[index] = total;
                            Ok(())
                        }
                    };
                    prop_assert_eq!(budget.charge(codes[index], amount), expected);
                }
                MeterOp::Release(index, amount) => {
                    budget.release(codes[index], amount);
                    used[index] = used[index].saturating_sub(amount);
                }
                MeterOp::Check(index, value) => {
                    let expected = match limits[index] {
                        Some(limit) if value > limit => Err(LimitExceeded::over(codes[index], limit, value)),
                        _ => Ok(()),
                    };
                    prop_assert_eq!(budget.check(codes[index], value), expected);
                }
            }
            for (&code, &used) in codes.iter().zip(&used) {
                prop_assert_eq!(budget.used(code), used);
            }
        }
    }

    #[test]
    fn a_vector_changes_only_as_its_budget_allows(
        elements in 0..24_u64,
        memory in 0..160_u64,
        ops in proptest::collection::vec(vec_op(), 0..48),
    ) {
        let count = LimitCode::ObjectCount;
        let bytes = LimitCode::MemorySize;
        let budget = Budget::default()
            .with_limit(count, Some(elements))
            .with_limit(bytes, Some(memory));
        let mut model: Vec<u32> = Vec::new();
        {
            let mut numbers = BudgetVec::counting(&budget, count);
            for op in ops {
                let capacity = numbers.capacity();
                let added = match op {
                    VecOp::Push(number) => Some((numbers.try_push(number), vec![number])),
                    VecOp::Extend(items) => Some((numbers.try_extend_from_slice(&items), items)),
                    VecOp::Pop => {
                        prop_assert_eq!(numbers.pop(), model.pop());
                        None
                    }
                    VecOp::Truncate(len) => {
                        numbers.truncate(len);
                        model.truncate(len);
                        None
                    }
                };
                if let Some((result, items)) = added {
                    // The elements must fit, and the buffer must already
                    // hold them or grow to exactly their number.
                    let needed = model.len() + items.len();
                    let total = needed as u64;
                    let expected = if total > elements {
                        Err(LimitExceeded::over(count, elements, total))
                    } else if needed > capacity && total * 4 > memory {
                        Err(LimitExceeded::over(bytes, memory, total * 4))
                    } else {
                        model.extend(&items);
                        Ok(())
                    };
                    prop_assert_eq!(result, expected);
                }
                prop_assert_eq!(numbers.as_slice(), model.as_slice());
                prop_assert_eq!(budget.used(count), model.len() as u64);
                prop_assert!(budget.used(bytes) >= model.len() as u64 * 4);
                prop_assert!(budget.used(bytes) <= memory);
            }
        }
        prop_assert_eq!(budget.used(count), 0);
        prop_assert_eq!(budget.used(bytes), 0);
    }
}

#[test]
fn clones_of_a_shared_budget_charge_one_operation() {
    let operation = Budget::default()
        .with_limit(LimitCode::MemorySize, Some(10))
        .share();
    let other = operation.clone();
    other.charge(LimitCode::MemorySize, 6).unwrap();
    assert_eq!(operation.used(LimitCode::MemorySize), 6);
    assert!(operation.charge(LimitCode::MemorySize, 5).is_err());
    let independent = Budget::default()
        .with_limit(LimitCode::MemorySize, Some(10))
        .share();
    independent.charge(LimitCode::MemorySize, 10).unwrap();
    assert_eq!(operation.used(LimitCode::MemorySize), 6);
    assert!(!independent.same_operation(&operation));
    assert!(other.same_operation(&operation) && other.ptr_eq(&operation));
}

#[test]
fn a_root_tightened_after_sharing_is_a_new_view_that_the_clone_does_not_relax() {
    // The configuration of a shared budget is frozen: tightening the root
    // after a clone yields a view, and a charge of 6 through the view is
    // refused although the older clone still allows 10.
    let root = Budget::default()
        .with_limit(LimitCode::MemorySize, Some(10))
        .share();
    let clone = root.clone();
    let tightened = root.tightened(LimitCode::MemorySize, 5).unwrap();
    assert!(tightened.charge(LimitCode::MemorySize, 6).is_err());
    assert_eq!(clone.limit(LimitCode::MemorySize), Some(10));
    clone.charge(LimitCode::MemorySize, 6).unwrap();
    // The operation's meter is now above the view's limit: the view refuses
    // even to poll, and a further view cannot be taken.
    assert_eq!(tightened.poll().unwrap_err().code(), LimitCode::MemorySize);
    assert!(root.tightened(LimitCode::MemorySize, 5).is_err());
    // A view never relaxes: a higher limit keeps the operation's.
    let higher = root.tightened(LimitCode::MemorySize, 20).unwrap();
    assert_eq!(higher.limit(LimitCode::MemorySize), Some(10));
    assert!(higher.charge(LimitCode::MemorySize, 5).is_err());
}

#[test]
fn a_locally_tightened_view_refuses_preexisting_usage() {
    let operation = Budget::default()
        .with_limit(LimitCode::MemorySize, Some(10))
        .share();
    operation.charge(LimitCode::MemorySize, 6).unwrap();
    let error = operation.tightened(LimitCode::MemorySize, 5).unwrap_err();
    assert_eq!(error.code(), LimitCode::MemorySize);
    let source = Budget::default().with_limit(LimitCode::MemorySize, Some(5));
    assert!(operation.constrained_by(&source).is_err());
    // The sibling views keep their own limit: the operation still polls.
    operation.poll().unwrap();
    operation.release(LimitCode::MemorySize, 6);
    let view = operation.constrained_by(&source).unwrap();
    view.charge(LimitCode::MemorySize, 5).unwrap();
    assert!(view.charge(LimitCode::MemorySize, 1).is_err());
    operation.charge(LimitCode::MemorySize, 1).unwrap();
    assert!(view.poll().is_err());
    operation.poll().unwrap();
}

#[test]
fn a_deadline_or_cancellation_reaches_every_clone_of_a_shared_budget() {
    // A shared budget takes no new deadline or token: a clone made before
    // a view observes the same configuration as one made after it, and a
    // view that adds a deadline or token adds it to itself alone.
    let token = CancellationToken::new();
    let operation = Budget::default().with_cancellation(token.clone()).share();
    let early = operation.clone();
    token.cancel();
    assert_eq!(early.poll().unwrap_err().code(), LimitCode::Cancelled);
    assert_eq!(operation.poll().unwrap_err().code(), LimitCode::Cancelled);

    let operation = Budget::default().share();
    let early = operation.clone();
    let source_token = CancellationToken::new();
    let source = Budget::default()
        .with_timeout(Duration::from_secs(3600))
        .with_cancellation(source_token.clone());
    let view = operation.constrained_by(&source).unwrap();
    assert!(view.deadline().is_some());
    assert!(early.deadline().is_none() && operation.deadline().is_none());
    source_token.cancel();
    assert_eq!(view.poll().unwrap_err().code(), LimitCode::Cancelled);
    early.poll().unwrap();
    let expired = Budget::default().with_timeout(Duration::ZERO);
    assert_eq!(
        operation.constrained_by(&expired).unwrap_err().code(),
        LimitCode::Deadline
    );
}

#[test]
fn a_view_keeps_the_earlier_deadline_of_its_operation() {
    let operation = Budget::default().with_timeout(Duration::ZERO).share();
    let later = Budget::default().with_timeout(Duration::from_secs(3600));
    assert_eq!(
        operation.constrained_by(&later).unwrap_err().code(),
        LimitCode::Deadline
    );
}

#[test]
fn a_cached_bound_read_from_a_view_stays_its_limit() {
    // A consumer that reads a check limit once, as the content parser does
    // for `content_operation_count`, and checks only above it, sees the
    // limit of the view it was given for its whole run.
    let root = Budget::default()
        .with_limit(LimitCode::ContentOperationCount, Some(10))
        .share();
    let view = root.tightened(LimitCode::ContentOperationCount, 5).unwrap();
    let cached = view.limit(LimitCode::ContentOperationCount);
    assert_eq!(cached, Some(5));
    assert!(view.check(LimitCode::ContentOperationCount, 6).is_err());
    assert!(root.check(LimitCode::ContentOperationCount, 8).is_ok());
    assert_eq!(view.limit(LimitCode::ContentOperationCount), cached);
}

#[test]
fn shared_views_charge_concurrently_within_the_operation_limit() {
    let operation = Budget::default()
        .with_limit(LimitCode::MemorySize, Some(1000))
        .share();
    let accepted: u64 = std::thread::scope(|scope| {
        let handles: Vec<_> = (0..8)
            .map(|_| {
                let view = operation.tightened(LimitCode::MemorySize, 900).unwrap();
                scope.spawn(move || {
                    (0..200)
                        .filter(|_| view.charge(LimitCode::MemorySize, 1).is_ok())
                        .count() as u64
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .sum()
    });
    assert_eq!(accepted, 900);
    assert_eq!(operation.used(LimitCode::MemorySize), 900);
}

#[test]
fn set_and_map_reservations_charge_memory_and_refuse_beyond_the_limit() {
    use std::collections::{HashMap, HashSet};

    let set_entry = (64 + 2 * size_of::<u32>()) as u64;
    let map_entry = (64 + 2 * size_of::<(u32, u64)>()) as u64;

    let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(2 * set_entry));
    let mut set = HashSet::new();
    assert_eq!(try_insert_set(&mut set, 10_u32, &budget), Ok(true));
    assert_eq!(budget.used(LimitCode::MemorySize), set_entry);
    // Re-inserting an existing element charges nothing.
    assert_eq!(try_insert_set(&mut set, 10_u32, &budget), Ok(false));
    assert_eq!(budget.used(LimitCode::MemorySize), set_entry);
    assert_eq!(try_insert_set(&mut set, 20_u32, &budget), Ok(true));
    assert_eq!(budget.used(LimitCode::MemorySize), 2 * set_entry);
    assert_eq!(
        try_insert_set(&mut set, 30_u32, &budget),
        Err(LimitExceeded::over(
            LimitCode::MemorySize,
            2 * set_entry,
            3 * set_entry
        ))
    );
    assert_eq!(set.len(), 2);
    assert!(!set.contains(&30));
    assert_eq!(budget.used(LimitCode::MemorySize), 2 * set_entry);

    let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(map_entry));
    let mut map = HashMap::new();
    assert_eq!(try_insert_map(&mut map, 1_u32, 100_u64, &budget), Ok(None));
    assert_eq!(budget.used(LimitCode::MemorySize), map_entry);
    // Replacing the value of an existing key needs no extra slot.
    assert_eq!(
        try_insert_map(&mut map, 1_u32, 200_u64, &budget),
        Ok(Some(100))
    );
    assert_eq!(map.get(&1), Some(&200));
    assert_eq!(budget.used(LimitCode::MemorySize), map_entry);
    assert_eq!(
        try_insert_map(&mut map, 2_u32, 300_u64, &budget),
        Err(LimitExceeded::over(
            LimitCode::MemorySize,
            map_entry,
            2 * map_entry
        ))
    );
    assert_eq!(
        try_reserve_map(&mut map, 1, &budget),
        Err(LimitExceeded::over(
            LimitCode::MemorySize,
            map_entry,
            2 * map_entry
        ))
    );
    assert_eq!(map.len(), 1);
    assert_eq!(budget.used(LimitCode::MemorySize), map_entry);
}

#[test]
fn set_and_map_refused_allocations_release_their_charges_and_obey_cancellation() {
    use std::collections::{HashMap, HashSet};

    let budget = Budget::default();
    let mut set = HashSet::<u64>::new();
    assert_eq!(try_insert_set(&mut set, 1, &budget), Ok(true));
    let used = budget.used(LimitCode::MemorySize);
    let huge_set = (isize::MAX.unsigned_abs() / (64 + 2 * size_of::<u64>())) - 1;
    for impossible in [huge_set, usize::MAX] {
        let refusal = try_reserve_set(&mut set, impossible, &budget).unwrap_err();
        assert_eq!(refusal.code(), LimitCode::MemorySize);
        assert_eq!(refusal.limit(), None);
        assert_eq!(budget.used(LimitCode::MemorySize), used);
    }

    let mut map = HashMap::<u64, u64>::new();
    assert_eq!(try_insert_map(&mut map, 1, 2, &budget), Ok(None));
    let used = budget.used(LimitCode::MemorySize);
    let huge_map = (isize::MAX.unsigned_abs() / (64 + 2 * size_of::<(u64, u64)>())) - 1;
    for impossible in [huge_map, usize::MAX] {
        let refusal = try_reserve_map(&mut map, impossible, &budget).unwrap_err();
        assert_eq!(refusal.code(), LimitCode::MemorySize);
        assert_eq!(refusal.limit(), None);
        assert_eq!(budget.used(LimitCode::MemorySize), used);
    }

    let token = CancellationToken::new();
    let budget = Budget::default().with_cancellation(token.clone());
    assert_eq!(try_insert_set(&mut set, 1, &budget), Ok(false));
    assert_eq!(try_insert_map(&mut map, 1, 3, &budget), Ok(Some(2)));
    token.cancel();
    assert_eq!(
        try_insert_set(&mut set, 1, &budget),
        Err(LimitExceeded::new(LimitCode::Cancelled))
    );
    assert_eq!(
        try_insert_map(&mut map, 1, 4, &budget),
        Err(LimitExceeded::new(LimitCode::Cancelled))
    );
    assert_eq!(map.get(&1), Some(&3));
}
