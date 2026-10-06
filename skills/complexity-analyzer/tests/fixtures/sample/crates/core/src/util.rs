pub fn add_checked(a: i32, b: i32) -> i32 {
    a.checked_add(b).unwrap_or(i32::MAX)
}

pub fn never_used_elsewhere(a: i32) -> i32 {
    a + 1
}

pub fn merge_all<A: Clone, B: Clone, C: Clone>(a: A, b: B, c: C) -> (A, B, C) {
    (a.clone(), b.clone(), c.clone())
}

pub fn signs(xs: &[i32]) -> Vec<&'static str> {
    xs.iter()
        .map(|x| {
            if *x > 0 {
                if *x > 100 {
                    "big"
                } else {
                    "pos"
                }
            } else {
                "non-pos"
            }
        })
        .collect()
}
