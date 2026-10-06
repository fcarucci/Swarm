use core_lib::{classify, Repo, Shape, Square};

fn main() {
    let mut r = Repo::new();
    r.insert("k", 1);
    println!("{} {}", classify(5, true), Square(2.0).area());
}
