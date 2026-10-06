use crate::a::from_a;

pub fn from_b(n: i32) -> i32 {
    if n <= 0 {
        return 0;
    }
    from_a(n - 1) + 1
}

pub fn dup_block_copy(values: &[i32]) -> i32 {
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
