pub mod a;
pub mod b;
pub mod util;

/// Only one implementation exists below (single-impl trait).
pub trait Storage {
    fn put(&mut self, key: &str, value: i32);
}

pub struct MemStorage {
    pub items: Vec<(String, i32)>,
}

impl Storage for MemStorage {
    fn put(&mut self, key: &str, value: i32) {
        self.items.push((key.to_string(), value));
    }
}

/// Two implementations: a healthy abstraction.
pub trait Shape {
    fn area(&self) -> f64;
}

pub struct Square(pub f64);
pub struct Circle(pub f64);

impl Shape for Square {
    fn area(&self) -> f64 {
        self.0 * self.0
    }
}

impl Shape for Circle {
    fn area(&self) -> f64 {
        3.14 * self.0 * self.0
    }
}

pub struct Repo {
    store: MemStorage,
}

impl Repo {
    pub fn new() -> Self {
        Repo { store: MemStorage { items: Vec::new() } }
    }

    /// Pass-through: forwards its arguments to one other call.
    pub fn insert(&mut self, key: &str, value: i32) {
        self.store.put(key, value)
    }
}

pub fn forward(x: i32, y: i32) -> i32 {
    util::add_checked(x, y)
}

pub fn classify(n: i32, flag: bool) -> &'static str {
    if n > 0 {
        if flag {
            for i in 0..n {
                if i % 2 == 0 && i > 3 {
                    return "big-even";
                } else if i % 3 == 0 {
                    return "big-three";
                }
            }
        } else if n > 100 {
            return "huge";
        }
        "positive"
    } else if n < 0 {
        "negative"
    } else {
        "zero"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_zero() {
        assert_eq!(classify(0, false), "zero");
    }
}
