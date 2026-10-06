use crate::b::from_b;

pub fn from_a(n: i32) -> i32 {
    if n <= 0 {
        return 0;
    }
    from_b(n - 1) + 1
}

pub fn dup_block(values: &[i32]) -> i32 {
    let mut total = 0;
    for v in values {
        if *v > 10 {
            total += *v * 2;
        } else if *v < -10 {
            total -= *v * 3;
        } else {
            total += *v;
        }
        println!("running total {}", total);
    }
    total
}
